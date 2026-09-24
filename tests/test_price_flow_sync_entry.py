"""Flujo en tape para sync_entry (opción A: sin doble ruptura)."""

from __future__ import annotations

import pandas as pd

from bot.strategy.base import Signal, StrategyContext
from bot.strategy.price_flow import PriceFlowFilter


def test_sync_entry_skips_agile_breakout_when_tape_ok() -> None:
    bars = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104, 105],
            "high": [101, 102, 103, 104, 105, 106],
            "low": [99, 100, 101, 102, 103, 104],
            "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
            "volume": [1000] * 6,
        }
    )
    ctx = StrategyContext(
        symbol="ETH/USD",
        bars=bars,
        has_long_position=False,
        has_short_position=False,
        last_price=105.48,
        spread_pct=0.0003,
        momentum_pct=0.001,
        atr=1.0,
        entry_strategy="sync_entry",
    )
    ok, reason = PriceFlowFilter().confirm(Signal.BUY, ctx)
    assert ok is True
    assert "sync_entry" in reason


def test_sync_entry_still_blocks_strong_adverse_tape() -> None:
    bars = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104, 105],
            "high": [101, 102, 103, 104, 105, 106],
            "low": [99, 100, 101, 102, 103, 104],
            "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
            "volume": [1000] * 6,
        }
    )
    ctx = StrategyContext(
        symbol="ETH/USD",
        bars=bars,
        has_long_position=False,
        has_short_position=False,
        last_price=104.0,
        spread_pct=0.0003,
        momentum_pct=0.001,
        atr=1.0,
        entry_strategy="sync_entry",
    )
    ok, reason = PriceFlowFilter().confirm(Signal.BUY, ctx)
    assert ok is False
    assert "flujo bajista" in reason
