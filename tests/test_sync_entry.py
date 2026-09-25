"""Tests de entrada sync (velas cerradas + decisión)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.config import Settings
from bot.strategy.sync_entry import closed_bars_only, evaluate_sync_entry, timeframe_seconds


def _settings(**overrides) -> Settings:
    base = {
        "api_key_id": "k",
        "api_secret_key": "s",
        "api_base_url": "https://paper-api.alpaca.markets",
        "paper": True,
        "bot_profile": "stocks",
        "data_dir": None,
        "telegram_prefix": "",
        "log_level": "INFO",
        "dry_run": True,
        "poll_interval_seconds": 60,
        "scheduler_tick_seconds": 60,
        "tf_3m_seconds": 180,
        "tf_entry_seconds": 300,
        "stock_entry_timeframe": "5Min",
        "stock_regime_timeframe": "15Min",
        "stock_entry_signal_volume_mult": 0.0,
        "stock_entry_volume_period": 20,
        "tf_9m_seconds": 540,
        "tf_spike_threshold_pct": 0.005,
        "tf_macro_strong_pct": 2.5,
        "symbols": ["AAPL"],
        "stock_symbols": ["AAPL"],
        "crypto_symbols": [],
        "sma_fast": 20,
        "sma_slow": 50,
        "crypto_sma_fast": 9,
        "crypto_sma_slow": 21,
        "bar_timeframe": "1Day",
        "stock_data_feed": "iex",
        "crypto_bar_timeframe": "15Min",
        "crypto_regime_timeframe": "30Min",
        "lookback_bars": 120,
        "max_open_positions": 3,
        "position_size_pct": 0.05,
        "max_notional_per_order": 2000.0,
        "allow_short": False,
        "stop_loss_pct": 0.01,
        "take_profit_pct": 0.015,
        "close_on_mode_switch": True,
        "atr_period": 14,
        "atr_stop_mult": 1.5,
        "momentum_bars": 5,
        "max_spread_pct": 0.003,
        "order_limit_spread_pct": 0.0015,
        "order_retry_max": 8,
        "order_retry_timeout_seconds": 120,
        "order_retry_max_sl": 8,
        "order_retry_timeout_sl_seconds": 120,
        "order_retry_backoff_seconds": 1.0,
        "crypto_maker_first_enabled": False,
        "crypto_maker_timeout_seconds": 30,
        "crypto_maker_fallback": "market",
        "crypto_maker_max_retries": 2,
        "crypto_maker_tick_inside": 1,
        "crypto_maker_poll_seconds": 1.0,
        "crypto_maker_fee_pct": 0.0,
        "crypto_maker_taker_fee_pct": 0.0,
        "adverse_momentum_pct": 0.01,
        "backtest_years": 3,
        "backtest_cash": 100_000.0,
        "api_data_per_minute": 200,
        "order_per_minute": 200,
        "order_per_day": 10_000,
    }
    base.update(overrides)
    from pathlib import Path

    if base.get("data_dir") is None:
        base["data_dir"] = Path("data")
    return Settings(**base)


def test_closed_bars_drops_forming_candle() -> None:
    start = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([start + timedelta(minutes=5 * i) for i in range(5)])
    bars = pd.DataFrame(
        {
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.5] * 5,
            "volume": [1000] * 5,
        },
        index=idx,
    )
    # Last bar started at 14:20; at 14:22 the 14:20-14:25 bar is still forming
    now = datetime(2025, 1, 1, 14, 22, tzinfo=timezone.utc)
    trimmed = closed_bars_only(bars, "5Min", now)
    assert len(trimmed) == 4
    assert trimmed.index[-1] == idx[-2]


def test_evaluate_sync_entry_hold_without_position_data() -> None:
    settings = _settings()
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    decision = evaluate_sync_entry(
        entry_bars=empty,
        regime_bars=empty,
        confirm_bars=empty,
        entry_tf="5Min",
        regime_tf="9Min",
        confirm_tf="15Min",
        has_long=False,
        is_crypto=False,
        settings=settings,
    )
    assert not decision.allowed
    assert "sin velas" in decision.reason


def test_timeframe_seconds_known() -> None:
    assert timeframe_seconds("5Min") == 300
