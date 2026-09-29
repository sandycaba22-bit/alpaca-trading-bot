"""Riesgo, modos, kill switches (estado por noche/semana)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NightRiskState:
    trades_tonight: int = 0
    consecutive_losses: int = 0
    night_pnl_pct: float = 0.0
    hit_plus_1r: bool = False
    halted: bool = False
    halt_reason: str = ""
    week_pnl_pct: float = 0.0
    week_halted: bool = False
    cooldown_until_4h: int = 0


@dataclass
class RiskLimits:
    risk_normal_pct: float = 0.0075
    risk_a_plus_pct: float = 0.01
    kill_night_pct: float = -0.01
    kill_week_pct: float = -0.03
    max_trades_night: int = 3
    profit_lock_partial_pct: float = 0.012
    profit_lock_total_pct: float = 0.02


def can_open_trade(state: NightRiskState, limits: RiskLimits, *, mode: str) -> tuple[bool, str]:
    if state.week_halted:
        return False, "kill_switch_semana"
    if state.halted:
        return False, state.halt_reason or "halt_noche"
    if state.trades_tonight >= limits.max_trades_night:
        return False, "max_trades_noche"
    if state.night_pnl_pct <= limits.kill_night_pct:
        return False, "kill_noche_-1pct"
    if state.night_pnl_pct >= limits.profit_lock_total_pct:
        return False, "profit_lock_total"
    _ = mode
    return True, "ok"


def register_trade_result(state: NightRiskState, limits: RiskLimits, pnl_pct: float) -> None:
    state.trades_tonight += 1
    state.night_pnl_pct += pnl_pct
    state.week_pnl_pct += pnl_pct
    if pnl_pct < 0:
        state.consecutive_losses += 1
    else:
        state.consecutive_losses = 0
    if state.night_pnl_pct <= limits.kill_night_pct:
        state.halted = True
        state.halt_reason = "kill_noche"
    if state.week_pnl_pct <= limits.kill_week_pct:
        state.week_halted = True
    if state.consecutive_losses >= 2:
        state.halted = True
        state.halt_reason = "2_losses_segidas"
