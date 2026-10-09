"""Tendencia 4H cuando el ATR está por encima del techo del sweep."""

from __future__ import annotations

import pandas as pd

from strategies.crypto_night.trend_regime import (
    ATR_EXPANSION_MIN,
    evaluate_high_vol_trend,
    htf_close_above_sma50,
)


def _frame(n: int, freq: str, *, close_at, high_pad: float, low_pad: float, volume: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    rows = []
    for i in range(n):
        c = close_at(i)
        rows.append(
            {
                "open": c - 0.01,
                "high": c + high_pad,
                "low": c - low_pad,
                "close": c,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows, index=idx)


def test_htf_bull_requires_close_above_sma50() -> None:
    rising = _frame(80, "4h", close_at=lambda i: 100 + i, high_pad=0.4, low_pad=0.4)
    falling = _frame(80, "4h", close_at=lambda i: 200 - i, high_pad=0.4, low_pad=0.4)
    assert htf_close_above_sma50(rising)[0] is True
    assert htf_close_above_sma50(falling)[0] is False


def test_high_vol_without_expansion_is_hold() -> None:
    bars_4h = _frame(80, "4h", close_at=lambda i: 100 + i * 0.2, high_pad=0.3, low_pad=0.3)
    bars_1h = _frame(400, "1h", close_at=lambda i: 100 + i * 0.01, high_pad=0.4, low_pad=0.4)
    bars_15m = _frame(40, "15min", close_at=lambda i: 140, high_pad=0.2, low_pad=0.2)
    setup, msg = evaluate_high_vol_trend(bars_1h, bars_4h, bars_15m, symbol="ETH/USD")
    assert setup is None
    assert "htf_bull=true" in msg
    assert "señal=hold" in msg
    assert "atr_ratio=" in msg


def test_sma50_and_expansion_is_buy() -> None:
    bars_4h = _frame(80, "4h", close_at=lambda i: 100 + i, high_pad=0.5, low_pad=0.5)
    bars_1h = _frame(
        400,
        "1h",
        close_at=lambda i: 100 + i * 0.05,
        high_pad=0.15,
        low_pad=0.15,
    )
    last_i = bars_1h.index[-1]
    bars_1h.loc[last_i, "high"] = float(bars_1h.loc[last_i, "close"]) + 400.0
    bars_1h.loc[last_i, "low"] = float(bars_1h.loc[last_i, "close"]) - 400.0
    bars_15m = _frame(40, "15min", close_at=lambda i: float(bars_1h["close"].iloc[-1]), high_pad=0.2, low_pad=0.2, volume=500.0)
    setup, msg = evaluate_high_vol_trend(bars_1h, bars_4h, bars_15m, symbol="ETH/USD")
    assert "señal=buy" in msg, msg
    assert setup is not None
    assert setup.variant == "trend_4h_sma50_atr_exp_1.2"
    assert setup.side.value == "long"
    assert float(setup.meta["atr_ratio"]) >= ATR_EXPANSION_MIN
