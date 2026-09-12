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


def _plus_minus_dm(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    return plus_dm, minus_dm


def adx(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX de Wilder (suavizado EWMA con alpha = 1/period)."""
    required = {"high", "low", "close"}
    if bars is None or bars.empty or not required.issubset(bars.columns):
        return pd.Series(dtype=float)
    tr = true_range(bars)
    plus_dm, minus_dm = _plus_minus_dm(bars)
    alpha = 1.0 / period
    atr_s = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_s = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    minus_s = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_s / atr_s.replace(0.0, np.nan)
    minus_di = 100.0 * minus_s / atr_s.replace(0.0, np.nan)
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def last_adx(bars: pd.DataFrame, period: int = 14) -> float | None:
    series = adx(bars, period).dropna()
    if series.empty:
        return None
    value = float(series.iloc[-1])
    return value if np.isfinite(value) else None


def volume_vs_average(
    bars: pd.DataFrame,
    period: int = 20,
    multiplier: float = 1.0,
) -> tuple[bool | None, float | None]:
    """Compara el volumen de la última vela con la media de las N anteriores.

    Devuelve (está_por_encima, ratio) o (None, None) si no hay datos.
    """
    if bars is None or bars.empty or "volume" not in bars.columns:
        return None, None
    if period < 1 or len(bars) < period + 1:
        return None, None
    vol = bars["volume"].astype(float)
    last = float(vol.iloc[-1])
    avg = float(vol.iloc[-(period + 1) : -1].mean())
    if not np.isfinite(last) or not np.isfinite(avg) or avg <= 0:
        return None, None
    ratio = last / avg
    return ratio >= multiplier, ratio


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.astype(float).ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def last_rsi(closes: pd.Series, period: int = 14) -> float | None:
    series = rsi(closes, period).dropna()
    if series.empty:
        return None
    value = float(series.iloc[-1])
    return value if np.isfinite(value) else None


def bollinger(closes: pd.Series, period: int = 20, std_mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(closes, period)
    std = closes.astype(float).rolling(period, min_periods=period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return lower, mid, upper


def bollinger_width(closes: pd.Series, period: int = 20, std_mult: float = 2.0) -> pd.Series:
    lower, mid, upper = bollinger(closes, period, std_mult)
    return (upper - lower) / mid.replace(0.0, np.nan)


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
