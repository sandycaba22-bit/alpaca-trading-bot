"""Filtros de entrada cripto (research): tendencia HTF + expansión ATR.

Capa previa al scan asimétrico 1H/4H (`evaluate_entry_at_bar`). No altera salidas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from bot.strategy.crypto_asymmetric import (
    CryptoAsymmetricParams,
    CryptoAsymmetricSignal,
    _htf_until,
    evaluate_entry_at_bar,
)
from bot.strategy.indicators import atr as atr_series
from bot.strategy.multi_tf_analysis import analyze_trend

REGIME_STRATEGY_TAG = "trend_4h_sma50_atr_exp_1.2"


def resample_1d_from_1h(bars_1h: pd.DataFrame) -> pd.DataFrame:
    if bars_1h is None or bars_1h.empty:
        return bars_1h
    agg: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in bars_1h.columns:
        agg["volume"] = "sum"
    return bars_1h.resample("1D", label="right", closed="right").agg(agg).dropna(how="any")


@dataclass(frozen=True)
class CryptoRegimeGate:
    """Configuración de filtros previos a la entrada asimétrica."""

    name: str
    htf_mode: Literal["none", "4h", "1d"] = "4h"
    htf_fast: int = 9
    htf_slow: int = 21
    atr_expansion_mult: float | None = 1.2
    atr_expansion_period: int = 20
    atr_period: int = 14


GATE_ETH_SMA50_ATR12 = CryptoRegimeGate(
    name=REGIME_STRATEGY_TAG,
    htf_mode="4h",
    htf_fast=9,
    htf_slow=50,
    atr_expansion_mult=1.2,
    atr_expansion_period=20,
)


def htf_trend_at(
    bars_4h: pd.DataFrame,
    bars_1d: pd.DataFrame,
    ts: pd.Timestamp,
    gate: CryptoRegimeGate,
) -> tuple[bool, str]:
    if gate.htf_mode == "none":
        return True, "HTF off"
    if gate.htf_mode == "4h":
        htf = _htf_until(bars_4h, ts)
        label = "4H"
    else:
        htf = _htf_until(bars_1d, ts)
        label = "1D"
    if htf is None or htf.empty or len(htf) < gate.htf_slow + 3:
        return False, f"{label} insuficiente"
    trend, detail = analyze_trend(htf, fast=gate.htf_fast, slow=gate.htf_slow)
    if trend != "bull":
        return False, f"{label} no alcista ({trend})"
    return True, f"{label} bull | {detail}"


def atr_expansion_at_series(
    atr_s: pd.Series,
    entry_bar_index: int,
    gate: CryptoRegimeGate,
) -> tuple[bool, str]:
    """Expansión ATR en la barra de señal (entry_bar_index - 1), coherente con hist = bars[:entry]."""
    mult = gate.atr_expansion_mult
    if mult is None or mult <= 0:
        return True, "ATR exp off"
    sig_i = entry_bar_index - 1
    if sig_i < max(gate.atr_period + gate.atr_expansion_period + 2, 30):
        return False, "warmup ATR exp"
    if sig_i >= len(atr_s) or sig_i < gate.atr_expansion_period + 1:
        return False, "ATR exp ventana corta"
    current = float(atr_s.iloc[sig_i])
    avg = float(atr_s.iloc[sig_i - gate.atr_expansion_period : sig_i].mean())
    if avg <= 0 or not pd.notna(current):
        return False, "ATR exp invalido"
    ratio = current / avg
    if current <= float(mult) * avg:
        return False, f"ATR sin expansion ({ratio:.2f}x < {mult:.2f}x)"
    return True, f"ATR expansion {ratio:.2f}x >= {mult:.2f}x"


def atr_expansion_at(
    bars_1h: pd.DataFrame,
    bar_index: int,
    gate: CryptoRegimeGate,
) -> tuple[bool, str]:
    mult = gate.atr_expansion_mult
    if mult is None or mult <= 0:
        return True, "ATR exp off"
    atr_s = atr_series(bars_1h, gate.atr_period)
    return atr_expansion_at_series(atr_s, bar_index, gate)


def evaluate_regime_entry_live(
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    params: CryptoAsymmetricParams,
    gate: CryptoRegimeGate,
    *,
    has_long: bool,
    trades_today: int,
) -> CryptoAsymmetricSignal:
    """Live: gates régimen + entrada asimétrica 1H/4H (misma base que backtest)."""
    if bars_1h.empty:
        return CryptoAsymmetricSignal(False, f"{REGIME_STRATEGY_TAG} | sin velas 1H")
    i = len(bars_1h)
    ts = bars_1h.index[i - 1]
    bars_1d = resample_1d_from_1h(bars_1h)
    ok_t, msg_t = htf_trend_at(bars_4h, bars_1d, ts, gate)
    if not ok_t:
        return CryptoAsymmetricSignal(False, f"{REGIME_STRATEGY_TAG} | {msg_t}")
    atr_s = atr_series(bars_1h, gate.atr_period)
    ok_a, msg_a = atr_expansion_at_series(atr_s, i, gate)
    if not ok_a:
        return CryptoAsymmetricSignal(False, f"{REGIME_STRATEGY_TAG} | {msg_a}")
    base = evaluate_entry_at_bar(
        bars_1h,
        bars_4h,
        i,
        params,
        has_long=has_long,
        trades_today=trades_today,
    )
    if not base.allowed:
        return CryptoAsymmetricSignal(False, f"{REGIME_STRATEGY_TAG} | {base.reason}")
    return CryptoAsymmetricSignal(
        True,
        f"{REGIME_STRATEGY_TAG} | {msg_t} | {msg_a} | {base.reason}",
        atr_value=base.atr_value,
    )
