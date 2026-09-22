"""Puntuación de entradas: elige la mejor estrategia y filtra setups débiles."""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import Settings, entry_score_min_for
from bot.strategy.base import Signal, StrategyContext
from bot.strategy.multi_tf_analysis import MacroSnapshot, SpikeSnapshot
from bot.strategy.regime_selector import MarketRegime, RegimeSnapshot, StrategyId

_STRATEGY_BASE: dict[StrategyId, float] = {
    StrategyId.BREAKOUT: 45.0,
    StrategyId.TREND_PULLBACK: 42.0,
    StrategyId.SQUEEZE: 40.0,
    StrategyId.PULLBACK: 38.0,
    StrategyId.MEAN_REV: 32.0,
}


@dataclass(frozen=True)
class ScoreResult:
    total: float
    parts: tuple[str, ...]

    def summary(self) -> str:
        detail = ", ".join(self.parts) if self.parts else "sin componentes"
        return f"score={self.total:.0f} ({detail})"


def _htf_bonus(signal: Signal, htf_trend: str | None) -> tuple[float, str | None]:
    trend = str(htf_trend or "").strip().lower()
    if signal is Signal.BUY and trend == "bull":
        return 25.0, "HTF bull +25"
    if signal is Signal.BUY and trend == "sideways":
        return 5.0, "HTF lateral +5"
    if signal is Signal.SELL and trend == "bear":
        return 25.0, "HTF bear +25"
    if signal is Signal.BUY and trend == "bear":
        return -20.0, "HTF bear -20"
    if signal is Signal.SELL and trend == "bull":
        return -20.0, "HTF bull -20"
    return 0.0, None


def _adx_bonus(regime: RegimeSnapshot | None, settings: Settings) -> tuple[float, str | None]:
    if regime is None or regime.adx is None:
        return 0.0, None
    threshold = float(settings.adx_threshold)
    if regime.regime is not MarketRegime.TREND:
        return 0.0, None
    extra = max(0.0, float(regime.adx) - threshold)
    bonus = min(20.0, extra * 0.8)
    if bonus <= 0:
        return 0.0, None
    return bonus, f"ADX fuerte +{bonus:.0f}"


def _momentum_bonus(signal: Signal, momentum_pct: float | None) -> tuple[float, str | None]:
    if momentum_pct is None:
        return 0.0, None
    mom = float(momentum_pct)
    if signal is Signal.BUY:
        if mom >= 0.001:
            bonus = min(15.0, mom * 8000.0)
            return bonus, f"momentum +{bonus:.0f}"
        if mom <= -0.001:
            penalty = min(15.0, abs(mom) * 8000.0)
            return -penalty, f"momentum -{penalty:.0f}"
    if signal is Signal.SELL:
        if mom <= -0.001:
            bonus = min(15.0, abs(mom) * 8000.0)
            return bonus, f"momentum bajista +{bonus:.0f}"
        if mom >= 0.001:
            penalty = min(15.0, mom * 8000.0)
            return -penalty, f"momentum alcista -{penalty:.0f}"
    return 0.0, None


def _spread_bonus(spread_pct: float | None) -> tuple[float, str | None]:
    if spread_pct is None:
        return 0.0, None
    spread = float(spread_pct)
    if spread <= 0.0005:
        return 5.0, "spread fino +5"
    if spread >= 0.0015:
        penalty = min(12.0, (spread - 0.0015) * 4000.0 + 8.0)
        return -penalty, f"spread ancho -{penalty:.0f}"
    return 0.0, None


def score_strategy_candidate(
    signal: Signal,
    strategy: StrategyId | None,
    regime: RegimeSnapshot | None,
    ctx: StrategyContext,
    settings: Settings,
) -> ScoreResult:
    """Puntúa un candidato de estrategia para resolver colisiones."""
    parts: list[str] = []
    total = 0.0
    if strategy is not None:
        base = _STRATEGY_BASE.get(strategy, 30.0)
        total += base
        parts.append(f"{strategy.value} +{base:.0f}")

    for bonus_fn in (
        lambda: _htf_bonus(signal, ctx.htf_trend),
        lambda: _adx_bonus(regime, settings),
        lambda: _momentum_bonus(signal, ctx.momentum_pct),
        lambda: _spread_bonus(ctx.spread_pct),
    ):
        delta, label = bonus_fn()
        if label:
            total += delta
            parts.append(label)
    return ScoreResult(total=total, parts=tuple(parts))


def score_entry_gate(
    signal: Signal,
    *,
    trend: str | None,
    structure: str | None,
    trend_return_pct: float,
    structure_return_pct: float,
    momentum_pct: float | None,
    macro: MacroSnapshot | None,
    spike: SpikeSnapshot | None,
    settings: Settings,
) -> ScoreResult:
    """Puntúa si conviene ejecutar la señal (capas HTF, macro suave, spike)."""
    parts: list[str] = []
    total = 40.0
    parts.append("base +40")

    trend_key = str(trend or "").strip().lower()
    structure_key = str(structure or "").strip().lower()

    if signal is Signal.BUY:
        if trend_key == "bull":
            total += 25.0
            parts.append("tendencia bull +25")
        elif trend_key == "sideways":
            total += 10.0
            parts.append("tendencia lateral +10")
        elif trend_key == "bear":
            total -= 25.0
            parts.append("tendencia bear -25")

        if structure_key == "bull":
            total += 10.0
            parts.append("estructura bull +10")
        elif structure_key == "bear":
            total -= 10.0
            parts.append("estructura bear -10")

        total += min(12.0, max(-12.0, float(trend_return_pct) * 2.0))
        if abs(trend_return_pct) >= 0.5:
            parts.append(f"ret HTF {trend_return_pct:+.1f}%")
        total += min(8.0, max(-8.0, float(structure_return_pct) * 1.5))
    elif signal is Signal.SELL:
        if trend_key == "bear":
            total += 25.0
            parts.append("tendencia bear +25")
        elif trend_key == "bull":
            total -= 15.0
            parts.append("tendencia bull -15")

    mom_delta, mom_label = _momentum_bonus(signal, momentum_pct)
    if mom_label:
        total += mom_delta
        parts.append(mom_label)

    if macro is not None:
        if signal is Signal.BUY and macro.blocks_buy:
            penalty = float(settings.entry_score_macro_penalty)
            total -= penalty
            parts.append(f"macro bajista fuerte -{penalty:.0f}")
        elif signal is Signal.BUY and macro.regime == "bull" and macro.return_pct >= settings.tf_macro_strong_pct * 0.5:
            total += 10.0
            parts.append("macro alcista +10")
        elif signal is Signal.SELL and macro.blocks_sell:
            penalty = float(settings.entry_score_macro_penalty)
            total -= penalty
            parts.append(f"macro alcista fuerte -{penalty:.0f}")

    if spike is not None and spike.detected:
        move = float(spike.move_pct)
        if signal is Signal.BUY:
            if move < 0:
                penalty = float(settings.entry_score_spike_adverse_penalty)
                total -= penalty
                parts.append(f"spike bajista -{penalty:.0f}")
            elif move > 0:
                bonus = float(settings.entry_score_spike_favor_bonus)
                total += bonus
                parts.append(f"spike alcista +{bonus:.0f}")
        elif signal is Signal.SELL:
            if move > 0:
                penalty = float(settings.entry_score_spike_adverse_penalty)
                total -= penalty
                parts.append(f"spike alcista -{penalty:.0f}")
            elif move < 0:
                bonus = float(settings.entry_score_spike_favor_bonus)
                total += bonus
                parts.append(f"spike bajista +{bonus:.0f}")

    return ScoreResult(total=total, parts=tuple(parts))


def entry_allowed(result: ScoreResult, settings: Settings, symbol: str | None = None) -> bool:
    if not settings.entry_score_enabled:
        return True
    return float(result.total) >= entry_score_min_for(settings, symbol)
