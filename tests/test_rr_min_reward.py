"""R:R mínimo en protective vol_2x."""

from bot.risk.rr_take_profit import _ensure_min_reward_risk_pct


def test_tp_raised_to_meet_min_rr() -> None:
    sp, tp = _ensure_min_reward_risk_pct(0.002, 0.004, max_tp=0.008, min_tp=0.0065)
    assert tp >= sp * 2.5 - 1e-9
    assert sp == 0.002


def test_stop_tightened_when_tp_capped() -> None:
    sp, tp = _ensure_min_reward_risk_pct(0.003, 0.005, max_tp=0.006, min_tp=0.006)
    assert abs(tp / sp - 2.5) < 0.02
    assert tp <= 0.006 + 1e-9
