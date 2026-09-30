import pytest

from bot.risk.rr_take_profit import compute_rr_take_profit, enforce_vol2x_protective
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
    sp, tp, _, tp_pct = enforce_vol2x_protective(
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
    assert sp == 24.9125
    assert tp > 25.0
    assert tp_pct > 0


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
