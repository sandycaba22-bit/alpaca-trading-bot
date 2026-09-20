"""Ejecución maker-first para cripto en Alpaca (post-only style via GTC limit en bid/ask).

Alpaca Crypto diferencia maker vs taker (tier 1: 0.15% / 0.25% por lado).
Docs: https://docs.alpaca.markets/docs/crypto-fees

- Cripto admite TIF `gtc` e `ioc` (no DAY/FOK).
- Limit GTC en bid (compra) o ask (venta) sin cruzar el spread → maker si descansa en book.
- Limit marketable (cruza spread) o market → taker.
- Fee real: Activities API `CFEE`/`FEE` (posting EOD; no siempre mismo día).

Este módulo no altera smart-order de acciones ni la lógica de señales.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from alpaca.trading.enums import OrderSide, TimeInForce

from bot.alpaca.execution import AssetRules, OrderSubmitResult, normalize_limit_price_for_asset
from bot.market.assets import is_crypto_symbol

logger = logging.getLogger(__name__)

# Tier 1 Alpaca Crypto (volumen 30d < 100k USD)
DEFAULT_CRYPTO_MAKER_FEE_PCT = 0.15
DEFAULT_CRYPTO_TAKER_FEE_PCT = 0.25
DEFAULT_CRYPTO_MAKER_SLIPPAGE_PCT = 0.01
DEFAULT_CRYPTO_TAKER_SLIPPAGE_PCT = 0.03

CryptoMakerFallback = Literal["taker", "retry", "cancel"]


@dataclass(frozen=True)
class CryptoMakerConfig:
    enabled: bool = False
    timeout_seconds: int = 45
    fallback: CryptoMakerFallback = "taker"
    max_retries: int = 1
    tick_inside: int = 0
    poll_interval_seconds: float = 2.0
    maker_fee_pct: float = DEFAULT_CRYPTO_MAKER_FEE_PCT
    taker_fee_pct: float = DEFAULT_CRYPTO_TAKER_FEE_PCT

    @classmethod
    def from_settings(cls, settings: Any) -> CryptoMakerConfig:
        raw_fb = str(getattr(settings, "crypto_maker_fallback", "taker") or "taker").lower()
        fallback: CryptoMakerFallback = raw_fb if raw_fb in {"taker", "retry", "cancel"} else "taker"
        return cls(
            enabled=bool(getattr(settings, "crypto_maker_first_enabled", False)),
            timeout_seconds=int(getattr(settings, "crypto_maker_timeout_seconds", 45)),
            fallback=fallback,
            max_retries=int(getattr(settings, "crypto_maker_max_retries", 1)),
            tick_inside=int(getattr(settings, "crypto_maker_tick_inside", 0)),
            poll_interval_seconds=float(getattr(settings, "crypto_maker_poll_seconds", 2.0)),
            maker_fee_pct=float(getattr(settings, "crypto_maker_fee_pct", DEFAULT_CRYPTO_MAKER_FEE_PCT)),
            taker_fee_pct=float(getattr(settings, "crypto_maker_taker_fee_pct", DEFAULT_CRYPTO_TAKER_FEE_PCT)),
        )


def price_tick(price: float, rules: AssetRules | None) -> float:
    inc = getattr(rules, "price_increment", None) if rules else None
    if inc and float(inc) > 0:
        return float(inc)
    if price >= 1000:
        return 0.01
    if price >= 1:
        return 0.0001
    return 0.000001


def choose_maker_limit_price(
    side: OrderSide,
    bid: float | None,
    ask: float | None,
    symbol: str,
    rules: AssetRules | None,
    *,
    tick_inside: int = 0,
) -> float:
    """Precio límite post-only: compra en bid (+ticks), venta en ask (-ticks), sin cruzar spread."""
    b = float(bid or 0.0)
    a = float(ask or 0.0)
    if b <= 0 or a <= 0 or a <= b:
        mid = (b + a) / 2.0 if b > 0 and a > 0 else max(b, a)
        return normalize_limit_price_for_asset(mid, symbol, rules)

    tick = price_tick((b + a) / 2.0, rules)
    inside = max(0, int(tick_inside)) * tick

    if side is OrderSide.BUY:
        px = b + inside
        if px >= a:
            px = max(b, a - tick)
    else:
        px = a - inside
        if px <= b:
            px = min(a, b + tick)

    return normalize_limit_price_for_asset(px, symbol, rules)


def estimate_liquidity_role(
    side: OrderSide,
    limit_price: float,
    bid: float | None,
    ask: float | None,
) -> str:
    """Heurística maker/taker según precio límite vs book (Alpaca no devuelve rol en la orden)."""
    b = float(bid or 0.0)
    a = float(ask or 0.0)
    if limit_price <= 0 or b <= 0 or a <= 0:
        return "unknown"
    if side is OrderSide.BUY:
        if limit_price >= a:
            return "taker"
        if limit_price <= b:
            return "maker"
        return "mixed"
    if limit_price <= b:
        return "taker"
    if limit_price >= a:
        return "maker"
    return "mixed"


def estimated_fee_pct(role: str, config: CryptoMakerConfig) -> float:
    if role == "maker":
        return config.maker_fee_pct
    if role == "taker":
        return config.taker_fee_pct
    return (config.maker_fee_pct + config.taker_fee_pct) / 2.0


def log_crypto_execution(
    symbol: str,
    side: OrderSide,
    *,
    order_id: str,
    limit_price: float | None,
    fill_price: float,
    filled_qty: float,
    role: str,
    fee_pct: float,
    tif: str,
    attempt: int,
    reason: str,
) -> None:
    logger.info(
        "%s | cripto maker-first | %s qty=%s | id=%s | limit=%s fill=%.4f | "
        "rol=%s fee_est=%.2f%%/lado tif=%s intento=%s | %s",
        symbol,
        side.value,
        f"{filled_qty:g}",
        order_id or "—",
        f"{limit_price:.4f}" if limit_price else "—",
        fill_price,
        role,
        fee_pct,
        tif,
        attempt,
        reason,
    )


def round_trip_cost_pct(fee_pct: float, slippage_pct: float) -> float:
    return 2.0 * (float(fee_pct) + float(slippage_pct)) / 100.0
