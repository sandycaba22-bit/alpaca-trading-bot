from strategies.crypto_night.asymmetric_tp import take_profit_from_stop
from strategies.crypto_night.filter_profile import AGGRESSIVE, filters_for_profile, parse_filter_profile


def test_tp_long_2_5r():
    tp = take_profit_from_stop(
        entry_price=100.0,
        stop_price=99.0,
        side="long",
        reward_risk=2.5,
    )
    assert tp == 102.5


def test_aggressive_profile_default():
    assert parse_filter_profile(None) == "aggressive"
    flt = filters_for_profile("aggressive")
    assert flt.setup_min_volume_ratio == 1.1
    assert flt.vol_pct_low == 10.0
    assert AGGRESSIVE.quality_min_score == 2
