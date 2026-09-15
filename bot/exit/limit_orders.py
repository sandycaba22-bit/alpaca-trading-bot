"""Órdenes límite resting (TP dinámico) vía cliente Alpaca — no usa submit_smart_order."""

from __future__ import annotations

import logging
from typing import Any

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from bot.alpaca.client import AlpacaClient
from bot.alpaca.execution import _round_limit_price, alpaca_reject_detail, classify_order
from bot.market.assets import is_crypto_symbol
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError
from bot.security.sanitize import sanitize_qty, sanitize_symbol

logger = logging.getLogger(__name__)


def submit_resting_limit_sell(
    client: AlpacaClient,
    *,
    symbol: str,
    qty: float,
    limit_price: float,
    dry_run: bool,
    reason: str = "dynamic_tp_limit",
) -> tuple[str | None, str]:
    """Coloca una orden límite de venta resting (GTC cripto / DAY acciones)."""
    symbol = sanitize_symbol(symbol)
    qty = sanitize_qty(qty, fractional=is_crypto_symbol(symbol), mode="floor")
    px = _round_limit_price(float(limit_price), symbol)
    if qty <= 0 or px <= 0:
        return None, "qty o precio inválido"

    tif = TimeInForce.GTC if is_crypto_symbol(symbol) else TimeInForce.DAY
    if dry_run:
        logger.info(
            "%s | DRY_RUN limit sell qty=%s @ %.4f TIF=%s | %s",
            symbol,
            qty,
            px,
            tif.value,
            reason,
        )
        audit("dynamic_tp_limit", "dry_run", symbol=symbol, qty=qty, limit_price=px)
        return "dry_run_dynamic_tp", "dry_run"

    try:
        client.limiter.acquire("order")
    except RateLimitError as exc:
        return None, alpaca_reject_detail(exc)

    try:
        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=tif,
            limit_price=px,
        )
        order = client.trading.submit_order(order_data=request)
        order_id = str(getattr(order, "id", "") or "")
        logger.info(
            "%s | limit TP resting | id=%s qty=%s @ %.4f TIF=%s",
            symbol,
            order_id or "—",
            qty,
            px,
            tif.value,
        )
        audit("dynamic_tp_limit", "allow", symbol=symbol, order_id=order_id, limit_price=px)
        return order_id or None, "accepted"
    except Exception as exc:
        detail = alpaca_reject_detail(exc)
        log_caught(logger, "dynamic_tp_limit_submit_failed", exc, symbol=symbol)
        audit("dynamic_tp_limit", "deny", symbol=symbol, reason=detail[:180])
        return None, detail


def cancel_order(client: AlpacaClient, order_id: str, *, symbol: str = "") -> bool:
    if not order_id or order_id.startswith("dry_run"):
        return True
    try:
        client.limiter.acquire("order")
        client.trading.cancel_order_by_id(order_id)
        logger.info("%s | limit TP cancelada | id=%s", symbol or "—", order_id)
        audit("dynamic_tp_limit_cancel", "allow", symbol=symbol, order_id=order_id)
        return True
    except Exception as exc:
        log_caught(logger, "dynamic_tp_limit_cancel_failed", exc, symbol=symbol)
        return False


def fetch_order(client: AlpacaClient, order_id: str) -> Any | None:
    if not order_id or order_id.startswith("dry_run"):
        return None
    try:
        client.limiter.acquire("trading_read")
        return client.trading.get_order_by_id(order_id)
    except Exception as exc:
        log_caught(logger, "dynamic_tp_order_lookup_failed", exc)
        return None


def order_fill_snapshot(order: Any) -> tuple[str, float, float]:
    """Retorna (kind, filled_qty, fill_price) usando classify_order del executor."""
    kind = classify_order(order)
    filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
    return kind, filled_qty, fill_price
