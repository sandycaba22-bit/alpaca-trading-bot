"""Libro de posiciones abiertas (memoria + disco) para dry-run y protecciones SL/TP."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.config import PROJECT_ROOT
from bot.market.assets import asset_class_for

logger = logging.getLogger(__name__)

POSITIONS_PATH = PROJECT_ROOT / "data" / "open_positions.json"


@dataclass
class TrackedPosition:
    """Posición abierta con niveles de salida registrados al comprar."""

    symbol: str
    qty: float
    avg_entry_price: float
    stop_price: float
    take_profit_price: float
    stop_pct: float
    take_profit_pct: float
    dry_run: bool
    asset_class: str
    opened_at: str
    current_price: float = 0.0
    trailing_notified: bool = False
    breakeven_notified: bool = False
    opened_qty: float = 0.0

    @property
    def side(self) -> str:
        return "long" if self.qty >= 0 else "short"


class OpenPositionBook:
    """
    Estado de posiciones abiertas.

    En dry-run es la fuente de verdad (Alpaca no recibe la orden).
    En live complementa al broker con niveles SL/TP para alertas.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or POSITIONS_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._positions: dict[str, TrackedPosition] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("positions", {})
            if not isinstance(rows, dict):
                return
            for symbol, data in rows.items():
                if not isinstance(data, dict):
                    continue
                self._positions[str(symbol).upper()] = TrackedPosition(
                    symbol=str(data.get("symbol", symbol)).upper(),
                    qty=float(data["qty"]),
                    avg_entry_price=float(data["avg_entry_price"]),
                    stop_price=float(data.get("stop_price", 0.0)),
                    take_profit_price=float(data.get("take_profit_price", 0.0)),
                    stop_pct=float(data.get("stop_pct", 0.0)),
                    take_profit_pct=float(data.get("take_profit_pct", 0.0)),
                    dry_run=bool(data.get("dry_run", True)),
                    asset_class=str(data.get("asset_class", asset_class_for(str(symbol)))),
                    opened_at=str(data.get("opened_at", "")),
                    current_price=float(data.get("current_price", 0.0)),
                    trailing_notified=bool(data.get("trailing_notified", False)),
                    breakeven_notified=bool(data.get("breakeven_notified", False)),
                    opened_qty=float(data.get("opened_qty", data.get("qty", 0.0)) or 0.0),
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("No se pudo cargar libro de posiciones: %s", type(exc).__name__)

    def save(self) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "positions": {sym: asdict(pos) for sym, pos in self._positions.items()},
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, symbol: str) -> TrackedPosition | None:
        return self._positions.get(str(symbol).upper())

    def list(self) -> list[TrackedPosition]:
        return list(self._positions.values())

    def has(self, symbol: str) -> bool:
        return str(symbol).upper() in self._positions

    def open(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        *,
        stop_price: float,
        take_profit_price: float,
        stop_pct: float,
        take_profit_pct: float,
        dry_run: bool,
    ) -> TrackedPosition:
        key = str(symbol).upper()
        pos = TrackedPosition(
            symbol=key,
            qty=float(qty),
            avg_entry_price=float(entry_price),
            stop_price=float(stop_price),
            take_profit_price=float(take_profit_price),
            stop_pct=float(stop_pct),
            take_profit_pct=float(take_profit_pct),
            dry_run=dry_run,
            asset_class=asset_class_for(key),
            opened_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            current_price=float(entry_price),
            opened_qty=float(qty),
        )
        self._positions[key] = pos
        self.save()
        logger.info(
            "Posicion abierta | %s qty=%s entry=%.4f SL=%.4f (%.2f%%) TP=%.4f (%.2f%%)",
            key,
            qty,
            entry_price,
            stop_price,
            stop_pct * 100,
            take_profit_price,
            take_profit_pct * 100,
        )
        return pos

    def update_mark(self, symbol: str, last_price: float) -> None:
        pos = self.get(symbol)
        if pos is None:
            return
        pos.current_price = float(last_price)
        self.save()

    def close(self, symbol: str) -> TrackedPosition | None:
        key = str(symbol).upper()
        pos = self._positions.pop(key, None)
        if pos is not None:
            self.save()
            logger.info("Posicion cerrada en libro | %s", key)
        return pos

    def add_fill(self, symbol: str, qty: float, fill_price: float) -> TrackedPosition | None:
        pos = self.get(symbol)
        if pos is None or qty <= 0 or fill_price <= 0:
            return pos
        old_qty = abs(float(pos.qty))
        new_qty = old_qty + abs(float(qty))
        pos.avg_entry_price = (
            (pos.avg_entry_price * old_qty) + (float(fill_price) * abs(float(qty)))
        ) / new_qty
        pos.qty = new_qty if pos.qty >= 0 else -new_qty
        pos.opened_qty = abs(float(pos.opened_qty or old_qty)) + abs(float(qty))
        self.save()
        return pos

    def apply_sell_fill(
        self,
        symbol: str,
        qty: float,
        *,
        dust_threshold: float = 0.0,
    ) -> tuple[TrackedPosition | None, float, float]:
        """
        Resta el fill de venta del libro.

        Returns:
            (posición restante o None si cerrada, qty vendida, qty polvo absorbida)
        """
        pos = self.get(symbol)
        if pos is None:
            return None, 0.0, 0.0
        book_qty = abs(float(pos.qty))
        sold = min(book_qty, abs(float(qty)))
        if sold <= 1e-8:
            return pos, 0.0, 0.0
        remaining = book_qty - sold
        dust_absorbed = 0.0
        if remaining <= 1e-8:
            return self.close(symbol), sold, 0.0
        if dust_threshold > 0 and remaining <= dust_threshold + 1e-12:
            dust_absorbed = remaining
            logger.info(
                "Posicion cerrada (polvo) | %s vendido=%s residuo=%s <= umbral=%s",
                pos.symbol,
                sold,
                remaining,
                dust_threshold,
            )
            return self.close(symbol), sold, dust_absorbed
        pos.qty = remaining if pos.qty >= 0 else -remaining
        self.save()
        logger.info("Posicion reducida en libro | %s vendido=%s restante=%s", pos.symbol, sold, pos.qty)
        return pos, sold, 0.0

    def drop_dry_run_rows(self) -> list[str]:
        """Quita filas dry-run cuando el bot opera en paper/live (no son posiciones reales)."""
        gone = [sym for sym, pos in list(self._positions.items()) if pos.dry_run]
        for sym in gone:
            self._positions.pop(sym, None)
        if gone:
            self.save()
            logger.info("Libro: filas dry-run omitidas en modo live | %s", ",".join(gone))
        return gone

    def symbols_by_asset_class(self, asset_class: str) -> list[str]:
        return [p.symbol for p in self._positions.values() if p.asset_class == asset_class]
