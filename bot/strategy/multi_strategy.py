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
        regime = select_regime(ctx.symbol, ctx.bars, self.settings)
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

        if StrategyId.BREAKOUT in enabled:
            raw, detail = detect_breakout(
                ctx.bars,
                lookback=self.breakout.lookback_for(ctx.symbol),
                volume_mult=self.breakout.volume_mult_for(ctx.symbol),
                min_range_atr_mult=self.breakout.min_range_mult_for(ctx.symbol),
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
            )
            if raw is not Signal.HOLD:
                candidates.append(
                    StrategyPick(raw, StrategyId.TREND_PULLBACK, detail, regime, None)
                )

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
            return StrategyPick(
                Signal.HOLD,
                None,
                f"{regime.regime.value} | {regime.reason} | sin señal",
                regime,
                None,
            )

        picked = self._resolve(candidates)
        logger.info(
            "%s | pick=%s %s | regimen=%s | candidatas=%s",
            ctx.symbol,
            picked.strategy.value if picked.strategy else "none",
            picked.signal.value,
            regime.regime.value,
            ",".join(f"{c.strategy.value}:{c.signal.value}" for c in candidates),
        )
        return picked

    def _resolve(self, candidates: list[StrategyPick]) -> StrategyPick:
        if len(candidates) == 1:
            return candidates[0]
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
