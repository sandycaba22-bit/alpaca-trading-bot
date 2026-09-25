"""Filtro vol_2x en vela de señal (acciones) — alineado con sweep/backtest."""

from __future__ import annotations

import pandas as pd

from bot.config import Settings
from bot.strategy.indicators import volume_vs_average
from bot.strategy.sync_entry import closed_bars_only


def check_stock_entry_signal_volume(
    bars: pd.DataFrame,
    *,
    entry_tf: str,
    settings: Settings,
) -> tuple[bool, str]:
    """Volumen de la última vela cerrada >= mult × media (period velas previas)."""
    mult = float(settings.stock_entry_signal_volume_mult)
    period = int(settings.stock_entry_volume_period)
    if mult <= 0:
        return True, ""
    closed = closed_bars_only(bars, entry_tf)
    if closed is None or closed.empty or len(closed) < period + 1:
        return False, f"vol_2x sin histórico ({len(closed) if closed is not None else 0} velas)"
    vol_ok, ratio = volume_vs_average(closed, period, mult)
    if vol_ok is True:
        return True, f"vol_2x OK ({ratio:.2f}x >= {mult:.2f}x)"
    if vol_ok is False:
        return False, f"vol_2x rechazado ({ratio:.2f}x < {mult:.2f}x)" if ratio is not None else "vol_2x rechazado"
    return False, "vol_2x sin datos de volumen"
