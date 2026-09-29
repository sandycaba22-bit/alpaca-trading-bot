import pandas as pd

from strategies.crypto_night.bias import parse_bias_mode, resolve_night_bias


def _bars(direction: str, n: int = 10) -> pd.DataFrame:
    if direction == "up":
        close = [100 + i * 2 for i in range(n)]
    else:
        close = [200 - i * 2 for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1000] * n,
        }
    )


def test_4h_only_passes_without_1d_match():
    b4 = _bars("up")
    b1 = _bars("down")
    gate, bias = resolve_night_bias(b4, b1, "BTC/USD", bias_mode="4h_only")
    assert gate.ok
    assert bias is not None


def test_strict_still_requires_alignment():
    b4 = _bars("up")
    b1 = _bars("down")
    gate, _ = resolve_night_bias(b4, b1, "BTC/USD", bias_mode="4h_1d")
    assert not gate.ok


def test_parse_4h_only():
    assert parse_bias_mode("4h_only") == "4h_only"
