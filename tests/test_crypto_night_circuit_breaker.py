"""Circuit breaker 3 pérdidas — crypto night risk."""

from strategies.crypto_night.risk import (
    CIRCUIT_BREAKER_HALT_REASON,
    NightRiskState,
    RiskLimits,
    can_open_trade,
    register_trade_result,
    reset_circuit_breaker_for_new_cycle,
)


def test_three_losses_halt_even_when_limits_off() -> None:
    state = NightRiskState()
    limits = RiskLimits(limits_enabled=False)
    for _ in range(3):
        register_trade_result(state, limits, -0.002)
    ok, reason = can_open_trade(state, limits, mode="normal")
    assert not ok
    assert reason == CIRCUIT_BREAKER_HALT_REASON
    assert state.consecutive_losses == 3


def test_win_resets_streak() -> None:
    state = NightRiskState()
    limits = RiskLimits(limits_enabled=False)
    register_trade_result(state, limits, -0.001)
    register_trade_result(state, limits, -0.001)
    register_trade_result(state, limits, 0.005)
    register_trade_result(state, limits, -0.001)
    ok, _ = can_open_trade(state, limits, mode="normal")
    assert ok
    assert state.consecutive_losses == 1


def test_daily_reset_clears_circuit_halt() -> None:
    state = NightRiskState(halted=True, halt_reason=CIRCUIT_BREAKER_HALT_REASON, consecutive_losses=3)
    reset_circuit_breaker_for_new_cycle(state)
    assert not state.halted
    assert state.consecutive_losses == 0
