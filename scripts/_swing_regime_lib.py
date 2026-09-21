"""Filtros de régimen 1D + volumen/momentum sobre swing 1H/4H — offline, no producción."""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, Settings
from bot.strategy.indicators import adx as adx_series
from bot.strategy.indicators import atr as atr_series
from bot.strategy.indicators import ema, rsi, volume_vs_average
from bot.strategy.multi_tf_analysis import analyze_trend

from _swing_backtest_lib import (
    BASELINE,
    CACHE,
    CONFIRM_TF,
    DEFAULT_END,
    ENTRY_TF,
    HISTORY_START,
    OOS_START,
    SYMBOLS,
    SYMBOLS,
    SwingParams,
    _breakout_at,
    _htf_until,
    _tp_sl_pcts,
    load_1h_and_4h,
    load_or_fetch,
    simulate_swing,
    summarize_run,
)

MAKER_FEE_PCT = 0.15
MAKER_SLIPPAGE_PCT = 0.01
DAILY_TF = "1Day"
ADX_GRID = (20.0, 25.0, 30.0)


@dataclass(frozen=True)
class RegimeFilterParams:
    adx_threshold: float
    volume_confirm: bool = False
    momentum_confirm: bool = False
    adx_period: int = 14
    volume_period: int = 20
    volume_mult: float = 1.5
    rsi_period: int = 14
    rsi_min: float = 50.0

    def cache_key(self) -> str:
        v = "vol1" if self.volume_confirm else "vol0"
        m = "mom1" if self.momentum_confirm else "mom0"
        adx = int(self.adx_threshold) if self.adx_threshold == int(self.adx_threshold) else self.adx_threshold
        return f"adx{adx}_{v}_{m}"


def _entries_cache_path(symbol: str, key: str) -> Path:
    safe = symbol.replace("/", "-")
    return CACHE / f"{safe}_1Hour_4H_swing_regime_{key}_entries.pkl"


def load_daily_bars(
    market: MarketDataService,
    symbol: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    warmup = start - timedelta(days=120)
    return load_or_fetch(market, symbol, DAILY_TF, warmup, end)


def _macd_histogram(closes: pd.Series) -> pd.Series:
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    line = fast - slow
    signal = ema(line, 9)
    return line - signal


def _daily_adx_lookup(daily: pd.DataFrame, period: int, hourly_index: pd.DatetimeIndex) -> np.ndarray:
    """ADX diario sin look-ahead: usa la última vela 1D cerrada antes del día calendario de la barra 1H."""
    if daily.empty:
        return np.full(len(hourly_index), np.nan)
    d_adx = adx_series(daily, period)
    out = np.full(len(hourly_index), np.nan)
    d_idx = daily.index
    for i, ts in enumerate(hourly_index):
        day_start = pd.Timestamp(ts).normalize()
        if ts.tzinfo is not None and day_start.tzinfo is None:
            day_start = day_start.tz_localize(ts.tzinfo)
        pos = d_idx.searchsorted(day_start, side="left") - 1
        if pos >= 0:
            val = d_adx.iloc[pos]
            if pd.notna(val):
                out[i] = float(val)
    return out


def _volume_ok_at(bars: pd.DataFrame, i: int, period: int, mult: float) -> bool:
    if i < 1:
        return False
    hist = bars.iloc[:i]
    ok, _ = volume_vs_average(hist, period=period, multiplier=mult)
    return bool(ok)


def _momentum_ok_at(bars: pd.DataFrame, i: int, rsi_period: int, rsi_min: float) -> bool:
    if i < max(rsi_period, 26) + 2:
        return False
    closes = bars["close"].iloc[:i]
    rsi_val = float(rsi(closes, rsi_period).iloc[-1])
    hist = float(_macd_histogram(closes).iloc[-1])
    if not np.isfinite(rsi_val) or not np.isfinite(hist):
        return False
    return rsi_val >= rsi_min and hist > 0.0


def precompute_regime_entries(
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    daily: pd.DataFrame,
    swing: SwingParams,
    regime: RegimeFilterParams,
) -> list[int]:
    key = regime.cache_key()
    path = _entries_cache_path(symbol, key)
    if path.exists():
        data = pickle.loads(path.read_bytes())
        if isinstance(data, list):
            print(f"cache {symbol} regime entries={len(data)} {key}")
            return data

    atr_s = atr_series(bars, swing.atr_period)
    adx_at = _daily_adx_lookup(daily, regime.adx_period, bars.index)

    warmup = max(
        swing.breakout_lookback + 2,
        swing.atr_period + swing.atr_percentile_window,
        swing.htf_slow + 2,
        regime.volume_period + 2,
        regime.rsi_period + 26,
    )

    entries: list[int] = []
    trades_today: dict[str, int] = {}
    in_pos_until = -1
    n = len(bars)

    print(f"{symbol} regime scan {n} bars key={key} start={warmup}")
    for i in range(warmup, n):
        if i <= in_pos_until:
            continue
        if not _breakout_at(bars, i, swing.breakout_lookback):
            continue

        atr_val = float(atr_s.iloc[i - 1]) if pd.notna(atr_s.iloc[i - 1]) else 0.0
        if atr_val <= 0:
            continue
        atr_window = atr_s.iloc[max(0, i - 1 - swing.atr_percentile_window) : i].dropna()
        if len(atr_window) < swing.atr_period + 5:
            continue
        if atr_val <= float(np.percentile(atr_window, swing.atr_percentile)):
            continue

        ts = bars.index[i - 1]
        htf_hist = _htf_until(htf, ts)
        trend, _ = analyze_trend(htf_hist, fast=swing.htf_fast, slow=swing.htf_slow)
        if trend != "bull":
            continue

        adx_val = adx_at[i - 1]
        if not np.isfinite(adx_val) or adx_val < regime.adx_threshold:
            continue

        if regime.volume_confirm and not _volume_ok_at(
            bars, i, regime.volume_period, regime.volume_mult
        ):
            continue

        if regime.momentum_confirm and not _momentum_ok_at(
            bars, i, regime.rsi_period, regime.rsi_min
        ):
            continue

        day_key = str(ts.date())
        if trades_today.get(day_key, 0) >= swing.max_trades_per_day:
            continue

        entries.append(i)
        trades_today[day_key] = trades_today.get(day_key, 0) + 1

        entry_px = float(bars["open"].iloc[i])
        tp_pct, sl_pct = _tp_sl_pcts(entry_px, atr_val, swing)
        sl = entry_px * (1.0 - sl_pct)
        tp = entry_px * (1.0 + tp_pct)
        exit_j = n - 1
        for j in range(i + 1, n):
            if float(bars["low"].iloc[j]) <= sl:
                exit_j = j
                break
            if float(bars["high"].iloc[j]) >= tp:
                exit_j = j
                break
        in_pos_until = exit_j

    print(f"{symbol} regime entries={len(entries)} {key}")
    path.write_bytes(pickle.dumps(entries, protocol=pickle.HIGHEST_PROTOCOL))
    return entries


def run_regime_combo(
    settings: Settings,
    market: MarketDataService,
    symbol: str,
    regime: RegimeFilterParams,
    *,
    scope: str,
    end: pd.Timestamp,
    fee_pct: float = MAKER_FEE_PCT,
    slippage_pct: float = MAKER_SLIPPAGE_PCT,
) -> dict[str, object]:
    start_dt = datetime(HISTORY_START.year, HISTORY_START.month, HISTORY_START.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    bars, htf = load_1h_and_4h(market, symbol, start_dt, end_dt)
    daily = load_daily_bars(market, symbol, start_dt, end_dt)
    if bars.empty or len(bars) < 300:
        return {}

    entries = precompute_regime_entries(symbol, bars, htf, daily, BASELINE, regime)
    atr_s = atr_series(bars, BASELINE.atr_period)

    if scope == "is":
        period_start = HISTORY_START
        period_end = OOS_START
    else:
        period_start = OOS_START
        period_end = end

    trades, equity = simulate_swing(
        settings,
        symbol,
        bars,
        entries,
        BASELINE,
        atr_s,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        period_start=period_start,
        period_end=period_end,
    )
    summary = summarize_run(symbol, trades, equity, bars, float(settings.backtest_cash))
    summary["scope"] = scope
    summary["adx_threshold"] = regime.adx_threshold
    summary["volume_confirm"] = regime.volume_confirm
    summary["momentum_confirm"] = regime.momentum_confirm
    summary["fee_pct"] = fee_pct
    summary["slippage_pct"] = slippage_pct
    summary["round_trip_pct"] = 2.0 * (fee_pct + slippage_pct)
    summary["filter_key"] = regime.cache_key()
    return summary
