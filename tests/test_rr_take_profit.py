import pytest

from bot.risk.rr_take_profit import compute_rr_take_profit, enforce_vol2x_protective
from bot.risk.stops import MIN_REWARD_RISK, StopTakeProfitPolicy
from bot.security.exceptions import ValidationError


def test_long_rr_2():
    tp, pct = compute_rr_take_profit(
        entry_price=100.0,
        stop_price=99.65,
        qty=10,
        symbol="SPY",
        reward_risk=2.0,
    )
    assert tp == 100.70
    assert pct == pytest.approx(0.007, rel=1e-3)


def test_short_rr_2():
    tp, _ = compute_rr_take_profit(
        entry_price=100.0,
        stop_price=100.35,
        qty=-10,
        symbol="SPY",
        reward_risk=2.0,
    )
    assert tp == 99.30


def test_enforce_replaces_zero_tp():
    sp, tp, st_pct, tp_pct = enforce_vol2x_protective(
        symbol="SLV",
        side="buy",
        reason="vol_2x",
        entry_price=25.0,
        qty=5,
        stop_price=24.9125,
        take_profit_price=0.0,
        stop_pct=0.0035,
        take_profit_pct=0.0,
    )
    assert st_pct <= 0.0021 + 1e-9
    assert sp >= 25.0 * (1 - 0.0021) - 0.02
    assert tp_pct >= 0.0065 - 1e-4
    assert tp_pct <= 0.008 + 1e-4


def test_pct_fallback_lifts_tp_to_min_reward_risk():
    policy = StopTakeProfitPolicy(
        stop_loss_pct=0.01,
        take_profit_pct=0.015,
        max_stop_pct=0.0,
        max_tp_pct=0.20,
        min_tp_pct=0.0,
    )
    levels = policy.levels(100.0, 10)
    assert levels.stop_pct == pytest.approx(0.01)
    assert levels.take_profit_pct == pytest.approx(0.01 * MIN_REWARD_RISK)
    assert levels.take_profit_price == pytest.approx(102.5)


def test_tp_cap_tightens_stop_to_keep_reward_risk():
    policy = StopTakeProfitPolicy(
        stop_loss_pct=0.01,
        take_profit_pct=0.012,
        max_stop_pct=0.0,
        max_tp_pct=0.012,
        min_tp_pct=0.0,
    )
    levels = policy.levels(100.0, 10)
    assert levels.take_profit_pct == pytest.approx(0.012)
    assert levels.stop_pct == pytest.approx(0.012 / MIN_REWARD_RISK)
    assert levels.take_profit_pct >= levels.stop_pct * MIN_REWARD_RISK - 1e-9


def test_enforce_rejects_missing_stop():
    with pytest.raises(ValidationError):
        enforce_vol2x_protective(
            symbol="SLV",
            side="buy",
            reason="vol_2x",
            entry_price=25.0,
            qty=5,
            stop_price=0.0,
            take_profit_price=0.0,
            stop_pct=0.0,
            take_profit_pct=0.0,
        )
