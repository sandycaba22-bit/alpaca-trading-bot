"""Entradas en tendencia 9m alcista: dip a EMA, continuación en máximos y micro-HH.

No compra el régimen a ciegas. En tendencia_fuerte + HTF bull exige:
- dip/rebote a la EMA, o
- close cerca del techo del rango, o
- higher high reciente en el tercio superior.
"""

from __future__ import annotations

import logging

import pandas as pd

from bot.config import Settings
from bot.market.assets import is_crypto_symbol
from bot.strategy.base import Signal
from bot.strategy.indicators import bollinger, ema, last_rsi, sma
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


def _last_rsi_ok(
    rsi_value: float,
    settings: Settings,
    symbol: str | None = None,
) -> tuple[bool, float, float]:
    rsi_min = float(settings.trend_pullback_rsi_min)
    rsi_max = float(settings.trend_pullback_rsi_max)
    if symbol and is_crypto_symbol(symbol):
        rsi_max = max(rsi_max, float(settings.crypto_trend_pullback_rsi_max))
    if rsi_max < rsi_min:
        rsi_min, rsi_max = rsi_max, rsi_min
    return rsi_min <= rsi_value <= rsi_max, rsi_min, rsi_max


def _range_context(bars: pd.DataFrame, lookback: int) -> tuple[float, float, float, float]:
    """high/low del rango previo, fracción del close en el rango, distancia al techo."""
    prior = bars.iloc[-(lookback + 1) : -1]
    range_high = float(prior["high"].max())
    range_low = float(prior["low"].min())
    close = float(bars["close"].iloc[-1])
    width = range_high - range_low
    frac = (close - range_low) / width if width > 0 else 0.0
    dist_high = (range_high - close) / range_high if range_high > 0 else 1.0
    return range_high, range_low, frac, dist_high


def _continuation_dip(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    sma_px: float | None,
    symbol: str = "",
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
    rsi_ok, rsi_min, rsi_max = _last_rsi_ok(rsi_value, settings, symbol)

    dipped = low <= ema_px + near or close <= ema_px + near or low <= mid_px
    bounce = close > open_
    trend_intact = close >= ema_px - (near * 2.0 if near > 0 else ema_px * 0.004)
    above_sma = sma_px is None or close > sma_px

    if dipped and bounce and rsi_ok and trend_intact and above_sma:
        why = (
            f"trend-pullback BUY | dip EMA{ema_period}={ema_px:.4f} mid={mid_px:.4f} "
            f"close={close:.4f} RSI={rsi_value:.1f} [{rsi_min:.0f}-{rsi_max:.0f}]"
        )
        return Signal.BUY, why
    return Signal.HOLD, (
        f"trend-pullback sin dip/rebote | RSI={rsi_value:.1f} "
        f"EMA={ema_px:.4f} close={close:.4f}"
    )


def _near_high_continuation(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    sma_px: float | None,
    symbol: str = "",
) -> tuple[Signal, str]:
    lookback = int(settings.trend_range_lookback)
    if bars is None or bars.empty or len(bars) < lookback + 2:
        return Signal.HOLD, "trend-cont techo: barras insuficientes"
    closes = bars["close"].astype(float)
    ema_px = float(ema(closes, int(settings.pullback_ema_period)).iloc[-1])
    if pd.isna(ema_px) or ema_px <= 0:
        return Signal.HOLD, "trend-cont techo: EMA no lista"
    rsi_value = last_rsi(closes, settings.rsi_period)
    if rsi_value is None:
        return Signal.HOLD, "trend-cont techo: RSI no disponible"
    rsi_ok, rsi_min, rsi_max = _last_rsi_ok(rsi_value, settings, symbol)
    close = float(closes.iloc[-1])
    open_ = float(bars["open"].iloc[-1])
    range_high, _range_low, _frac, dist_high = _range_context(bars, lookback)
    near_high = float(settings.trend_near_high_pct)
    not_red = close >= open_
    above = close >= ema_px and (sma_px is None or close > sma_px)
    if dist_high <= near_high and not_red and above and rsi_ok:
        why = (
            f"trend-cont BUY | cerca techo {range_high:.4f} dist={dist_high:.2%} "
            f"close={close:.4f} EMA={ema_px:.4f} RSI={rsi_value:.1f} [{rsi_min:.0f}-{rsi_max:.0f}]"
        )
        return Signal.BUY, why
    reasons = []
    if dist_high > near_high:
        reasons.append(f"dist={dist_high:.2%} > {near_high:.2%}")
    if not not_red:
        reasons.append("vela roja")
    if not above:
        reasons.append("bajo EMA/SMA")
    if not rsi_ok:
        reasons.append(f"RSI={rsi_value:.1f} fuera [{rsi_min:.0f}-{rsi_max:.0f}]")
    return Signal.HOLD, (
        f"trend-cont techo no | high={range_high:.4f} close={close:.4f} | " + ", ".join(reasons)
    )


def _micro_higher_high(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    sma_px: float | None,
    symbol: str = "",
) -> tuple[Signal, str]:
    lookback = max(2, int(settings.trend_micro_lookback))
    range_lb = int(settings.trend_range_lookback)
    if bars is None or bars.empty or len(bars) < max(lookback, range_lb) + 2:
        return Signal.HOLD, "micro-HH barras insuficientes"
    closes = bars["close"].astype(float)
    ema_px = float(ema(closes, int(settings.pullback_ema_period)).iloc[-1])
    if pd.isna(ema_px) or ema_px <= 0:
        return Signal.HOLD, "micro-HH EMA no lista"
    rsi_value = last_rsi(closes, settings.rsi_period)
    if rsi_value is None:
        return Signal.HOLD, "micro-HH RSI no disponible"
    rsi_ok, rsi_min, rsi_max = _last_rsi_ok(rsi_value, settings, symbol)
    close = float(closes.iloc[-1])
    open_ = float(bars["open"].iloc[-1])
    prev_high = float(bars["high"].iloc[-2])
    _range_high, _range_low, frac, _dist = _range_context(bars, range_lb)
    upper = float(settings.trend_upper_frac)
    hh = close > prev_high
    not_red = close >= open_
    above = close >= ema_px and (sma_px is None or close > sma_px)
    if hh and frac >= upper and not_red and above and rsi_ok:
        why = (
            f"micro-HH BUY | close={close:.4f} > prev_high={prev_high:.4f} "
            f"frac={frac:.2f} EMA={ema_px:.4f} RSI={rsi_value:.1f} [{rsi_min:.0f}-{rsi_max:.0f}]"
        )
        return Signal.BUY, why
    return Signal.HOLD, (
        f"micro-HH no | close={close:.4f} prev_high={prev_high:.4f} "
        f"frac={frac:.2f} (min {upper:.2f})"
    )


def detect_trend_pullback(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    has_long: bool,
    symbol: str = "",
    htf_trend: str | None = None,
    slow_period: int | None = None,
) -> tuple[Signal, str]:
    trend = resolve_htf_trend(bars, htf_trend)
    if trend != "bull":
        return Signal.HOLD, f"trend-pullback inactivo | tendencia 9m={trend}"
    if has_long:
        return Signal.HOLD, "trend-pullback omitido — ya hay long"

    sma_px: float | None = None
    period = int(slow_period or 0)
    if period >= 2 and bars is not None and not bars.empty and len(bars) >= period:
        last_sma = sma(bars["close"].astype(float), period).iloc[-1]
        if not pd.isna(last_sma) and float(last_sma) > 0:
            sma_px = float(last_sma)

    dip, dip_detail = _continuation_dip(bars, settings=settings, sma_px=sma_px, symbol=symbol)
    if dip is Signal.BUY:
        logger.info("%s | %s | tendencia 9m alcista", symbol or "?", dip_detail)
        return Signal.BUY, dip_detail

    near, near_detail = _near_high_continuation(
        bars, settings=settings, sma_px=sma_px, symbol=symbol
    )
    if near is Signal.BUY:
        logger.info("%s | %s | tendencia 9m alcista", symbol or "?", near_detail)
        return Signal.BUY, near_detail

    micro, micro_detail = _micro_higher_high(
        bars, settings=settings, sma_px=sma_px, symbol=symbol
    )
    if micro is Signal.BUY:
        logger.info("%s | %s | tendencia 9m alcista", symbol or "?", micro_detail)
        return Signal.BUY, micro_detail

    deep, deep_detail = mean_reversion_criteria(bars, settings=settings, has_long=has_long)
    if deep is Signal.BUY:
        why = deep_detail.replace("mean-rev BUY", "trend-pullback BUY", 1)
        logger.info("%s | %s | tendencia 9m alcista (dip profundo)", symbol or "?", why)
        return Signal.BUY, why

    return Signal.HOLD, (
        f"trend-pullback sin corrección | {dip_detail} | {near_detail} | {micro_detail}"
    )
