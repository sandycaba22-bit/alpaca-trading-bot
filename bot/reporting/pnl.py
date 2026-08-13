"""Cálculo y presentación de P&L por operación (verde / rojo)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class PnLEvent(str, Enum):
    OPENED = "opened"
    UPDATED = "updated"
    CLOSED = "closed"


@dataclass(frozen=True)
class PnLSnapshot:
    """Estado de rendimiento de una operación en un instante."""

    symbol: str
    event: PnLEvent
    qty: float
    side: str
    entry_price: float
    current_price: float
    invested: float
    pnl_abs: float
    pnl_pct: float
    in_profit: bool
    timestamp: datetime

    @property
    def color_name(self) -> str:
        if self.pnl_abs == 0:
            return "NEUTRO"
        return "VERDE" if self.in_profit else "ROJO"


def compute_pnl(
    symbol: str,
    qty: float,
    entry_price: float,
    current_price: float,
    event: PnLEvent,
    timestamp: datetime | None = None,
) -> PnLSnapshot:
    """P&L neto y % respecto al capital invertido en esa operación."""
    if entry_price <= 0:
        raise ValueError("entry_price debe ser > 0")
    invested = abs(qty) * entry_price
    if qty >= 0:
        pnl_abs = (current_price - entry_price) * qty
    else:
        pnl_abs = (entry_price - current_price) * abs(qty)
    pnl_pct = (pnl_abs / invested * 100.0) if invested else 0.0
    return PnLSnapshot(
        symbol=symbol.upper(),
        event=event,
        qty=qty,
        side="long" if qty >= 0 else "short",
        entry_price=entry_price,
        current_price=current_price,
        invested=invested,
        pnl_abs=pnl_abs,
        pnl_pct=pnl_pct,
        in_profit=pnl_abs > 0,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def format_snapshot(snap: PnLSnapshot) -> str:
    sign = "+" if snap.pnl_abs >= 0 else ""
    event_es = {
        PnLEvent.OPENED: "ABIERTA",
        PnLEvent.UPDATED: "ACTUALIZADA",
        PnLEvent.CLOSED: "CERRADA",
    }[snap.event]
    return (
        f"P&L [{snap.color_name}] {snap.symbol} | {event_es} | {snap.side} "
        f"qty={snap.qty:g} | neto {sign}${snap.pnl_abs:,.2f} | "
        f"{sign}{snap.pnl_pct:.2f}% | invertido ${snap.invested:,.2f} | "
        f"entry={snap.entry_price:.4f} last={snap.current_price:.4f}"
    )


class PerformanceReporter:
    """Emite un reporte cada vez que una operación se abre, marca o cierra."""

    def emit(self, snap: PnLSnapshot) -> PnLSnapshot:
        logger.info(format_snapshot(snap))
        return snap
