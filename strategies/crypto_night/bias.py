"""Candado 3 — bias 4H + 1D (HH/HL long, LH/LL short)."""

from __future__ import annotations

import pandas as pd

from strategies.crypto_night.types import GateResult, NightBias, RejectReason, TradeSide


def _swing_bias(bars: pd.DataFrame, lookback: int = 6) -> TradeSide | None:
    if bars is None or len(bars) < lookback + 2:
        return None
    tail = bars.iloc[-lookback:]
    highs = tail["high"].astype(float)
    lows = tail["low"].astype(float)
    hh = highs.iloc[-1] > highs.iloc[:-1].max()
    hl = lows.iloc[-1] > lows.iloc[:-1].min()
    lh = highs.iloc[-1] < highs.iloc[:-1].max()
    ll = lows.iloc[-1] < lows.iloc[:-1].min()
    if hh and hl:
        return TradeSide.LONG
    if lh and ll:
        return TradeSide.SHORT
    return None


def resolve_night_bias(
    bars_4h: pd.DataFrame,
    bars_1d: pd.DataFrame,
    symbol: str,
    *,
    btc_bias: NightBias | None = None,
) -> tuple[GateResult, NightBias | None]:
    b4 = _swing_bias(bars_4h)
    b1 = _swing_bias(bars_1d, lookback=5)
    if b4 is None or b1 is None or b4 != b1:
        if symbol.upper().startswith("ETH") and btc_bias is not None:
            return GateResult(True), btc_bias
        return GateResult(False, RejectReason.BIAS, "4H/1D sin alineación"), None
    side = b4
    return (
        GateResult(True),
        NightBias(side=side, symbol=symbol, reason=f"4H+1D {side.value}"),
    )
