"""Reversión a la media: bandas de Bollinger + RSI, solo en régimen lateral."""

from __future__ import annotations

import logging

import pandas as pd

from bot.config import Settings
from bot.strategy.base import Signal
from bot.strategy.indicators import bollinger, last_rsi

logger = logging.getLogger(__name__)


def mean_reversion_criteria(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    has_long: bool,
) -> tuple[Signal, str]:
    """Criterio RSI + toque de banda/media. Sin logs (reutilizado por otras estrategias)."""
    if bars is None or bars.empty or "close" not in bars.columns:
        return Signal.HOLD, "mean-rev sin barras"
    period = int(settings.bb_period)
    if len(bars) < period + int(settings.rsi_period) + 2:
        return Signal.HOLD, "mean-rev barras insuficientes"

    closes = bars["close"].astype(float)
    lower, mid, upper = bollinger(closes, period, settings.bb_std)
    if pd.isna(lower.iloc[-1]) or pd.isna(upper.iloc[-1]):
        return Signal.HOLD, "Bollinger no listo"
    low = float(bars["low"].iloc[-1])
    high = float(bars["high"].iloc[-1])
    close = float(closes.iloc[-1])
    band_lo = float(lower.iloc[-1])
    band_hi = float(upper.iloc[-1])
    rsi_value = last_rsi(closes, settings.rsi_period)
    if rsi_value is None:
        return Signal.HOLD, "RSI no disponible"

    touched_low = low <= band_lo
    touched_high = high >= band_hi
    if touched_low and rsi_value <= settings.rsi_oversold and not has_long:
        why = (
            f"mean-rev BUY | toca banda inf {band_lo:.4f} close={close:.4f} "
            f"RSI={rsi_value:.1f}<={settings.rsi_oversold:.0f}"
        )
        return Signal.BUY, why
    if touched_high and rsi_value >= settings.rsi_overbought and has_long:
        why = (
            f"mean-rev SELL | toca banda sup {band_hi:.4f} close={close:.4f} "
            f"RSI={rsi_value:.1f}>={settings.rsi_overbought:.0f}"
        )
        return Signal.SELL, why
    return Signal.HOLD, (
        f"mean-rev sin toque | RSI={rsi_value:.1f} mid={float(mid.iloc[-1]):.4f}"
    )


def detect_mean_reversion(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    has_long: bool,
    symbol: str = "",
) -> tuple[Signal, str]:
    raw, why = mean_reversion_criteria(bars, settings=settings, has_long=has_long)
    if raw is not Signal.HOLD:
        logger.info("%s | %s", symbol or "?", why)
    return raw, why
