"""Pullback de compra en tendencia 9m alcista.

No reutiliza mean-reversion profundo (RSI<=30 + banda inferior): eso casi
nunca ocurre en tendencia_fuerte. Aquí basta un dip a la EMA/media y un rebote.
"""

from __future__ import annotations

import logging

import pandas as pd

from bot.config import Settings
from bot.strategy.base import Signal
from bot.strategy.indicators import bollinger, ema, last_rsi
from bot.strategy.mean_reversion import mean_reversion_criteria
from bot.strategy.multi_tf_analysis import analyze_trend

logger = logging.getLogger(__name__)


def resolve_htf_trend(bars: pd.DataFrame, htf_trend: str | None) -> str:
    """Usa la tendencia 9m ya calculada; si no llega, el mismo analyze_trend del filtro."""
    raw = str(htf_trend or "").strip().lower()
    if raw in {"bull", "bear", "sideways"}:
        return raw
    trend, _ret = analyze_trend(bars)
    return trend


def _continuation_dip(
    bars: pd.DataFrame,
    *,
    settings: Settings,
) -> tuple[Signal, str]:
    if bars is None or bars.empty or "close" not in bars.columns:
        return Signal.HOLD, "trend-pullback sin barras"
    ema_period = int(settings.pullback_ema_period)
    bb_period = int(settings.bb_period)
    if len(bars) < max(ema_period, bb_period, int(settings.rsi_period)) + 2:
        return Signal.HOLD, "trend-pullback barras insuficientes"

    closes = bars["close"].astype(float)
    fast = ema(closes, ema_period)
    _lower, mid, _upper = bollinger(closes, bb_period, settings.bb_std)
    if pd.isna(fast.iloc[-1]) or pd.isna(mid.iloc[-1]):
        return Signal.HOLD, "trend-pullback medias no listas"

    rsi_value = last_rsi(closes, settings.rsi_period)
    if rsi_value is None:
        return Signal.HOLD, "trend-pullback RSI no disponible"

    close = float(closes.iloc[-1])
    open_ = float(bars["open"].iloc[-1])
    low = float(bars["low"].iloc[-1])
    ema_px = float(fast.iloc[-1])
    mid_px = float(mid.iloc[-1])
    near = max(0.0, float(settings.pullback_ema_near_pct)) * ema_px
    rsi_min = float(settings.trend_pullback_rsi_min)
    rsi_max = float(settings.trend_pullback_rsi_max)
    if rsi_max < rsi_min:
        rsi_min, rsi_max = rsi_max, rsi_min

    dipped = low <= ema_px + near or close <= ema_px + near or low <= mid_px
    bounce = close > open_
    rsi_ok = rsi_min <= rsi_value <= rsi_max
    trend_intact = close >= ema_px - (near * 2.0 if near > 0 else ema_px * 0.004)

    if dipped and bounce and rsi_ok and trend_intact:
        why = (
            f"trend-pullback BUY | dip EMA{ema_period}={ema_px:.4f} mid={mid_px:.4f} "
            f"close={close:.4f} RSI={rsi_value:.1f} [{rsi_min:.0f}-{rsi_max:.0f}]"
        )
        return Signal.BUY, why
    return Signal.HOLD, (
        f"trend-pullback sin dip/rebote | RSI={rsi_value:.1f} "
        f"EMA={ema_px:.4f} close={close:.4f}"
    )


def detect_trend_pullback(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    has_long: bool,
    symbol: str = "",
    htf_trend: str | None = None,
) -> tuple[Signal, str]:
    trend = resolve_htf_trend(bars, htf_trend)
    if trend != "bull":
        return Signal.HOLD, f"trend-pullback inactivo | tendencia 9m={trend}"
    if has_long:
        return Signal.HOLD, "trend-pullback omitido — ya hay long"

    raw, detail = _continuation_dip(bars, settings=settings)
    if raw is Signal.BUY:
        logger.info("%s | %s | tendencia 9m alcista", symbol or "?", detail)
        return Signal.BUY, detail

    deep, deep_detail = mean_reversion_criteria(bars, settings=settings, has_long=has_long)
    if deep is Signal.BUY:
        why = deep_detail.replace("mean-rev BUY", "trend-pullback BUY", 1)
        logger.info("%s | %s | tendencia 9m alcista (dip profundo)", symbol or "?", why)
        return Signal.BUY, why

    return Signal.HOLD, f"trend-pullback sin corrección | {detail}"
