"""Filtros régimen cripto (ATR expansion)."""

from __future__ import annotations

import pandas as pd

from bot.strategy.crypto_regime_entry import CryptoRegimeGate, atr_expansion_at


def test_atr_expansion_rejects_flat_regime() -> None:
    gate = CryptoRegimeGate(name="t", atr_expansion_mult=1.2, atr_expansion_period=10, atr_period=5)
    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    # Rango estrecho -> ATR estable
    close = [100.0 + (i % 3) * 0.01 for i in range(n)]
    bars = pd.DataFrame(
        {
            "open": close,
            "high": [c + 0.05 for c in close],
            "low": [c - 0.05 for c in close],
            "close": close,
            "volume": [1000.0] * n,
        },
        index=idx,
    )
    ok, msg = atr_expansion_at(bars, n - 1, gate)
    assert ok is False
    assert "expansion" in msg.lower() or "ATR" in msg
