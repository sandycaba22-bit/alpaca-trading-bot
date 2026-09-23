"""Cripto asimétrico 1H entrada + confirmación 4H.

Entrada: ruptura lookback en 1H + ATR > percentil + tendencia alcista 4H (SMA/ADX/ATR sin escalar al mínimo).
Salida (backtest / live vía risk): SL ceñido ATR, trailing ancho ATR, sin TP fijo %.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from bot.config import Settings
from bot.strategy.indicators import atr as atr_series
from bot.strategy.multi_tf_analysis import analyze_trend


@dataclass(frozen=True)
class CryptoAsymmetricParams:
    """Períodos completos en velas 1H."""

    entry_timeframe: str = "1Hour"
    confirm_timeframe: str = "4H"
    breakout_lookback: int = 20
    sma_fast: int = 9
    sma_slow: int = 21
    adx_period: int = 14
    atr_period: int = 14
    atr_percentile_window: int = 100
    atr_percentile: float = 75.0
    htf_fast: int = 9
    htf_slow: int = 21
    max_trades_per_day: int = 3
    sl_atr_mult: float = 1.2
    trail_atr_mult: float = 2.75
    trail_activate_atr_mult: float = 1.25
    double_breakout: bool = False


def params_from_settings(settings: Settings) -> CryptoAsymmetricParams:
    return CryptoAsymmetricParams(
        breakout_lookback=int(settings.crypto_asymmetric_breakout_lookback),
        sma_fast=int(settings.crypto_sma_fast),
        sma_slow=int(settings.crypto_sma_slow),
        adx_period=int(settings.adx_period),
        atr_period=int(settings.atr_period),
        atr_percentile_window=int(settings.crypto_asymmetric_atr_pct_window),
        atr_percentile=float(settings.crypto_asymmetric_atr_percentile),
        htf_fast=int(settings.crypto_asymmetric_htf_fast),
        htf_slow=int(settings.crypto_asymmetric_htf_slow),
        max_trades_per_day=int(settings.crypto_asymmetric_max_trades_per_day),
        sl_atr_mult=float(settings.crypto_asymmetric_sl_atr_mult),
        trail_atr_mult=float(settings.crypto_asymmetric_trail_atr_mult),
        trail_activate_atr_mult=float(settings.crypto_asymmetric_trail_activate_atr_mult),
        double_breakout=bool(settings.crypto_asymmetric_double_breakout),
    )


def resample_4h_from_1h(bars_1h: pd.DataFrame) -> pd.DataFrame:
    if bars_1h.empty:
        return bars_1h
    agg: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in bars_1h.columns:
        agg["volume"] = "sum"
    return bars_1h.resample("4h", label="right", closed="right").agg(agg).dropna(how="any")


def _htf_until(htf: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    if htf is None or htf.empty:
        return htf
    return htf.loc[htf.index <= ts]


def _breakout_at(bars: pd.DataFrame, i: int, lookback: int) -> bool:
    if i < lookback + 1:
        return False
    prev_close = float(bars["close"].iloc[i - 1])
    window = bars["high"].iloc[i - 1 - lookback : i - 1]
    if window.empty:
        return False
    return prev_close > float(window.max())


@dataclass(frozen=True)
class CryptoAsymmetricSignal:
    allowed: bool
    reason: str
    atr_value: float | None = None


def evaluate_entry_at_bar(
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    bar_index: int,
    params: CryptoAsymmetricParams,
    *,
    has_long: bool,
    trades_today: int,
) -> CryptoAsymmetricSignal:
    """Evalúa entrada en el índice `bar_index` (apertura de esa vela)."""
    if has_long:
        return CryptoAsymmetricSignal(False, "ya hay posición larga")
    if trades_today >= params.max_trades_per_day:
        return CryptoAsymmetricSignal(False, "máximo entradas del día")

    i = bar_index
    warmup = max(
        params.breakout_lookback + 2,
        params.atr_period + params.atr_percentile_window,
        params.htf_slow + 2,
    )
    if i < warmup:
        return CryptoAsymmetricSignal(False, "warmup insuficiente")

    if not _breakout_at(bars_1h, i, params.breakout_lookback):
        return CryptoAsymmetricSignal(False, "sin ruptura 1H")
    if params.double_breakout and not _breakout_at(bars_1h, i - 1, params.breakout_lookback):
        return CryptoAsymmetricSignal(False, "doble ruptura no cumplida")

    atr_s = atr_series(bars_1h, params.atr_period)
    atr_val = float(atr_s.iloc[i - 1]) if pd.notna(atr_s.iloc[i - 1]) else 0.0
    if atr_val <= 0:
        return CryptoAsymmetricSignal(False, "ATR no disponible")
    atr_window = atr_s.iloc[max(0, i - 1 - params.atr_percentile_window) : i].dropna()
    if len(atr_window) < params.atr_period + 5:
        return CryptoAsymmetricSignal(False, "ventana ATR corta")
    if atr_val <= float(np.percentile(atr_window, params.atr_percentile)):
        return CryptoAsymmetricSignal(False, "ATR bajo percentil")

    ts = bars_1h.index[i - 1]
    htf_hist = _htf_until(bars_4h, ts)
    trend, _ = analyze_trend(htf_hist, fast=params.htf_fast, slow=params.htf_slow)
    if trend != "bull":
        return CryptoAsymmetricSignal(False, f"4H no alcista ({trend})")

    return CryptoAsymmetricSignal(
        True,
        f"asim 1H BUY | ruptura {params.breakout_lookback} | ATR>{params.atr_percentile:.0f}pct | 4H bull",
        atr_value=atr_val,
    )


def evaluate_entry_live(
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    params: CryptoAsymmetricParams,
    *,
    has_long: bool,
    trades_today: int,
) -> CryptoAsymmetricSignal:
    if bars_1h.empty:
        return CryptoAsymmetricSignal(False, "sin velas 1H")
    return evaluate_entry_at_bar(
        bars_1h,
        bars_4h,
        len(bars_1h),
        params,
        has_long=has_long,
        trades_today=trades_today,
    )
