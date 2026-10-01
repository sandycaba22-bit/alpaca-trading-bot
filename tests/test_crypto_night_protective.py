from strategies.crypto_night.protective_pct import apply_crypto_night_protective


def test_cap_stop_and_min_tp_long():
    sp, tp, stop_pct, tp_pct = apply_crypto_night_protective(
        entry_price=100_000.0,
        stop_price=99_300.0,
        side="long",
        reward_risk=2.5,
        max_stop_pct=0.0025,
        min_tp_pct=0.014,
    )
    assert stop_pct <= 0.0025 + 1e-9
    assert tp_pct >= 0.014 - 1e-6
    assert tp >= 100_000.0 * 1.014 - 1.0
    assert sp >= 100_000.0 * (1 - 0.0025) - 1.0


def test_tight_structural_stop_unchanged_if_within_cap():
    sp, _, stop_pct, _ = apply_crypto_night_protective(
        entry_price=50_000.0,
        stop_price=49_950.0,
        side="long",
        reward_risk=2.0,
        max_stop_pct=0.0025,
        min_tp_pct=0.014,
    )
    assert stop_pct == 0.001
    assert sp == 49_950.0
