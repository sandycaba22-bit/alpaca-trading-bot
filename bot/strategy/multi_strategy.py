"""Orquesta las estrategias según el régimen. No modifica breakout.py."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.config import Settings
from bot.storage.breakout_state import BreakoutStateStore
from bot.strategy.base import Signal, Strategy, StrategyContext
from bot.strategy.breakout import BreakoutStrategy, detect_breakout
from bot.strategy.mean_reversion import detect_mean_reversion
from bot.strategy.pullback import detect_pullback
from bot.strategy.trend_pullback import detect_trend_pullback
from bot.strategy.regime_selector import (
    MarketRegime,
    RegimeSnapshot,
    StrategyId,
    select_regime,
)
from bot.strategy.signal_filters import SignalFilterLayer
from bot.strategy.entry_score import score_strategy_candidate
from bot.strategy.squeeze import detect_squeeze

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyPick:
    signal: Signal
    strategy: StrategyId | None
    detail: str
    regime: RegimeSnapshot | None
    sl_mult: float | None


class MultiStrategyOrchestrator(Strategy):
    name = "multi_regime"

    def __init__(
        self,
        settings: Settings,
        sma_slow_by_symbol: dict[str, int] | None = None,
        filters: SignalFilterLayer | None = None,
    ) -> None:
        self.settings = settings
        self.breakout = BreakoutStrategy(settings, sma_slow_by_symbol)
        self.filters = filters or SignalFilterLayer(settings, BreakoutStateStore(persist=False))
        self.last_detail = ""
        self.last_strategy: StrategyId | None = None
        self.last_sl_mult: float | None = None
        self.last_regime: RegimeSnapshot | None = None
        self.force_strategy: StrategyId | None = None
        self.enable_trend_pullback = True

    def slow_period(self, symbol: str) -> int:
        return self.breakout.slow_period(symbol)

    def generate_signal(self, ctx: StrategyContext) -> Signal:
        pick = self.evaluate(ctx)
        self.last_detail = pick.detail
        self.last_strategy = pick.strategy
        self.last_sl_mult = pick.sl_mult
        self.last_regime = pick.regime
        return pick.signal

    def evaluate(self, ctx: StrategyContext) -> StrategyPick:
        regime = select_regime(
            ctx.symbol,
            ctx.bars,
            self.settings,
            htf_trend=ctx.htf_trend,
        )
        bb_value = (
            f"{regime.bb_width:.4f}/{regime.bb_width_avg:.4f}"
            if regime.bb_width is not None and regime.bb_width_avg is not None
            else "n/a"
        )
        adx_value = f"{regime.adx:.2f}" if regime.adx is not None else "n/a"
        logger.info(
            "%s | regimen=%s | razon=%s | ADX=%s | BB=%s",
            ctx.symbol,
            regime.regime.value,
            regime.reason,
            adx_value,
            bb_value,
        )
        enabled = regime.enabled
        if self.force_strategy is not None:
            if self.force_strategy not in enabled:
                return StrategyPick(
                    Signal.HOLD,
                    None,
                    f"{regime.regime.value} | {self.force_strategy.value} apagada en este regimen",
                    regime,
                    None,
                )
            enabled = (self.force_strategy,)
        candidates: list[StrategyPick] = []
        holds: list[str] = []
        htf_bull = str(ctx.htf_trend or "").strip().lower() == "bull"
        trend_relax = regime.regime is MarketRegime.TREND and htf_bull

        if StrategyId.BREAKOUT in enabled:
            lookback = self.breakout.lookback_for(ctx.symbol)
            volume_mult = self.breakout.volume_mult_for(ctx.symbol)
            min_range = self.breakout.min_range_mult_for(ctx.symbol)
            if trend_relax:
                lookback = max(8, lookback // 2)
                volume_mult = max(1.0, volume_mult * 0.80)
                min_range = max(0.25, min_range * 0.80)
            raw, detail = detect_breakout(
                ctx.bars,
                lookback=lookback,
                volume_mult=volume_mult,
                min_range_atr_mult=min_range,
                atr_period=self.settings.atr_period,
                has_long=ctx.has_long_position,
                symbol=ctx.symbol,
            )
            if raw is not Signal.HOLD:
                if self.filters is not None:
                    bias = self.filters.check_sma_bias(
                        ctx.symbol, raw, ctx.bars, self.slow_period(ctx.symbol)
                    )
                    if not bias.allowed:
                        logger.info("%s | ruptura filtrada | %s", ctx.symbol, bias.reason)
                        raw = Signal.HOLD
                        detail = f"{detail} | {bias.reason}"
                if raw is not Signal.HOLD:
                    candidates.append(
                        StrategyPick(raw, StrategyId.BREAKOUT, detail, regime, None)
                    )
                else:
                    holds.append(detail)
            else:
                holds.append(detail)

        if StrategyId.PULLBACK in enabled:
            raw, detail = detect_pullback(
                ctx.bars,
                settings=self.settings,
                has_long=ctx.has_long_position,
                slow_period=self.slow_period(ctx.symbol),
                symbol=ctx.symbol,
            )
            if raw is not Signal.HOLD:
                candidates.append(
                    StrategyPick(raw, StrategyId.PULLBACK, detail, regime, None)
                )
            else:
                holds.append(detail)

        want_trend_pb = (
            self.enable_trend_pullback or self.force_strategy is StrategyId.TREND_PULLBACK
        )
        if want_trend_pb and StrategyId.TREND_PULLBACK in enabled:
            raw, detail = detect_trend_pullback(
                ctx.bars,
                settings=self.settings,
                has_long=ctx.has_long_position,
                symbol=ctx.symbol,
                htf_trend=ctx.htf_trend,
                slow_period=self.slow_period(ctx.symbol),
            )
            if raw is not Signal.HOLD:
                candidates.append(
                    StrategyPick(raw, StrategyId.TREND_PULLBACK, detail, regime, None)
                )
            else:
                holds.append(detail)

        if StrategyId.MEAN_REV in enabled and regime.regime in (
            MarketRegime.RANGE,
            MarketRegime.SQUEEZE,
        ):
            raw, detail = detect_mean_reversion(
                ctx.bars,
                settings=self.settings,
                has_long=ctx.has_long_position,
                symbol=ctx.symbol,
            )
            if raw is not Signal.HOLD:
                candidates.append(
                    StrategyPick(
                        raw,
                        StrategyId.MEAN_REV,
                        detail,
                        regime,
                        self.settings.meanrev_atr_sl_mult,
                    )
                )
            else:
                holds.append(detail)

        if StrategyId.SQUEEZE in enabled:
            raw, detail = detect_squeeze(
                ctx.bars,
                settings=self.settings,
                has_long=ctx.has_long_position,
                symbol=ctx.symbol,
            )
            if raw is not Signal.HOLD:
                candidates.append(
                    StrategyPick(raw, StrategyId.SQUEEZE, detail, regime, None)
                )

        if not candidates:
            extra = " | ".join(holds[:3]) if holds else "sin trigger"
            return StrategyPick(
                Signal.HOLD,
                None,
                f"{regime.regime.value} | {regime.reason} | sin señal | {extra}",
                regime,
                None,
            )

        picked = self._resolve(candidates, ctx)
        logger.info(
            "%s | pick=%s %s | regimen=%s | candidatas=%s",
            ctx.symbol,
            picked.strategy.value if picked.strategy else "none",
            picked.signal.value,
            regime.regime.value,
            ",".join(f"{c.strategy.value}:{c.signal.value}" for c in candidates),
        )
        return picked

    def _resolve(self, candidates: list[StrategyPick], ctx: StrategyContext) -> StrategyPick:
        if len(candidates) == 1:
            return candidates[0]
        mode = str(getattr(self.settings, "strategy_collision_mode", "score") or "score").lower()
        if mode == "score":
            return self._resolve_by_score(candidates, ctx)
        priority = str(self.settings.strategy_collision_priority or "breakout").lower()
        if priority == "pullback":
            order = (
                StrategyId.PULLBACK,
                StrategyId.TREND_PULLBACK,
                StrategyId.BREAKOUT,
                StrategyId.SQUEEZE,
                StrategyId.MEAN_REV,
            )
        else:
            order = (
                StrategyId.BREAKOUT,
                StrategyId.PULLBACK,
                StrategyId.TREND_PULLBACK,
                StrategyId.SQUEEZE,
                StrategyId.MEAN_REV,
            )
        by_id = {c.strategy: c for c in candidates if c.strategy is not None}
        for sid in order:
            if sid in by_id:
                if len(candidates) > 1 and sid == StrategyId.BREAKOUT:
                    logger.info(
                        "colision de estrategias — prioriza ruptura (descarta %s)",
                        ",".join(
                            c.strategy.value
                            for c in candidates
                            if c.strategy and c.strategy != StrategyId.BREAKOUT
                        ),
                    )
                return by_id[sid]
        return candidates[0]

    def _resolve_by_score(self, candidates: list[StrategyPick], ctx: StrategyContext) -> StrategyPick:
        scored: list[tuple[float, StrategyPick, str]] = []
        for pick in candidates:
            if pick.strategy is None:
                continue
            result = score_strategy_candidate(
                pick.signal,
                pick.strategy,
                pick.regime,
                ctx,
                self.settings,
            )
            scored.append((result.total, pick, result.summary()))
        if not scored:
            return candidates[0]
        scored.sort(key=lambda row: row[0], reverse=True)
        best_score, best_pick, best_detail = scored[0]
        if len(scored) > 1:
            alts = ",".join(
                f"{pick.strategy.value}:{score:.0f}"
                for score, pick, _ in scored[1:]
                if pick.strategy is not None
            )
            logger.info(
                "%s | colision estrategias — score elige %s=%.0f (descarta %s) | %s",
                ctx.symbol,
                best_pick.strategy.value if best_pick.strategy else "none",
                best_score,
                alts,
                best_detail,
            )
        return best_pick
