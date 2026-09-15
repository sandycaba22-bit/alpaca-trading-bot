"""Análisis por capa Tesla 3-6-9: spike 3m → señal 6m → macro/tendencia 9m."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.strategy.base import Signal, Strategy, StrategyContext
from bot.strategy.indicators import classify_regime, last_atr, momentum_pct, sma


@dataclass(frozen=True)
class SpikeSnapshot:
    detected: bool
    move_pct: float
    reason: str


@dataclass(frozen=True)
class MacroSnapshot:
    regime: str
    return_pct: float
    blocks_buy: bool
    blocks_sell: bool
    reason: str


@dataclass(frozen=True)
class SymbolLayers:
    symbol: str
    spike: SpikeSnapshot
    signal: Signal
    signal_detail: str
    structure: str
    structure_return_pct: float
    trend: str
    trend_return_pct: float
    macro: MacroSnapshot


def detect_spike(
    bars_1m: pd.DataFrame,
    last_price: float,
    threshold_pct: float = 0.005,
    lookback_bars: int = 3,
    window_label: str | None = None,
) -> SpikeSnapshot:
    """Detecta movimiento brusco en ventana de ~3 minutos (velas 1m)."""
    if bars_1m.empty or len(bars_1m) < lookback_bars + 1 or last_price <= 0:
        return SpikeSnapshot(False, 0.0, "sin datos 3m")
    ref = float(bars_1m["close"].iloc[-(lookback_bars + 1)])
    if ref <= 0:
        return SpikeSnapshot(False, 0.0, "referencia invalida")
    move = (last_price - ref) / ref
    window = window_label or f"{lookback_bars} velas"
    if abs(move) >= threshold_pct:
        direction = "alcista" if move > 0 else "bajista"
        return SpikeSnapshot(
            True,
            move * 100.0,
            f"movimiento {direction} {move:+.2%} en {window}",
        )
    return SpikeSnapshot(False, move * 100.0, "rango normal")


def analyze_signal(
    strategy: Strategy,
    symbol: str,
    bars: pd.DataFrame,
    has_long: bool,
    last_price: float,
    spread_pct: float | None,
    htf_trend: str | None = None,
) -> tuple[Signal, str]:
    if bars.empty:
        return Signal.HOLD, "sin barras"
    ctx = StrategyContext(
        symbol=symbol,
        bars=bars,
        has_long_position=has_long,
        has_short_position=False,
        last_price=last_price,
        spread_pct=spread_pct,
        momentum_pct=momentum_pct(bars["close"], 5),
        atr=last_atr(bars, 14),
        htf_trend=htf_trend,
    )
    signal = strategy.generate_signal(ctx)
    detail = str(getattr(strategy, "last_detail", "") or "").strip()
    if signal is Signal.HOLD:
        return signal, detail or "sin ruptura"
    return signal, detail or f"estrategia {strategy.name} -> {signal.value}"


def analyze_structure(bars: pd.DataFrame, lookback: int = 12) -> tuple[str, float]:
    if bars.empty or len(bars) < lookback + 1:
        return "sideways", 0.0
    closes = bars["close"]
    ret = float(closes.iloc[-1] / closes.iloc[-(lookback + 1)] - 1)
    regime = classify_regime(closes, lookback=lookback, threshold=0.02)
    return regime, ret * 100.0


def analyze_trend(bars: pd.DataFrame, fast: int = 5, slow: int = 13) -> tuple[str, float]:
    if bars.empty or len(bars) < slow + 1:
        return "sideways", 0.0
    closes = bars["close"]
    fast_ma = sma(closes, fast)
    slow_ma = sma(closes, slow)
    if pd.isna(fast_ma.iloc[-1]) or pd.isna(slow_ma.iloc[-1]):
        return "sideways", 0.0
    ret = float(closes.iloc[-1] / closes.iloc[-(slow + 1)] - 1) * 100.0
    if float(fast_ma.iloc[-1]) > float(slow_ma.iloc[-1]):
        return "bull", ret
    if float(fast_ma.iloc[-1]) < float(slow_ma.iloc[-1]):
        return "bear", ret
    return "sideways", ret


def analyze_macro(
    bars: pd.DataFrame,
    strong_threshold_pct: float = 2.5,
    lookback: int = 20,
    timeframe_label: str = "9Min",
) -> MacroSnapshot:
    if bars.empty or len(bars) < lookback:
        return MacroSnapshot("sideways", 0.0, False, False, "macro sin datos")

    closes = bars["close"]
    ret_pct = float(closes.iloc[-1] / closes.iloc[-lookback] - 1) * 100.0
    regime = classify_regime(
        closes,
        lookback=min(12, len(closes) - 1),
        threshold=0.025,
    )
    strong = abs(ret_pct) >= strong_threshold_pct
    blocks_buy = regime == "bear" and strong
    blocks_sell = regime == "bull" and strong
    reason = f"macro {timeframe_label} {regime} {ret_pct:+.1f}%"
    if blocks_buy:
        reason += " | bloquea compras"
    if blocks_sell:
        reason += " | bloquea ventas contra tendencia"
    return MacroSnapshot(regime, ret_pct, blocks_buy, blocks_sell, reason)


def macro_allows(signal: Signal, macro: MacroSnapshot) -> tuple[bool, str]:
    if signal is Signal.HOLD:
        return True, "hold"
    if signal is Signal.BUY and macro.blocks_buy:
        return False, macro.reason
    if signal is Signal.SELL and macro.blocks_sell:
        return False, macro.reason
    return True, "macro OK"
