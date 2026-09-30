import pandas as pd

from strategies.crypto_night.bias import resolve_night_bias
from strategies.crypto_night.filter_profile import filters_for_profile, parse_filter_profile


def test_parse_relaxed_aliases():
    assert parse_filter_profile("relaxed") == "relaxed"
    assert parse_filter_profile("flojo") == "relaxed"
    assert parse_filter_profile(None) == "strict"


def test_relaxed_filters_looser_than_strict():
    r = filters_for_profile("relaxed")
    s = filters_for_profile("strict")
    assert r.vol_pct_low < s.vol_pct_low
    assert r.quality_min_score < s.quality_min_score
    assert r.bias_relaxed_structure


def _flat_bars() -> pd.DataFrame:
    close = [100.0] * 10
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 0.2 for c in close],
            "low": [c - 0.2 for c in close],
            "close": close,
            "volume": [1000] * len(close),
        }
    )


def test_relaxed_bias_still_rejects_flat_market():
    b4 = _flat_bars()
    gate, bias = resolve_night_bias(
        b4,
        b4,
        "BTC/USD",
        bias_mode="4h_only",
        swing_lookback_4h=4,
        relaxed_structure=True,
    )
    assert not gate.ok
    assert bias is None
