"""Órdenes aceptadas por el broker que todavía no tienen fill confirmado."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.market.assets import normalize_symbol
from bot.runtime_paths import data_file

logger = logging.getLogger(__name__)


def _order_key(symbol: str) -> str:
    return normalize_symbol(symbol)


@dataclass
class RestingOrder:
    order_id: str
    symbol: str
    side: str
    qty: float
    reason: str
    is_close: bool
    event_id: str
    submitted_at: str
    filled_qty: float = 0.0
    entry_price: float | None = None
    signal_price: float = 0.0
    order_type: str = "limit"
    limit_price: float | None = None
    last_status: str = "accepted"
    stop_price: float | None = None
    take_profit_price: float | None = None
    stop_pct: float | None = None
    take_profit_pct: float | None = None
    dust_qty: float = 0.0


class PendingOrderBook:
    """Registro persistente de órdenes en vuelo (limit DAY/GTC sin fill)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_file("pending_orders.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._orders: dict[str, RestingOrder] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("orders", []) if isinstance(raw, dict) else raw
            if not isinstance(rows, list):
                return
            loaded: dict[str, RestingOrder] = {}
            for data in rows:
                if not isinstance(data, dict) or not data.get("order_id"):
                    continue
                order_id = str(data["order_id"])
                loaded[order_id] = RestingOrder(
                    order_id=order_id,
                    symbol=_order_key(str(data.get("symbol", ""))),
                    side=str(data.get("side", "sell")).lower(),
                    qty=float(data.get("qty", 0.0) or 0.0),
                    reason=str(data.get("reason", "close")),
                    is_close=bool(data.get("is_close", True)),
                    event_id=str(data.get("event_id", "")),
                    submitted_at=str(data.get("submitted_at", "")),
                    filled_qty=float(data.get("filled_qty", 0.0) or 0.0),
                    entry_price=(
                        float(data["entry_price"])
                        if data.get("entry_price") not in (None, "")
                        else None
                    ),
                    signal_price=float(data.get("signal_price", 0.0) or 0.0),
                    order_type=str(data.get("order_type", "limit")),
                    limit_price=(
                        float(data["limit_price"])
                        if data.get("limit_price") not in (None, "")
                        else None
                    ),
                    last_status=str(data.get("last_status", "accepted")),
                    stop_price=(
                        float(data["stop_price"])
                        if data.get("stop_price") not in (None, "")
                        else None
                    ),
                    take_profit_price=(
                        float(data["take_profit_price"])
                        if data.get("take_profit_price") not in (None, "")
                        else None
                    ),
                    stop_pct=(
                        float(data["stop_pct"])
                        if data.get("stop_pct") not in (None, "")
                        else None
                    ),
                    take_profit_pct=(
                        float(data["take_profit_pct"])
                        if data.get("take_profit_pct") not in (None, "")
                        else None
                    ),
                    dust_qty=float(data.get("dust_qty", 0.0) or 0.0),
                )
            self._orders = loaded
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            logger.warning("No se pudo cargar órdenes en vuelo: %s", type(exc).__name__)
            self._orders = {}

    def save(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "orders": [asdict(order) for order in self._orders.values()],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def list(self) -> list[RestingOrder]:
        with self._lock:
            return list(self._orders.values())

    def get(self, order_id: str) -> RestingOrder | None:
        with self._lock:
            return self._orders.get(str(order_id))

    def upsert(self, order: RestingOrder) -> RestingOrder:
        with self._lock:
            order.symbol = _order_key(order.symbol)
            current = self._orders.get(order.order_id)
            if current is not None:
                if not order.reason or order.reason == "close":
                    order.reason = current.reason
                if not order.event_id:
                    order.event_id = current.event_id
                if order.entry_price is None:
                    order.entry_price = current.entry_price
                if current.signal_price and not order.signal_price:
                    order.signal_price = current.signal_price
            self._orders[order.order_id] = order
            self.save()
        return order

    def remove(self, order_id: str) -> RestingOrder | None:
        with self._lock:
            gone = self._orders.pop(str(order_id), None)
            if gone is not None:
                self.save()
            return gone

    def has_close(self, symbol: str) -> bool:
        key = _order_key(symbol)
        with self._lock:
            return any(
                _order_key(row.symbol) == key and row.is_close for row in self._orders.values()
            )

    def has_open(self, symbol: str) -> bool:
        key = _order_key(symbol)
        with self._lock:
            return any(
                _order_key(row.symbol) == key and not row.is_close
                for row in self._orders.values()
            )

    def close_for(self, symbol: str) -> RestingOrder | None:
        key = _order_key(symbol)
        with self._lock:
            for row in self._orders.values():
                if _order_key(row.symbol) == key and row.is_close:
                    return row
        return None
