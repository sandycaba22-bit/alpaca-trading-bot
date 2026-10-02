"""Variantes SWAP V1–V5 (solo una activa por noche)."""

from __future__ import annotations

from enum import Enum

import pandas as pd

from strategies.crypto_night.types import NightBias, SweepSetup, TradeSide


class SweepVariant(str, Enum):
    V1 = "V1"
    V2 = "V2"
    V3 = "V3"
    V4 = "V4"
    V5 = "V5"
    CHAMPION = "CHAMPION"


def parse_sweep_variant(raw: str | None) -> SweepVariant:
    key = (raw or "V1").strip().upper()
    if key == "CHAMPION":
        return SweepVariant.CHAMPION
    try:
        return SweepVariant(key)
    except ValueError as exc:
        raise ValueError(f"SWEEP_VARIANT invalido: {raw!r}") from exc


def _reclaim_pattern(
    bars_15m: pd.DataFrame,
    i: int,
    level: float,
    side: TradeSide,
    vol_ma: pd.Series,
    *,
    min_volume_ratio: float = 1.5,
) -> SweepSetup | None:
    if i < 2 or i >= len(bars_15m):
        return None
    row = bars_15m.iloc[i]
    prev = bars_15m.iloc[i - 1]
    o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
    vol = float(row.volume or 0)
    vma = float(vol_ma.iloc[i]) if i < len(vol_ma) and vol_ma.iloc[i] > 0 else 0.0
    vol_ratio = vol / vma if vma > 0 else 0.0
    if vol_ratio < min_volume_ratio:
        return None
    if side == TradeSide.LONG:
        swept = float(prev.low) < level and l < level
        reclaim = c > level and c > o
        if not (swept and reclaim):
            return None
        extreme = min(float(prev.low), l)
        limit_price = (o + c) / 2.0
        stop = extreme - 0.1 * abs(level - extreme)
    else:
        swept = float(prev.high) > level and h > level
        reclaim = c < level and c < o
        if not (swept and reclaim):
            return None
        extreme = max(float(prev.high), h)
        limit_price = (o + c) / 2.0
        stop = extreme + 0.1 * abs(level - extreme)
    return SweepSetup(
        side=side,
        symbol="",
        sweep_extreme=extreme,
        reclaim_bar_idx=i,
        limit_price=limit_price,
        stop_price=stop,
        atr_1h=0.0,
        volume_ratio=vol_ratio,
        variant="",
        meta={"level": level},
    )


def find_v1_session_sweep(
    bars_15m: pd.DataFrame,
    session_start: pd.Timestamp,
    bias: NightBias,
    vol_ma: pd.Series,
    *,
    min_volume_ratio: float = 1.5,
    max_bars_scan: int = 3,
) -> SweepSetup | None:
    window = bars_15m.loc[bars_15m.index >= session_start]
    if len(window) < 5:
        return None
    if bias.side == TradeSide.LONG:
        level = float(window["low"].astype(float).min())
    else:
        level = float(window["high"].astype(float).max())
    cap = max(2, int(max_bars_scan))
    for j in range(2, len(window)):
        setup = _reclaim_pattern(
            window,
            j,
            level,
            bias.side,
            vol_ma.loc[window.index],
            min_volume_ratio=min_volume_ratio,
        )
        if setup:
            setup.symbol = bias.symbol
            setup.variant = "V1"
            return setup
        if j >= cap:
            break
    return None


def find_v2_equal_levels(
    bars_15m: pd.DataFrame,
    at_ts: pd.Timestamp,
    bias: NightBias,
    vol_ma: pd.Series,
    hours: int = 4,
    *,
    min_volume_ratio: float = 1.5,
) -> SweepSetup | None:
    start = at_ts - pd.Timedelta(hours=hours)
    window = bars_15m.loc[(bars_15m.index >= start) & (bars_15m.index <= at_ts)]
    if len(window) < 8:
        return None
    highs = window["high"].astype(float)
    lows = window["low"].astype(float)
    if bias.side == TradeSide.LONG:
        level = float(lows.min())
    else:
        level = float(highs.max())
    idx = window.index.get_indexer([at_ts], method="pad")[0]
    if idx < 2:
        return None
    setup = _reclaim_pattern(
        window,
        idx,
        level,
        bias.side,
        vol_ma.loc[window.index],
        min_volume_ratio=min_volume_ratio,
    )
    if setup:
        setup.symbol = bias.symbol
        setup.variant = "V2"
    return setup


def find_v3_prior_day(
    bars_15m: pd.DataFrame,
    bars_1d: pd.DataFrame,
    at_ts: pd.Timestamp,
    bias: NightBias,
    vol_ma: pd.Series,
) -> SweepSetup | None:
    daily = bars_1d.loc[bars_1d.index < at_ts.normalize()]
    if daily.empty:
        return None
    prev = daily.iloc[-1]
    level = float(prev["high"] if bias.side == TradeSide.SHORT else prev["low"])
    window = bars_15m.loc[bars_15m.index <= at_ts].tail(40)
    if len(window) < 3:
        return None
    setup = _reclaim_pattern(window, len(window) - 1, level, bias.side, vol_ma.loc[window.index])
    if setup:
        setup.symbol = bias.symbol
        setup.variant = "V3"
    return setup


def find_v4_asia_range(
    bars_15m: pd.DataFrame,
    session_start: pd.Timestamp,
    bias: NightBias,
    vol_ma: pd.Series,
) -> SweepSetup | None:
    asia_end = session_start + pd.Timedelta(hours=4)
    asia = bars_15m.loc[(bars_15m.index >= session_start) & (bars_15m.index <= asia_end)]
    if len(asia) < 4:
        return None
    level = float(asia["low"].min() if bias.side == TradeSide.LONG else asia["high"].max())
    after = bars_15m.loc[bars_15m.index > asia_end].head(12)
    for j in range(2, len(after)):
        setup = _reclaim_pattern(after, j, level, bias.side, vol_ma.loc[after.index])
        if setup:
            setup.symbol = bias.symbol
            setup.variant = "V4"
            return setup
    return None


def find_v5_double_sweep(
    bars_15m: pd.DataFrame,
    at_ts: pd.Timestamp,
    bias: NightBias,
    vol_ma: pd.Series,
) -> SweepSetup | None:
    start = at_ts - pd.Timedelta(hours=2)
    window = bars_15m.loc[(bars_15m.index >= start) & (bars_15m.index <= at_ts)]
    if len(window) < 6:
        return None
    sweeps = 0
    if bias.side == TradeSide.LONG:
        level = float(window["low"].min())
        for _, row in window.iterrows():
            if float(row["low"]) < level * 1.001:
                sweeps += 1
    else:
        level = float(window["high"].max())
        for _, row in window.iterrows():
            if float(row["high"]) > level * 0.999:
                sweeps += 1
    if sweeps < 2:
        return None
    setup = _reclaim_pattern(window, len(window) - 1, level, bias.side, vol_ma.loc[window.index])
    if setup:
        setup.symbol = bias.symbol
        setup.variant = "V5"
    return setup


def find_setup_for_variant(
    variant: SweepVariant,
    *,
    bars_15m: pd.DataFrame,
    bars_1d: pd.DataFrame,
    session_start: pd.Timestamp,
    at_ts: pd.Timestamp,
    bias: NightBias,
    vol_ma: pd.Series,
    min_volume_ratio: float = 1.5,
    v1_max_bars_scan: int = 3,
) -> SweepSetup | None:
    v = SweepVariant.CHAMPION if variant == SweepVariant.CHAMPION else variant
    if v == SweepVariant.V1:
        return find_v1_session_sweep(
            bars_15m,
            session_start,
            bias,
            vol_ma,
            min_volume_ratio=min_volume_ratio,
            max_bars_scan=v1_max_bars_scan,
        )
    if v == SweepVariant.V2:
        return find_v2_equal_levels(
            bars_15m,
            at_ts,
            bias,
            vol_ma,
            min_volume_ratio=min_volume_ratio,
        )
    if v == SweepVariant.V3:
        return find_v3_prior_day(bars_15m, bars_1d, at_ts, bias, vol_ma)
    if v == SweepVariant.V4:
        return find_v4_asia_range(bars_15m, session_start, bias, vol_ma)
    if v == SweepVariant.V5:
        return find_v5_double_sweep(bars_15m, at_ts, bias, vol_ma)
    return find_v1_session_sweep(bars_15m, session_start, bias, vol_ma)
