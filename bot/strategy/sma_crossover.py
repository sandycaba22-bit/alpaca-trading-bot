"""Cruce de medias móviles simples (SMA fast / SMA slow)."""

from __future__ import annotations

import logging

import pandas as pd

from bot.strategy.base import Signal, Strategy, StrategyContext

logger = logging.getLogger(__name__)


class SmaCrossoverStrategy(Strategy):
    name = "sma_crossover"

    def __init__(self, fast: int = 20, slow: int = 50) -> None:
        if fast >= slow:
            raise ValueError("fast debe ser menor que slow")
        self.fast = fast
        self.slow = slow

    def generate_signal(self, ctx: StrategyContext) -> Signal:
        closes = self._closes(ctx.bars)
        if len(closes) < self.slow + 1:
            logger.debug(
                "%s: barras insuficientes (%s < %s)",
                ctx.symbol,
                len(closes),
                self.slow + 1,
            )
            return Signal.HOLD

        sma_fast = closes.rolling(self.fast).mean()
        sma_slow = closes.rolling(self.slow).mean()

        prev_fast, curr_fast = float(sma_fast.iloc[-2]), float(sma_fast.iloc[-1])
        prev_slow, curr_slow = float(sma_slow.iloc[-2]), float(sma_slow.iloc[-1])

        if any(pd.isna(x) for x in (prev_fast, curr_fast, prev_slow, curr_slow)):
            return Signal.HOLD

        golden = prev_fast <= prev_slow and curr_fast > curr_slow
        death = prev_fast >= prev_slow and curr_fast < curr_slow

        last_px = ctx.last_price if ctx.last_price is not None else float(closes.iloc[-1])
        logger.debug(
            "%s | SMA%d=%.4f SMA%d=%.4f | close=%.4f last=%.4f | long=%s",
            ctx.symbol,
            self.fast,
            curr_fast,
            self.slow,
            curr_slow,
            float(closes.iloc[-1]),
            last_px,
            ctx.has_long_position,
        )

        if golden and not ctx.has_long_position:
            logger.info("%s | cruce alcista (golden cross) -> BUY", ctx.symbol)
            return Signal.BUY
        if death and ctx.has_long_position:
            logger.info("%s | cruce bajista (death cross) -> SELL", ctx.symbol)
            return Signal.SELL
        return Signal.HOLD

    @staticmethod
    def _closes(bars: pd.DataFrame) -> pd.Series:
        if "close" not in bars.columns:
            raise ValueError("El DataFrame de barras debe incluir la columna 'close'")
        return bars["close"].astype(float)
