"""Conexión y validación del cliente Alpaca Markets."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock, TradeAccount

from bot.alpaca.market_clock import MarketClockView, crypto_fallback_clock, from_alpaca_clock
from bot.config import Settings
from bot.security.audit import audit
from bot.security.ratelimit import RateLimitConfig, RateLimiter
from bot.security.secrets import mask_secret

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountSnapshot:
    id: str
    status: str
    currency: str
    cash: float
    buying_power: float
    equity: float
    portfolio_value: float
    pattern_day_trader: bool
    trading_blocked: bool
    account_blocked: bool
    paper: bool


class AlpacaClient:
    """Fachada sobre TradingClient y StockHistoricalDataClient."""

    def __init__(self, settings: Settings, limiter: RateLimiter | None = None) -> None:
        self.settings = settings
        data_budget = max(int(settings.api_data_per_minute), 80)
        self.limiter = limiter or RateLimiter(
            RateLimitConfig(
                data_per_minute=settings.api_data_per_minute,
                order_per_minute=settings.order_per_minute,
                order_per_day=settings.order_per_day,
                burst_data=data_budget,
            )
        )
        self.trading = TradingClient(
            api_key=settings.api_key_id,
            secret_key=settings.api_secret_key,
            paper=settings.paper,
            url_override=settings.api_base_url,
        )
        self.data = StockHistoricalDataClient(
            api_key=settings.api_key_id,
            secret_key=settings.api_secret_key,
        )
        self.crypto_data = CryptoHistoricalDataClient(
            api_key=settings.api_key_id,
            secret_key=settings.api_secret_key,
        )
        self._account_snapshot_cache: AccountSnapshot | None = None
        self._account_snapshot_cached_at = 0.0
        logger.info(
            "Cliente Alpaca inicializado | paper=%s | base_url=%s | key=%s",
            settings.paper,
            settings.api_base_url,
            mask_secret(settings.api_key_id),
        )

    def get_account(self) -> TradeAccount:
        self.limiter.acquire("trading_read")
        return self.trading.get_account()

    def get_clock(self) -> Clock:
        self.limiter.acquire("trading_read")
        return self.trading.get_clock()

    def snapshot_account(self) -> AccountSnapshot:
        account = self.get_account()
        snapshot = AccountSnapshot(
            id=str(account.id),
            status=str(account.status),
            currency=str(account.currency),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
            equity=float(account.equity),
            portfolio_value=float(account.portfolio_value),
            pattern_day_trader=bool(account.pattern_day_trader),
            trading_blocked=bool(account.trading_blocked),
            account_blocked=bool(account.account_blocked),
            paper=self.settings.paper,
        )
        self._account_snapshot_cache = snapshot
        self._account_snapshot_cached_at = time.monotonic()
        return snapshot

    def cached_account_snapshot(
        self, max_age_seconds: float | None = 180.0
    ) -> AccountSnapshot | None:
        snapshot = self._account_snapshot_cache
        if snapshot is None:
            return None
        if max_age_seconds is None:
            return snapshot
        age = max(0.0, time.monotonic() - self._account_snapshot_cached_at)
        if age > max_age_seconds:
            return None
        return snapshot

    def get_market_clock(self) -> MarketClockView:
        """Reloj NYSE; si falla, asume cerrado y sigue en modo cripto."""
        try:
            return from_alpaca_clock(self.get_clock())
        except Exception as exc:
            logger.info(
                "Reloj de acciones no disponible (%s) — se asume mercado cerrado / modo cripto",
                type(exc).__name__,
            )
            return crypto_fallback_clock()

    def snapshot_account_optional(self) -> AccountSnapshot | None:
        try:
            return self.snapshot_account()
        except Exception as exc:
            cached = self.cached_account_snapshot()
            if cached is not None:
                age = max(0.0, time.monotonic() - self._account_snapshot_cached_at)
                logger.warning(
                    "Cuenta Alpaca no consultada en este ciclo (%s) — usando snapshot cacheado %.1fs "
                    "para no bloquear señales ni órdenes",
                    type(exc).__name__,
                    age,
                )
                return cached
            logger.warning(
                "Cuenta Alpaca no consultada en este ciclo (%s) — sin cache disponible; "
                "solo se podrán ejecutar cierres defensivos",
                type(exc).__name__,
            )
            return None

    def validate_startup(self) -> tuple[AccountSnapshot | None, MarketClockView]:
        """Validación no bloqueante: credenciales útiles pero sin exigir mercado de acciones."""
        logger.info("Validando conexion con Alpaca (modo hibrido)...")
        snapshot: AccountSnapshot | None = None
        clock = crypto_fallback_clock()

        try:
            snapshot = self.snapshot_account()
        except Exception as exc:
            logger.info(
                "Cuenta Alpaca no disponible al arranque (%s) — se continua en modo hibrido",
                type(exc).__name__,
            )

        try:
            clock = from_alpaca_clock(self.get_clock())
        except Exception as exc:
            logger.info(
                "Reloj NYSE omitido (%s) — operacion cripto / fuera de horario permitida",
                type(exc).__name__,
            )
            clock = crypto_fallback_clock()

        if snapshot is not None:
            if snapshot.account_blocked or snapshot.trading_blocked:
                logger.warning(
                    "Cuenta con restricciones (blocked=%s trading_blocked=%s) — "
                    "solo se operara cripto si el broker lo permite",
                    snapshot.account_blocked,
                    snapshot.trading_blocked,
                )
            audit(
                "auth",
                "allow",
                paper=snapshot.paper,
                market_open=clock.is_open,
                account_ok=True,
            )
            logger.info(
                "Conexion OK | paper=%s | status=%s | equity=%.2f %s | "
                "buying_power=%.2f | cash=%.2f | mercado_acciones=%s",
                snapshot.paper,
                snapshot.status,
                snapshot.equity,
                snapshot.currency,
                snapshot.buying_power,
                snapshot.cash,
                "abierto" if clock.is_open else "cerrado",
            )
            if not clock.is_open and clock.stock_feed_ok:
                logger.info(
                    "Mercado de acciones cerrado — modo cripto activo si corresponde"
                )
        else:
            audit("auth", "warn", reason="account_unavailable", market_open=clock.is_open)
            logger.info(
                "Arranque sin snapshot de cuenta — el motor puede operar cripto en seco"
            )

        return snapshot, clock

    def validate_connection(self) -> AccountSnapshot:
        """Compatibilidad: validacion estricta solo para --validate."""
        snapshot, clock = self.validate_startup()
        if snapshot is None:
            raise ConnectionError("No se pudo leer la cuenta Alpaca.")
        if snapshot.account_blocked or snapshot.trading_blocked:
            raise RuntimeError("La cuenta Alpaca no permite operar.")
        return snapshot
