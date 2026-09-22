"""Squeeze de volatilidad: compresión de Bollinger y primera ruptura con volumen."""

from __future__ import annotations

import logging

import pandas as pd

from bot.config import Settings
from bot.market.assets import is_crypto_symbol
from bot.strategy.base import Signal
from bot.strategy.indicators import bollinger, volume_vs_average
from bot.strategy.regime_selector import _is_compressed

logger = logging.getLogger(__name__)


def detect_squeeze(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    has_long: bool,
    symbol: str = "",
) -> tuple[Signal, str]:
    if bars is None or bars.empty or "close" not in bars.columns:
        return Signal.HOLD, "squeeze sin barras"
    period = int(settings.bb_period)
    if len(bars) < period + int(settings.squeeze_width_lookback) + 2:
        return Signal.HOLD, "squeeze barras insuficientes"

    compressed, width, width_avg = _is_compressed(bars["close"], settings)
    if not compressed:
        return Signal.HOLD, "sin compresión BB"

    lower, mid, upper = bollinger(bars["close"], period, settings.bb_std)
    if pd.isna(lower.iloc[-1]) or pd.isna(upper.iloc[-1]):
        return Signal.HOLD, "Bollinger squeeze no listo"

    close = float(bars["close"].iloc[-1])
    band_lo = float(lower.iloc[-1])
    band_hi = float(upper.iloc[-1])
    vol_mult = float(settings.squeeze_volume_mult)
    if symbol and is_crypto_symbol(symbol):
        vol_mult = float(settings.crypto_squeeze_volume_mult)
    vol_ok, vol_ratio = volume_vs_average(
        bars, settings.volume_confirmation_period, vol_mult
    )
    vol_txt = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
    if vol_ok is False:
        return Signal.HOLD, f"squeeze sin volumen ({vol_txt} < {vol_mult:.2f}x)"

    if close > band_hi and not has_long:
        why = (
            f"squeeze BUY | close={close:.4f} > banda {band_hi:.4f} "
            f"width={width:.4f}/{width_avg:.4f} vol={vol_txt}"
        )
        logger.info("%s | %s", symbol or "?", why)
        return Signal.BUY, why
    if close < band_lo and has_long:
        why = (
            f"squeeze SELL | close={close:.4f} < banda {band_lo:.4f} "
            f"width={width:.4f}/{width_avg:.4f} vol={vol_txt}"
        )
        logger.info("%s | %s", symbol or "?", why)
        return Signal.SELL, why
    return Signal.HOLD, f"squeeze comprimido sin ruptura | close={close:.4f} mid={float(mid.iloc[-1]):.4f}"
