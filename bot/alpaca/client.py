"""Conexión y validación del cliente Alpaca Markets."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock, TradeAccount

from bot.config import Settings
from bot.security.audit import audit
from bot.security.errors import log_caught
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
        self.limiter = limiter or RateLimiter(
            RateLimitConfig(
                data_per_minute=settings.api_data_per_minute,
                order_per_minute=settings.order_per_minute,
                order_per_day=settings.order_per_day,
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
        return AccountSnapshot(
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

    def validate_connection(self) -> AccountSnapshot:
        """Comprueba credenciales, estado de cuenta y reloj de mercado."""
        logger.info("Validando conexion con Alpaca...")
        try:
            snapshot = self.snapshot_account()
            clock = self.get_clock()
        except Exception as exc:
            log_caught(logger, "auth_failed", exc, paper=self.settings.paper)
            audit("auth", "deny", reason="connection_failed")
            raise ConnectionError("No se pudo validar la conexion con Alpaca.") from None

        if snapshot.account_blocked or snapshot.trading_blocked:
            audit("auth", "deny", reason="account_blocked")
            raise RuntimeError("La cuenta Alpaca no permite operar.")

        if str(snapshot.status).lower() not in {"active", "accountstatus.active"}:
            logger.warning("Estado de cuenta inesperado")
            audit("auth", "warn", reason="unexpected_status")

        audit("auth", "allow", paper=snapshot.paper, market_open=clock.is_open)
        logger.info(
            "Conexion OK | paper=%s | status=%s | equity=%.2f %s | "
            "buying_power=%.2f | cash=%.2f | market_open=%s",
            snapshot.paper,
            snapshot.status,
            snapshot.equity,
            snapshot.currency,
            snapshot.buying_power,
            snapshot.cash,
            clock.is_open,
        )
        if not clock.is_open:
            logger.info(
                "Mercado cerrado. Proxima apertura: %s | cierre: %s",
                clock.next_open,
                clock.next_close,
            )
        return snapshot
