"""Candado 2 — ATR% 1H en percentil 30–70 (lookback 20 días)."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class VolatilitySnapshot:
    current: float
    pct_floor: float
    pct_ceiling: float
    in_band: bool


def volatility_snapshot(
    bars_1h: pd.DataFrame,
    at_ts: pd.Timestamp,
    *,
    lookback_days: int = 20,
    pct_low: float = 30.0,
    pct_high: float = 70.0,
) -> tuple[GateResult, VolatilitySnapshot | None]:
    atr_pct = atr_percent_series(bars_1h)
    if atr_pct.empty:
        return GateResult(False, RejectReason.VOLATILITY, "ATR% 1H insuficiente"), None
    hist = atr_pct.loc[atr_pct.index <= at_ts].dropna()
    if len(hist) < lookback_days * 12:
        return GateResult(False, RejectReason.VOLATILITY, "historia ATR% corta"), None
    window = hist.iloc[-lookback_days * 24 :]
    current = float(hist.iloc[-1])
    p_lo = float(np.percentile(window, pct_low))
    p_hi = float(np.percentile(window, pct_high))
    snap = VolatilitySnapshot(
        current=current,
        pct_floor=p_lo,
        pct_ceiling=p_hi,
        in_band=p_lo <= current <= p_hi,
    )
    if not snap.in_band:
        return (
            GateResult(
                False,
                RejectReason.VOLATILITY,
                f"ATR%={current:.3f} fuera [{p_lo:.3f},{p_hi:.3f}]",
            ),
            snap,
        )
    return GateResult(True), snap


def volatility_gate(
    bars_1h: pd.DataFrame,
    at_ts: pd.Timestamp,
    *,
    lookback_days: int = 20,
    pct_low: float = 30.0,
    pct_high: float = 70.0,
) -> tuple[GateResult, float | None, float | None]:
    gate, snap = volatility_snapshot(
        bars_1h,
        at_ts,
        lookback_days=lookback_days,
        pct_low=pct_low,
        pct_high=pct_high,
    )
    if snap is None:
        return gate, None, None
    if gate.ok:
        return gate, snap.current, snap.pct_floor
    return gate, snap.current, None
