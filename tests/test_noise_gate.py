"""Filtro de ruido en acciones: lateral y falsas rupturas no abren."""

from __future__ import annotations

import pandas as pd

from bot.strategy.noise_gate import check_stock_entry_noise, noise_limits
from tests.test_sync_entry import _settings


def _bars(closes: list[float], *, volume: float = 1000.0, last_volume: float | None = None) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-06-03 14:00", periods=n, freq="5min", tz="UTC")
    vol = [volume] * n
    if last_volume is not None:
        vol[-1] = last_volume
    rows = []
    for close in closes:
        rows.append(
            {
                "open": close - 0.05,
                "high": close + 0.08,
                "low": close - 0.12,
                "close": close,
                "volume": vol[len(rows)],
            }
        )
    return pd.DataFrame(rows, index=idx)


def test_noisy_symbols_use_stricter_limits() -> None:
    settings = _settings(adx_threshold=20.0, sync_entry_volume_mult_stock=1.35)
    aapl = noise_limits("AAPL", settings)
    intu = noise_limits("INTU", settings)
    cmcsa = noise_limits("CMCSA", settings)
    assert aapl.adx_min == 26.0
    assert aapl.volume_mult == 1.85
    assert intu.adx_min == 32.0
    assert intu.volume_mult == 2.3
    assert intu.min_efficiency == 0.48
    assert intu.rsi_max == 62.0
    assert cmcsa.adx_min == 32.0
    assert cmcsa.volume_mult == 2.3


def test_symbol_override_can_lower_adx() -> None:
    settings = _settings(adx_threshold_overrides={"INTU": 22.0})
    assert noise_limits("INTU", settings).adx_min == 22.0


def test_chop_is_rejected() -> None:
    closes = [100.0 + (0.15 if i % 2 == 0 else -0.15) for i in range(80)]
    settings = _settings(adx_threshold=25.0, adx_period=14, volume_confirmation_period=20)
    ok, reason = check_stock_entry_noise("CMCSA", _bars(closes), settings)
    assert ok is False
    assert "entrada omitida" in reason


def test_trend_with_volume_passes(monkeypatch) -> None:
    closes = [100.0 + i * 0.45 for i in range(80)]
    settings = _settings(adx_threshold=26.0, adx_period=14, volume_confirmation_period=20)
    monkeypatch.setattr(
        "bot.strategy.noise_gate.last_rsi",
        lambda _closes, _period: 55.0,
    )
    ok, reason = check_stock_entry_noise(
        "AAPL",
        _bars(closes, last_volume=4000.0),
        settings,
    )
    assert ok is True, reason
    assert "ruido OK" in reason
    assert "RSI 55.0" in reason


def test_rsi_overbought_rejected() -> None:
    closes = [100.0 + i * 0.55 for i in range(80)]
    settings = _settings(adx_threshold=26.0, adx_period=14, volume_confirmation_period=20)
    ok, reason = check_stock_entry_noise(
        "AAPL",
        _bars(closes, last_volume=5000.0),
        settings,
    )
    assert ok is False
    assert "RSI" in reason
