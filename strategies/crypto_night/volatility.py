"""Candado 2 — ATR% 1H en percentil 30–70 (lookback 20 días)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.crypto_night.types import GateResult, RejectReason


def atr_percent_series(bars_1h: pd.DataFrame, period: int = 14) -> pd.Series:
    if bars_1h.empty or len(bars_1h) < period + 2:
        return pd.Series(dtype=float)
    high = bars_1h["high"].astype(float)
    low = bars_1h["low"].astype(float)
    close = bars_1h["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period, min_periods=period).mean()
    return (atr / close.replace(0, np.nan)) * 100.0


def volatility_gate(
    bars_1h: pd.DataFrame,
    at_ts: pd.Timestamp,
    *,
    lookback_days: int = 20,
    pct_low: float = 30.0,
    pct_high: float = 70.0,
) -> tuple[GateResult, float | None, float | None]:
    atr_pct = atr_percent_series(bars_1h)
    if atr_pct.empty:
        return GateResult(False, RejectReason.VOLATILITY, "ATR% 1H insuficiente"), None, None
    hist = atr_pct.loc[atr_pct.index <= at_ts].dropna()
    if len(hist) < lookback_days * 12:
        return (
            GateResult(False, RejectReason.VOLATILITY, "historia ATR% corta"),
            None,
            None,
        )
    window = hist.iloc[-lookback_days * 24 :]
    current = float(hist.iloc[-1])
    p30 = float(np.percentile(window, pct_low))
    p70 = float(np.percentile(window, pct_high))
    if not (p30 <= current <= p70):
        return (
            GateResult(
                False,
                RejectReason.VOLATILITY,
                f"ATR%={current:.3f} fuera [{p30:.3f},{p70:.3f}]",
            ),
            current,
            None,
        )
    return GateResult(True), current, p30
