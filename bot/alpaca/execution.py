"""Envío y consulta de órdenes en paper trading."""

from __future__ import annotations

import logging
from typing import Any

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from bot.alpaca.client import AlpacaClient
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, ValidationError
from bot.security.sanitize import sanitize_qty, sanitize_symbol

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, client: AlpacaClient, dry_run: bool = True) -> None:
        self.client = client
        self.dry_run = dry_run

    def get_position(self, symbol: str) -> Position | None:
        symbol = sanitize_symbol(symbol)
        try:
            self.client.limiter.acquire("trading_read")
            return self.client.trading.get_open_position(symbol)
        except ValidationError:
            raise
        except Exception:
            return None

    def list_positions(self) -> list[Position]:
        self.client.limiter.acquire("trading_read")
        return list(self.client.trading.get_all_positions())

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        safe_symbol = sanitize_symbol(symbol) if symbol else None
        self.client.limiter.acquire("trading_read")
        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[safe_symbol] if safe_symbol else None,
        )
        return list(self.client.trading.get_orders(filter=request))

    def submit_market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> Order | dict[str, Any]:
        symbol = sanitize_symbol(symbol)
        qty = sanitize_qty(qty)
        if side not in (OrderSide.BUY, OrderSide.SELL):
            raise ValidationError("Lado de orden no permitido")

        try:
            self.client.limiter.acquire("order")
        except RateLimitError:
            audit("order_submit", "deny", symbol=symbol, reason="rate_limit", dry_run=self.dry_run)
            raise

        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side.value,
            "type": "market",
            "time_in_force": time_in_force.value,
        }

        if self.dry_run:
            logger.info("DRY_RUN | orden no enviada | %s", payload)
            audit("order_submit", "dry_run", symbol=symbol, qty=qty, side=side.value)
            return {"dry_run": True, **payload}

        try:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=time_in_force,
            )
            order = self.client.trading.submit_order(order_data=request)
        except Exception as exc:
            log_caught(logger, "order_submit_failed", exc, symbol=symbol, side=side.value)
            audit("order_submit", "deny", symbol=symbol, qty=qty, side=side.value, reason="broker_error")
            raise RuntimeError("No se pudo enviar la orden.") from None

        logger.info(
            "Orden enviada | id=%s | %s %s qty=%s | status=%s",
            order.id,
            side.value,
            symbol,
            qty,
            order.status,
        )
        audit("order_submit", "allow", symbol=symbol, qty=qty, side=side.value, order_id=str(order.id))
        return order

    def close_position(self, symbol: str) -> Order | dict[str, Any] | None:
        symbol = sanitize_symbol(symbol)
        position = self.get_position(symbol)
        if position is None:
            logger.info("No hay posicion abierta en %s", symbol)
            audit("order_close", "deny", symbol=symbol, reason="no_position")
            return None

        qty = sanitize_qty(abs(float(position.qty)))
        side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
        logger.info("Cerrando posicion %s qty=%s side=%s", symbol, qty, side.value)
        audit("order_close", "allow", symbol=symbol, qty=qty, side=side.value)
        return self.submit_market_order(symbol, qty, side)
