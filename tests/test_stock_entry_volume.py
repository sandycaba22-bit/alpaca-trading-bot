"""Filtro vol_2x acciones (vela cerrada)."""

from __future__ import annotations

import pandas as pd

from bot.strategy.stock_entry_volume import check_stock_entry_signal_volume
from tests.test_sync_entry import _settings


def test_vol2x_passes_on_high_volume_bar() -> None:
    settings = _settings(stock_entry_signal_volume_mult=2.0, stock_entry_volume_period=20)
    n = 25
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    vol = [100.0] * (n - 1) + [250.0]
    bars = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": vol},
        index=idx,
    )
    ok, msg = check_stock_entry_signal_volume(bars, entry_tf="5Min", settings=settings)
    assert ok is True
    assert "vol_2x OK" in msg


def test_vol2x_rejects_low_volume() -> None:
    settings = _settings(stock_entry_signal_volume_mult=2.0)
    n = 25
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    bars = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": [100.0] * n},
        index=idx,
    )
    ok, msg = check_stock_entry_signal_volume(bars, entry_tf="5Min", settings=settings)
    assert ok is False
    assert "vol_2x rechazado" in msg
