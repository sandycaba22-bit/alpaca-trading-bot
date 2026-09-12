"""Umbral de polvo (qty residual no operable) tras ventas parciales."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bot.market.assets import is_crypto_symbol, normalize_symbol

if TYPE_CHECKING:
    from bot.alpaca.client import AlpacaClient
    from bot.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class DustThresholdCache:
    """Mínimos operables por símbolo cripto (Alpaca min_order_size), con fallback env."""

    mins: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    updated_at: float = 0.0

    def floor_for(self, settings: Settings, symbol: str) -> tuple[float, str]:
        key = normalize_symbol(symbol)
        if key in self.mins:
            return self.mins[key], self.sources.get(key, "Alpaca min_order_size")
        if is_crypto_symbol(key):
            return float(settings.dust_threshold_crypto_min), "env DUST_THRESHOLD_CRYPTO_MIN"
        return float(settings.dust_threshold_stock_min), "env DUST_THRESHOLD_STOCK_MIN"

    def set_min(self, symbol: str, value: float, source: str) -> None:
        key = normalize_symbol(symbol)
        self.mins[key] = float(value)
        self.sources[key] = source


def _format_qty(value: float) -> str:
    return f"{float(value):g}"


def refresh_crypto_mins(
    client: AlpacaClient,
    settings: Settings,
    *,
    symbols: list[str] | None = None,
) -> None:
    """Consulta min_order_size en Alpaca y actualiza settings.dust_cache."""
    targets = symbols if symbols is not None else list(settings.crypto_symbols)
    cache = settings.dust_cache
    for sym in targets:
        key = normalize_symbol(sym)
        if not is_crypto_symbol(key):
            continue
        try:
            client.limiter.acquire("trading_read")
            asset = client.trading.get_asset(key)
            raw = getattr(asset, "min_order_size", None)
            if raw is not None:
                val = float(raw)
                if val > 0:
                    cache.set_min(key, val, "Alpaca min_order_size")
                    logger.info(
                        "Dust threshold %s: %s (fuente: Alpaca min_order_size)",
                        key,
                        _format_qty(val),
                    )
                    continue
            raise ValueError("min_order_size vacío o cero")
        except Exception as exc:
            fallback = float(settings.dust_threshold_crypto_min)
            cache.set_min(key, fallback, "env DUST_THRESHOLD_CRYPTO_MIN (fallback)")
            logger.warning(
                "Dust threshold %s: %s (fuente: env DUST_THRESHOLD_CRYPTO_MIN — Alpaca: %s)",
                key,
                _format_qty(fallback),
                type(exc).__name__,
            )
    cache.updated_at = time.time()


def dust_threshold_for(settings: Settings, symbol: str, reference_qty: float) -> float:
    """
    Umbral = max(piso operable, % de reference_qty, override por símbolo).
    Cripto: piso = Alpaca min_order_size (cache); .env solo si falla la consulta.
    """
    key = normalize_symbol(symbol)
    override = settings.dust_threshold_overrides.get(key)
    if override is not None:
        return float(override)
    ref = max(abs(float(reference_qty)), 0.0)
    pct_floor = float(settings.dust_threshold_pct) * ref
    floor, _ = settings.dust_cache.floor_for(settings, key)
    floor = max(floor, float(settings.dust_threshold_min_qty))
    return max(floor, pct_floor)


def is_dust_qty(settings: Settings, symbol: str, qty: float, reference_qty: float) -> bool:
    if qty <= 0:
        return True
    return abs(float(qty)) <= dust_threshold_for(settings, symbol, reference_qty) + 1e-12


def effective_qty(settings: Settings, symbol: str, qty: float, reference_qty: float | None = None) -> float:
    """Qty operable; 0 si es polvo (reconcile / mode_switch no la tratan como abierta)."""
    ref = abs(float(reference_qty if reference_qty is not None else qty))
    if is_dust_qty(settings, symbol, qty, ref):
        return 0.0
    return abs(float(qty))
