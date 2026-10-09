"""Entrada de tendencia cuando el ATR% 1H está por encima del techo del sweep.

No afloja el sweep. Solo abre long paper si el cierre 4H está sobre la SMA50
y el ATR 1H se expande por encima de 1.20x frente a las 20 horas previas.
"""

from __future__ import annotations

import pandas as pd

from bot.strategy.indicators import atr, sma
from strategies.crypto_night.types import SweepSetup, TradeSide

STRATEGY_TAG = "trend_4h_sma50_atr_exp_1.2"
SMA_BARS = 50
ATR_PERIOD = 14
ATR_EXPANSION_BARS = 20
ATR_EXPANSION_MIN = 1.20


def htf_close_above_sma50(bars_4h: pd.DataFrame) -> tuple[bool, float | None]:
    if bars_4h is None or bars_4h.empty or len(bars_4h) < SMA_BARS + 1:
        return False, None
    closes = bars_4h["close"].astype(float)
    slow = sma(closes, SMA_BARS)
    last_sma = float(slow.iloc[-1])
    last_close = float(closes.iloc[-1])
    if not pd.notna(last_sma) or last_sma <= 0:
        return False, None
    return last_close > last_sma, last_sma


def atr_expansion_ratio(bars_1h: pd.DataFrame) -> float | None:
    """ATR de la última hora / media de las 20 horas anteriores. None si no hay historia."""
    if bars_1h is None or bars_1h.empty:
        return None
    series = atr(bars_1h, ATR_PERIOD)
    need = ATR_PERIOD + ATR_EXPANSION_BARS + 2
    if len(series) < need:
        return None
    current = float(series.iloc[-1])
    window = series.iloc[-(ATR_EXPANSION_BARS + 1) : -1]
    avg = float(window.mean())
    if avg <= 0 or not pd.notna(current):
        return None
    return current / avg


def evaluate_high_vol_trend(
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    bars_15m: pd.DataFrame,
    *,
    symbol: str,
) -> tuple[SweepSetup | None, str]:
    """Señal buy solo con 4H sobre SMA50 y expansión ATR por encima de 1.20. Si no, hold."""
    htf_bull, sma_value = htf_close_above_sma50(bars_4h)
    ratio = atr_expansion_ratio(bars_1h)
    ratio_txt = f"{ratio:.2f}" if ratio is not None else "n/a"
    sma_txt = f"{sma_value:.2f}" if sma_value is not None else "n/a"
    base = (
        f"tendencia 4H | htf_bull={str(htf_bull).lower()} | sma50={sma_txt} | "
        f"atr_ratio={ratio_txt}"
    )
    if not htf_bull or ratio is None or ratio <= ATR_EXPANSION_MIN:
        return None, f"{base} | señal=hold"
    if bars_15m is None or bars_15m.empty:
        return None, f"{base} | señal=hold | sin velas 15m"

    last = float(bars_15m["close"].astype(float).iloc[-1])
    if last <= 0:
        return None, f"{base} | señal=hold | precio invalido"
    series = atr(bars_1h, ATR_PERIOD)
    atr_1h = float(series.iloc[-1]) if len(series) else 0.0
    stop = last - max(atr_1h, last * 0.002)
    vol = bars_15m["volume"].astype(float)
    vol_ma = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else 0.0
    volume_ratio = float(vol.iloc[-1]) / vol_ma if vol_ma > 0 else 0.0
    setup = SweepSetup(
        side=TradeSide.LONG,
        symbol=symbol,
        sweep_extreme=stop,
        reclaim_bar_idx=len(bars_15m) - 1,
        limit_price=last,
        stop_price=stop,
        atr_1h=atr_1h,
        volume_ratio=volume_ratio,
        variant=STRATEGY_TAG,
        meta={"htf_bull": True, "atr_ratio": ratio, "sma50": sma_value},
    )
    return setup, f"{base} | señal=buy"
