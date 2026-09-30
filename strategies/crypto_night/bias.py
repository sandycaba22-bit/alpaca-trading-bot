"""Candado 3 — bias 4H + 1D (HH/HL long, LH/LL short)."""

from __future__ import annotations

import pandas as pd

from strategies.crypto_night.types import GateResult, NightBias, RejectReason, TradeSide


def _swing_bias(
    bars: pd.DataFrame,
    lookback: int = 6,
    *,
    relaxed_structure: bool = False,
) -> TradeSide | None:
    if bars is None or len(bars) < lookback + 2:
        return None
    tail = bars.iloc[-lookback:]
    highs = tail["high"].astype(float)
    lows = tail["low"].astype(float)
    closes = tail["close"].astype(float)
    hh = highs.iloc[-1] > highs.iloc[:-1].max()
    hl = lows.iloc[-1] > lows.iloc[:-1].min()
    lh = highs.iloc[-1] < highs.iloc[:-1].max()
    ll = lows.iloc[-1] < lows.iloc[:-1].min()
    if relaxed_structure:
        up_close = closes.iloc[-1] > closes.iloc[0]
        down_close = closes.iloc[-1] < closes.iloc[0]
        if (hh or hl) and up_close:
            return TradeSide.LONG
        if (lh or ll) and down_close:
            return TradeSide.SHORT
        return None
    if hh and hl:
        return TradeSide.LONG
    if lh and ll:
        return TradeSide.SHORT
    return None


def parse_bias_mode(raw: str | None) -> str:
    key = (raw or "4h_1d").strip().lower().replace("-", "_")
    if key in {"4h_only", "4h", "only_4h"}:
        return "4h_only"
    if key in {"4h_1d", "4h1d", "strict", "default"}:
        return "4h_1d"
    raise ValueError(f"CRYPTO_NIGHT_BIAS_MODE invalido: {raw!r} (4h_1d | 4h_only)")


def resolve_night_bias(
    bars_4h: pd.DataFrame,
    bars_1d: pd.DataFrame,
    symbol: str,
    *,
    btc_bias: NightBias | None = None,
    bias_mode: str = "4h_1d",
    swing_lookback_4h: int = 6,
    swing_lookback_1d: int = 5,
    relaxed_structure: bool = False,
) -> tuple[GateResult, NightBias | None]:
    mode = parse_bias_mode(bias_mode)
    b4 = _swing_bias(
        bars_4h,
        lookback=swing_lookback_4h,
        relaxed_structure=relaxed_structure,
    )
    b1 = _swing_bias(
        bars_1d,
        lookback=swing_lookback_1d,
        relaxed_structure=relaxed_structure,
    )

    if mode == "4h_only":
        if b4 is not None:
            return (
                GateResult(True),
                NightBias(side=b4, symbol=symbol, reason=f"4H only {b4.value}"),
            )
        if symbol.upper().startswith("ETH") and btc_bias is not None:
            return GateResult(True), btc_bias
        return GateResult(False, RejectReason.BIAS, "4H sin estructura clara"), None

    if b4 is None or b1 is None or b4 != b1:
        if symbol.upper().startswith("ETH") and btc_bias is not None:
            return GateResult(True), btc_bias
        return GateResult(False, RejectReason.BIAS, "4H/1D sin alineación"), None
    side = b4
    return (
        GateResult(True),
        NightBias(side=side, symbol=symbol, reason=f"4H+1D {side.value}"),
    )
