"""Cruce de medias móviles simples (SMA fast / SMA slow)."""

from __future__ import annotations

import logging

import pandas as pd

from bot.strategy.base import Signal, Strategy, StrategyContext

logger = logging.getLogger(__name__)

# Impulso en TF de entrada (no sustituye el SMA lento; solo entra si la tendencia HTF acompaña).
IMPULSE_EMA_FAST = 5
IMPULSE_EMA_SLOW = 13
IMPULSE_RSI_PERIOD = 7
IMPULSE_RSI_BUY = 55.0
IMPULSE_RSI_SELL = 45.0
IMPULSE_ACCEL_BARS = 2
IMPULSE_ACCEL_PCT = 0.004


def _rsi(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def generate_impulse_signal(
    closes: pd.Series,
    *,
    has_long: bool,
    trend: str,
) -> tuple[Signal, str]:
    """
    Disparador de impulso: EMA 5/13, cruce RSI o aceleración de precio.

    Solo dispara si la tendencia HTF acompaña (bull → BUY, bear → SELL).
    """
    trend_n = str(trend or "").strip().lower()
    min_bars = max(IMPULSE_EMA_SLOW, IMPULSE_RSI_PERIOD, IMPULSE_ACCEL_BARS) + 2
    if len(closes) < min_bars:
        return Signal.HOLD, ""

    px = closes.astype(float)
    ema_fast = px.ewm(span=IMPULSE_EMA_FAST, adjust=False).mean()
    ema_slow = px.ewm(span=IMPULSE_EMA_SLOW, adjust=False).mean()
    rsi = _rsi(px, IMPULSE_RSI_PERIOD)

    prev_f, curr_f = float(ema_fast.iloc[-2]), float(ema_fast.iloc[-1])
    prev_s, curr_s = float(ema_slow.iloc[-2]), float(ema_slow.iloc[-1])
    prev_rsi, curr_rsi = float(rsi.iloc[-2]), float(rsi.iloc[-1])
    if any(pd.isna(x) for x in (prev_f, curr_f, prev_s, curr_s, prev_rsi, curr_rsi)):
        return Signal.HOLD, ""

    ema_golden = prev_f <= prev_s and curr_f > curr_s
    ema_death = prev_f >= prev_s and curr_f < curr_s
    rsi_up = prev_rsi < IMPULSE_RSI_BUY <= curr_rsi
    rsi_down = prev_rsi > IMPULSE_RSI_SELL >= curr_rsi
    ref = float(px.iloc[-(IMPULSE_ACCEL_BARS + 1)])
    accel = ((float(px.iloc[-1]) - ref) / ref) if ref > 0 else 0.0
    accel_up = accel >= IMPULSE_ACCEL_PCT and curr_rsi >= IMPULSE_RSI_BUY and curr_f > curr_s
    accel_down = (
        accel <= -IMPULSE_ACCEL_PCT and curr_rsi <= IMPULSE_RSI_SELL and curr_f < curr_s
    )

    if trend_n == "bull" and not has_long and (ema_golden or rsi_up or accel_up):
        if ema_golden:
            why = "impulso EMA5/13 golden"
        elif rsi_up:
            why = f"impulso RSI{IMPULSE_RSI_PERIOD} cruza {IMPULSE_RSI_BUY:.0f}"
        else:
            why = f"impulso aceleracion {accel:+.2%}"
        return Signal.BUY, why

    if trend_n == "bear" and has_long and (ema_death or rsi_down or accel_down):
        if ema_death:
            why = "impulso EMA5/13 death"
        elif rsi_down:
            why = f"impulso RSI{IMPULSE_RSI_PERIOD} cruza {IMPULSE_RSI_SELL:.0f}"
        else:
            why = f"impulso aceleracion {accel:+.2%}"
        return Signal.SELL, why

    return Signal.HOLD, ""


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

        # Impulso EMA/RSI/aceleración (HTF vía generate_impulse_signal); si no hay cruce SMA, HOLD.
        return Signal.HOLD

    @staticmethod
    def _closes(bars: pd.DataFrame) -> pd.Series:
        if "close" not in bars.columns:
            raise ValueError("El DataFrame de barras debe incluir la columna 'close'")
        return bars["close"].astype(float)


class TunedSmaStrategy(Strategy):
    """SMA por símbolo usando los periodos ganadores del walk-forward."""

    name = "sma_crossover_tuned"

    def __init__(
        self,
        default_fast: int,
        default_slow: int,
        by_symbol: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self._default = SmaCrossoverStrategy(default_fast, default_slow)
        self._by_symbol = {
            symbol.upper(): SmaCrossoverStrategy(fast, slow)
            for symbol, (fast, slow) in (by_symbol or {}).items()
            if fast < slow
        }

    def generate_signal(self, ctx: StrategyContext) -> Signal:
        strategy = self._by_symbol.get(ctx.symbol.upper(), self._default)
        return strategy.generate_signal(ctx)

    def slow_period(self, symbol: str) -> int:
        strategy = self._by_symbol.get(str(symbol).upper(), self._default)
        return int(strategy.slow)
