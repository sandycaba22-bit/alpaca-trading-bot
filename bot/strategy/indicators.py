"""Indicadores técnicos reutilizados por estrategia, riesgo y backtest."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.astype(float).rolling(window, min_periods=window).mean()


def true_range(bars: pd.DataFrame) -> pd.Series:
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR de Wilder (EWMA con alpha = 1/period)."""
    tr = true_range(bars)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def momentum_pct(closes: pd.Series, bars: int = 5) -> float | None:
    """Rendimiento porcentual de los últimos `bars` cierres."""
    if len(closes) < bars + 1:
        return None
    start = float(closes.iloc[-(bars + 1)])
    end = float(closes.iloc[-1])
    if start == 0:
        return None
    return (end - start) / start


def last_atr(bars: pd.DataFrame, period: int = 14) -> float | None:
    series = atr(bars, period).dropna()
    if series.empty:
        return None
    value = float(series.iloc[-1])
    return value if np.isfinite(value) else None


def classify_regime(
    closes: pd.Series,
    lookback: int = 60,
    threshold: float = 0.08,
) -> str:
    """Clasifica el tramo reciente: bull / bear / sideways."""
    if len(closes) < lookback + 1:
        return "sideways"
    ret = float(closes.iloc[-1] / closes.iloc[-(lookback + 1)] - 1)
    if ret >= threshold:
        return "bull"
    if ret <= -threshold:
        return "bear"
    return "sideways"
