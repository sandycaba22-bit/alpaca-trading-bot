"""Pullback a EMA dentro de tendencia confirmada por SMA lenta."""

from __future__ import annotations

import logging

import pandas as pd

from bot.config import Settings
from bot.strategy.base import Signal
from bot.strategy.indicators import ema, sma, volume_vs_average

logger = logging.getLogger(__name__)


def detect_pullback(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    has_long: bool,
    slow_period: int,
    symbol: str = "",
) -> tuple[Signal, str]:
    if bars is None or bars.empty or "close" not in bars.columns:
        return Signal.HOLD, "pullback sin barras"
    ema_period = int(settings.pullback_ema_period)
    if len(bars) < max(slow_period, ema_period) + 3:
        return Signal.HOLD, "pullback barras insuficientes"

    closes = bars["close"].astype(float)
    slow = sma(closes, slow_period)
    fast = ema(closes, ema_period)
    if pd.isna(slow.iloc[-1]) or pd.isna(fast.iloc[-1]):
        return Signal.HOLD, "medias pullback no listas"

    close = float(closes.iloc[-1])
    open_ = float(bars["open"].iloc[-1])
    high = float(bars["high"].iloc[-1])
    low = float(bars["low"].iloc[-1])
    slow_px = float(slow.iloc[-1])
    ema_px = float(fast.iloc[-1])
    near = max(0.0, float(settings.pullback_ema_near_pct)) * ema_px
    touched = (low - near) <= ema_px <= (high + near)
    vol_ok, vol_ratio = volume_vs_average(
        bars, settings.volume_confirmation_period, settings.pullback_volume_mult
    )
    vol_txt = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"

    if close > slow_px and touched and close > open_ and not has_long:
        if vol_ok is False:
            return Signal.HOLD, f"pullback sin volumen ({vol_txt})"
        why = (
            f"pullback BUY | toca EMA{ema_period}={ema_px:.4f} "
            f"sobre SMA{slow_period}={slow_px:.4f} rebote vol={vol_txt}"
        )
        logger.info("%s | %s", symbol or "?", why)
        return Signal.BUY, why

    if close < slow_px and touched and close < open_ and has_long:
        if vol_ok is False:
            return Signal.HOLD, f"pullback bajista sin volumen ({vol_txt})"
        why = (
            f"pullback SELL | toca EMA{ema_period}={ema_px:.4f} "
            f"bajo SMA{slow_period}={slow_px:.4f} vol={vol_txt}"
        )
        logger.info("%s | %s", symbol or "?", why)
        return Signal.SELL, why

    return Signal.HOLD, f"pullback sin rebote | EMA={ema_px:.4f} SMA={slow_px:.4f}"
