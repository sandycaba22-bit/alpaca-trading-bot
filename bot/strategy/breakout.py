"""Disparador de ruptura (breakout) con volumen y tamaño mínimo vs ATR."""

from __future__ import annotations

import logging

import pandas as pd

from bot.config import Settings
from bot.market.assets import is_crypto_symbol
from bot.strategy.base import Signal, Strategy, StrategyContext
from bot.strategy.indicators import last_atr, volume_vs_average

logger = logging.getLogger(__name__)


def detect_breakout(
    bars: pd.DataFrame,
    *,
    lookback: int,
    volume_mult: float,
    min_range_atr_mult: float,
    atr_period: int,
    has_long: bool,
    symbol: str = "",
) -> tuple[Signal, str]:
    """
    Ruptura por CIERRE fuera del rango de las N velas previas.

    No cuenta mecha: el close tiene que superar el máximo o el mínimo.
    Volumen de la vela > media reciente * volume_mult.
    Rango high-low de la vela >= min_range_atr_mult * ATR.
    """
    if bars is None or bars.empty or lookback < 2:
        return Signal.HOLD, "sin barras para ruptura"
    needed = {"high", "low", "close"}
    if not needed.issubset(bars.columns):
        return Signal.HOLD, "barras sin OHLC"
    if len(bars) < lookback + 1:
        return Signal.HOLD, f"barras insuficientes para ruptura ({len(bars)} < {lookback + 1})"

    prior = bars.iloc[-(lookback + 1) : -1]
    current = bars.iloc[-1]
    close = float(current["close"])
    high = float(current["high"])
    low = float(current["low"])
    range_high = float(prior["high"].max())
    range_low = float(prior["low"].min())
    if not (range_high > 0 and range_low > 0):
        return Signal.HOLD, "rango de consolidación inválido"

    bullish = close > range_high
    bearish = close < range_low
    if not bullish and not bearish:
        return Signal.HOLD, (
            f"sin ruptura | close={close:.4f} rango={range_low:.4f}-{range_high:.4f}"
        )

    vol_ok, vol_ratio = volume_vs_average(bars, lookback, volume_mult)
    if vol_ok is False:
        return Signal.HOLD, (
            f"ruptura sin volumen ({vol_ratio:.2f}x < {volume_mult:.2f}x)"
        )
    if vol_ok is None:
        logger.debug("%s | ruptura sin dato de volumen — se exige el resto de filtros", symbol)

    atr_value = last_atr(bars, atr_period)
    bar_range = high - low
    if atr_value and atr_value > 0:
        min_range = min_range_atr_mult * atr_value
        if bar_range < min_range:
            return Signal.HOLD, (
                f"ruptura de vela chica | rango={bar_range:.4f} < {min_range_atr_mult:.2f}x ATR "
                f"({atr_value:.4f})"
            )

    if bullish and not has_long:
        vol_txt = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
        why = (
            f"ruptura alcista close={close:.4f} > max={range_high:.4f} "
            f"| vol={vol_txt} | rango_vela={bar_range:.4f}"
        )
        logger.info("%s | %s -> BUY", symbol or "?", why)
        return Signal.BUY, why
    if bearish and has_long:
        vol_txt = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
        why = (
            f"ruptura bajista close={close:.4f} < min={range_low:.4f} "
            f"| vol={vol_txt} | rango_vela={bar_range:.4f}"
        )
        logger.info("%s | %s -> SELL", symbol or "?", why)
        return Signal.SELL, why
    if bullish and has_long:
        return Signal.HOLD, "ruptura alcista ignorada — ya hay posición"
    return Signal.HOLD, "ruptura bajista ignorada — sin posición que cerrar"


class BreakoutStrategy(Strategy):
    """Disparador de entrada: ruptura + volumen. SMA/ADX viven en SignalFilterLayer."""

    name = "breakout_volume"

    def __init__(
        self,
        settings: Settings,
        sma_slow_by_symbol: dict[str, int] | None = None,
    ) -> None:
        self.settings = settings
        self.sma_slow_by_symbol = {
            str(sym).upper(): int(slow)
            for sym, slow in (sma_slow_by_symbol or {}).items()
            if int(slow) >= 2
        }
        self.last_detail = ""

    def slow_period(self, symbol: str) -> int:
        key = str(symbol).upper()
        if key in self.sma_slow_by_symbol:
            return self.sma_slow_by_symbol[key]
        if is_crypto_symbol(key):
            return int(self.settings.crypto_sma_slow)
        return int(self.settings.sma_slow)

    def lookback_for(self, symbol: str) -> int:
        return int(
            self.settings.breakout_lookback_overrides.get(
                str(symbol).upper(), self.settings.breakout_lookback_periods
            )
        )

    def volume_mult_for(self, symbol: str) -> float:
        from bot.market.assets import is_crypto_symbol

        key = str(symbol).upper()
        if key in self.settings.breakout_volume_mult_overrides:
            return float(self.settings.breakout_volume_mult_overrides[key])
        if is_crypto_symbol(symbol):
            return float(self.settings.crypto_breakout_volume_mult)
        return float(self.settings.breakout_volume_mult)

    def min_range_mult_for(self, symbol: str) -> float:
        return float(
            self.settings.breakout_min_range_overrides.get(
                str(symbol).upper(), self.settings.breakout_min_range_atr_mult
            )
        )

    def generate_signal(self, ctx: StrategyContext) -> Signal:
        signal, detail = detect_breakout(
            ctx.bars,
            lookback=self.lookback_for(ctx.symbol),
            volume_mult=self.volume_mult_for(ctx.symbol),
            min_range_atr_mult=self.min_range_mult_for(ctx.symbol),
            atr_period=self.settings.atr_period,
            has_long=ctx.has_long_position,
            symbol=ctx.symbol,
        )
        self.last_detail = detail
        return signal
