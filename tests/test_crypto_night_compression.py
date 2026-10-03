"""Modo compresión — ATR bajo cerca de S/R."""

from __future__ import annotations

import pandas as pd

from strategies.crypto_night.compression import near_key_level, resolve_entry_volatility
from strategies.crypto_night.types import NightBias, TradeSide


def _bars_1h(n: int = 500, *, flat_vol: bool = True) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    base = 100.0
    rows = []
    for i in range(n):
        wobble = 0.02 if flat_vol else 0.8
        c = base + (i * 0.001) + (wobble if i % 2 == 0 else -wobble)
        rows.append({"open": c - 0.01, "high": c + 0.05, "low": c - 0.05, "close": c, "volume": 100.0})
    return pd.DataFrame(rows, index=idx)


def _bars_15m(n: int = 80, *, near_support: float = 99.5) -> pd.DataFrame:
    idx = pd.date_range("2024-06-01", periods=n, freq="15min", tz="UTC")
    closes = [near_support + 0.1] * (n - 1) + [near_support + 0.08]
    rows = []
    for c in closes:
        rows.append(
            {
                "open": c - 0.02,
                "high": c + 0.03,
                "low": c - 0.04,
                "close": c,
                "volume": 200.0,
            }
        )
    return pd.DataFrame(rows, index=idx)


def test_near_key_level() -> None:
    assert near_key_level(last_price=100.0, level=99.7, max_dist_pct=0.004) is True
    assert near_key_level(last_price=100.0, level=98.0, max_dist_pct=0.004) is False


def test_low_vol_mode_off_keeps_vol_rejection() -> None:
    bars_1h = _bars_1h()
    bars_15m = _bars_15m()
    ts = bars_15m.index[-1]
    bias = NightBias(side=TradeSide.LONG, symbol="BTC/USD", reason="test")
    gate, atr, comp = resolve_entry_volatility(
        bars_1h,
        bars_15m,
        ts,
        bias,
        ts - pd.Timedelta(hours=4),
        pct_low=10.0,
        pct_high=90.0,
        low_vol_mode=False,
    )
    assert comp is False
    if atr is not None and not gate.ok:
        assert "ATR%" in gate.detail
