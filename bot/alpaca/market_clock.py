"""Vista del reloj NYSE tolerante a fallos (modo cripto sin bloquear)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alpaca.trading.models import Clock


@dataclass(frozen=True)
class MarketClockView:
    is_open: bool
    next_open: Any | None = None
    next_close: Any | None = None
    stock_feed_ok: bool = True


def from_alpaca_clock(clock: Clock) -> MarketClockView:
    return MarketClockView(
        is_open=bool(clock.is_open),
        next_open=clock.next_open,
        next_close=clock.next_close,
        stock_feed_ok=True,
    )


def crypto_fallback_clock() -> MarketClockView:
    """Mercado de acciones no consultable — operar cripto 24/7."""
    return MarketClockView(is_open=False, stock_feed_ok=False)
