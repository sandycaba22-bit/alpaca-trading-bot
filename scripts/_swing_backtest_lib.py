"""Estrategia swing cripto 1H/4H — offline, no toca producción.

Entry: breakout 20 velas 1H + ATR > percentil 75 + tendencia alcista 4H.
Exit: TP asimétrico (3–5% ATR-scaled), SL fijo 1.5%, sin trailing.
Costos: comisión + slippage por lado (Alpaca crypto taker).
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.backtest.engine import SimulatedTrade
from bot.backtest.metrics import PerformanceMetrics, compute_metrics
from bot.config import PROJECT_ROOT, Settings
from bot.strategy.indicators import atr as atr_series
from bot.strategy.multi_tf_analysis import analyze_trend

CACHE = PROJECT_ROOT / "data" / "bars_cache"
SYMBOLS = ("BTC/USD", "ETH/USD")
ENTRY_TF = "1Hour"
CONFIRM_TF = "4H"
HISTORY_START = pd.Timestamp("2021-01-01", tz="UTC")
OOS_START = pd.Timestamp("2025-01-01", tz="UTC")
DEFAULT_END = pd.Timestamp("2026-09-20", tz="UTC")
DEFAULT_CRYPTO_FEE_PCT = 0.25
DEFAULT_SLIPPAGE_PCT = 0.03


@dataclass(frozen=True)
class SwingParams:
    """Parámetros de la estrategia swing (independiente de producción SMA/ADX/trailing)."""

    breakout_lookback: int = 20
    atr_period: int = 14
    atr_percentile_window: int = 100
    atr_percentile: float = 75.0
    tp_base_pct: float = 0.03
    tp_atr_scale: float = 0.005  # + (ATR/entry) × 0.5%
    tp_max_pct: float = 0.05
    sl_pct: float = 0.015
    max_trades_per_day: int = 5
    htf_fast: int = 5
    htf_slow: int = 13
    double_breakout: bool = False
    tp_fixed_pct: float | None = None  # variant A: fixed TP
    sl_fixed_pct: float | None = None  # variant A: fixed SL


@dataclass
class SwingTrade:
    symbol: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    qty: float
    tp_pct: float
    sl_pct: float
    pnl_gross: float
    pnl_net: float
    exit_reason: str


def round_trip_cost_pct(fee_pct: float, slippage_pct: float) -> float:
    return 2.0 * (float(fee_pct) + float(slippage_pct)) / 100.0


def _cache_path(symbol: str, tf: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_{tf}.pkl"


def _entries_cache_path(symbol: str, variant: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_1Hour_4H_swing_{variant}_entries.pkl"


def load_or_fetch(
    market: MarketDataService, symbol: str, tf: str, start: datetime, end: datetime
) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, tf)
    if path.exists():
        bars = pickle.loads(path.read_bytes())
        if isinstance(bars, pd.DataFrame) and not bars.empty:
            print(f"cache {symbol} {tf} bars={len(bars)} {bars.index.min()}->{bars.index.max()}")
            return bars
    print(f"fetch {symbol} {tf} {start.date()}->{end.date()} ...")
    bars = market.get_bars_range(symbol, tf, start=start, end=end)
    if not bars.empty:
        path.write_bytes(pickle.dumps(bars, protocol=pickle.HIGHEST_PROTOCOL))
        print(f"saved {symbol} {tf} bars={len(bars)}")
    else:
        print(f"empty {symbol} {tf}")
    return bars


def resample_4h(bars_1h: pd.DataFrame) -> pd.DataFrame:
    """4H desde 1H (Alpaca no tiene 4Hour nativo)."""
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
    if "vwap" in bars_1h.columns:
        agg["vwap"] = "mean"
    out = bars_1h.resample("4h", label="right", closed="right").agg(agg).dropna(how="any")
    return out


def load_1h_and_4h(
    market: MarketDataService, symbol: str, start: datetime, end: datetime
) -> tuple[pd.DataFrame, pd.DataFrame]:
    bars_1h = load_or_fetch(market, symbol, ENTRY_TF, start, end)
    path_4h = _cache_path(symbol, CONFIRM_TF)
    if path_4h.exists():
        bars_4h = pickle.loads(path_4h.read_bytes())
        if isinstance(bars_4h, pd.DataFrame) and not bars_4h.empty:
            print(f"cache {symbol} {CONFIRM_TF} bars={len(bars_4h)}")
        else:
            bars_4h = resample_4h(bars_1h)
            path_4h.write_bytes(pickle.dumps(bars_4h, protocol=pickle.HIGHEST_PROTOCOL))
    else:
        bars_4h = resample_4h(bars_1h)
        if not bars_4h.empty:
            path_4h.write_bytes(pickle.dumps(bars_4h, protocol=pickle.HIGHEST_PROTOCOL))
            print(f"saved {symbol} {CONFIRM_TF} bars={len(bars_4h)}")
    return bars_1h, bars_4h


def _htf_until(htf: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    if htf is None or htf.empty:
        return htf
    return htf.loc[htf.index <= ts]


def _size_qty(settings: Settings, cash: float, price: float, sl_pct: float) -> float:
    if price <= 0 or sl_pct <= 0:
        return 0.0
    cap = min(cash * settings.position_size_pct, settings.max_notional_per_order, cash)
    risk = cash * settings.risk_percent_per_trade
    sl_dist = price * sl_pct
    if settings.use_fixed_risk_sizing and sl_dist > 0:
        qty = risk / sl_dist
        return max(0.0, min(qty, cap / price))
    return cap / price


def _tp_sl_pcts(entry: float, atr_val: float, params: SwingParams) -> tuple[float, float]:
    if params.tp_fixed_pct is not None:
        tp_pct = float(params.tp_fixed_pct)
    else:
        atr_ratio = (atr_val / entry) if entry > 0 and atr_val > 0 else 0.0
        tp_pct = min(params.tp_max_pct, params.tp_base_pct + atr_ratio * params.tp_atr_scale)
    sl_pct = float(params.sl_fixed_pct) if params.sl_fixed_pct is not None else params.sl_pct
    return tp_pct, sl_pct


def _breakout_at(bars: pd.DataFrame, i: int, lookback: int) -> bool:
    """Cierre barra i-1 > máximo high de las `lookback` velas previas completadas."""
    if i < lookback + 1:
        return False
    prev_close = float(bars["close"].iloc[i - 1])
    window = bars["high"].iloc[i - 1 - lookback : i - 1]
    if window.empty:
        return False
    return prev_close > float(window.max())


def precompute_entries(
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    params: SwingParams,
    *,
    variant_key: str,
    period_start: pd.Timestamp | None = None,
) -> list[int]:
    cache_path = _entries_cache_path(symbol, variant_key)
    if cache_path.exists():
        data = pickle.loads(cache_path.read_bytes())
        if isinstance(data, list):
            print(f"cache {symbol} swing entries={len(data)} variant={variant_key}")
            return data

    atr_s = atr_series(bars, params.atr_period)
    warmup = max(
        params.breakout_lookback + 2,
        params.atr_period + params.atr_percentile_window,
        params.htf_slow + 2,
    )
    period_start_i = 0
    if period_start is not None:
        period_start_i = int(bars.index.searchsorted(period_start, side="left"))
    start_i = max(warmup, period_start_i)

    entries: list[int] = []
    trades_today: dict[str, int] = {}
    in_pos_until = -1
    n = len(bars)

    print(f"{symbol} swing scan {n} bars start={start_i} variant={variant_key}")
    for i in range(start_i, n):
        if i <= in_pos_until:
            continue

        if not _breakout_at(bars, i, params.breakout_lookback):
            continue
        if params.double_breakout and not _breakout_at(bars, i - 1, params.breakout_lookback):
            continue

        atr_val = float(atr_s.iloc[i - 1]) if pd.notna(atr_s.iloc[i - 1]) else 0.0
        if atr_val <= 0:
            continue
        atr_window = atr_s.iloc[max(0, i - 1 - params.atr_percentile_window) : i].dropna()
        if len(atr_window) < params.atr_period + 5:
            continue
        p75 = float(np.percentile(atr_window, params.atr_percentile))
        if atr_val <= p75:
            continue

        ts = bars.index[i - 1]
        htf_hist = _htf_until(htf, ts)
        trend, _ = analyze_trend(htf_hist, fast=params.htf_fast, slow=params.htf_slow)
        if trend != "bull":
            continue

        day_key = str(ts.date())
        if trades_today.get(day_key, 0) >= params.max_trades_per_day:
            continue

        entries.append(i)
        trades_today[day_key] = trades_today.get(day_key, 0) + 1

        entry_px = float(bars["open"].iloc[i])
        tp_pct, sl_pct = _tp_sl_pcts(entry_px, atr_val, params)
        sl = entry_px * (1.0 - sl_pct)
        tp = entry_px * (1.0 + tp_pct)
        exit_j = n - 1
        for j in range(i + 1, n):
            lo = float(bars["low"].iloc[j])
            hi = float(bars["high"].iloc[j])
            if lo <= sl:
                exit_j = j
                break
            if hi >= tp:
                exit_j = j
                break
        in_pos_until = exit_j

    print(f"{symbol} swing entries={len(entries)} variant={variant_key}")
    cache_path.write_bytes(pickle.dumps(entries, protocol=pickle.HIGHEST_PROTOCOL))
    return entries


def simulate_swing(
    settings: Settings,
    symbol: str,
    bars: pd.DataFrame,
    entry_idxs: list[int],
    params: SwingParams,
    atr_s: pd.Series,
    *,
    fee_pct: float,
    slippage_pct: float,
    period_start: pd.Timestamp | None = None,
    period_end: pd.Timestamp | None = None,
) -> tuple[list[SwingTrade], pd.Series]:
    cash0 = float(settings.backtest_cash)
    friction = (fee_pct + slippage_pct) / 100.0
    period_start_i = 0
    if period_start is not None:
        period_start_i = int(bars.index.searchsorted(period_start, side="left"))
    period_end_i = len(bars) - 1
    if period_end is not None:
        period_end_i = int(bars.index.searchsorted(period_end, side="right")) - 1
        period_end_i = max(period_start_i, period_end_i)

    scoped = [i for i in entry_idxs if period_start_i <= i <= period_end_i]
    trades: list[SwingTrade] = []
    equity_points: list[tuple[pd.Timestamp, float]] = []
    cum_pnl = 0.0
    in_pos_until = period_start_i - 1

    for ei in sorted(scoped):
        if ei <= in_pos_until:
            continue
        entry = float(bars["open"].iloc[ei])
        if entry <= 0:
            continue
        atr_val = float(atr_s.iloc[ei - 1]) if ei > 0 and pd.notna(atr_s.iloc[ei - 1]) else 0.0
        tp_pct, sl_pct = _tp_sl_pcts(entry, atr_val, params)
        qty = _size_qty(settings, cash0, entry, sl_pct)
        if qty <= 0:
            continue

        sl = entry * (1.0 - sl_pct)
        tp = entry * (1.0 + tp_pct)
        exit_px = None
        exit_j = period_end_i
        reason = "eod"
        for j in range(ei + 1, min(period_end_i + 1, len(bars))):
            lo = float(bars["low"].iloc[j])
            hi = float(bars["high"].iloc[j])
            if lo <= sl:
                exit_px = sl
                exit_j = j
                reason = "sl"
                break
            if hi >= tp:
                exit_px = tp
                exit_j = j
                reason = "tp"
                break
        if exit_px is None:
            exit_px = float(bars["close"].iloc[min(period_end_i, len(bars) - 1)])
            exit_j = min(period_end_i, len(bars) - 1)
            reason = "period_end"

        notional_entry = entry * qty
        notional_exit = exit_px * qty
        pnl_gross = (exit_px - entry) * qty
        cost = (notional_entry + notional_exit) * friction
        pnl_net = pnl_gross - cost
        cum_pnl += pnl_net

        trades.append(
            SwingTrade(
                symbol=symbol,
                entry_idx=ei,
                exit_idx=exit_j,
                entry_price=entry,
                exit_price=exit_px,
                qty=qty,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                pnl_gross=pnl_gross,
                pnl_net=pnl_net,
                exit_reason=reason,
            )
        )
        equity_points.append((bars.index[exit_j], cash0 + cum_pnl))
        in_pos_until = exit_j

    if equity_points:
        equity = pd.Series(
            [p[1] for p in equity_points],
            index=pd.DatetimeIndex([p[0] for p in equity_points]),
        )
    else:
        equity = pd.Series([cash0], index=pd.DatetimeIndex([bars.index[period_start_i]]))

    return trades, equity


def swing_trades_to_simulated(trades: list[SwingTrade], bars: pd.DataFrame) -> list[SimulatedTrade]:
    out: list[SimulatedTrade] = []
    for t in trades:
        out.append(
            SimulatedTrade(
                symbol=t.symbol,
                entry_time=bars.index[t.entry_idx],
                exit_time=bars.index[t.exit_idx],
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                qty=t.qty,
                pnl_abs=t.pnl_net,
                pnl_pct=(t.exit_price / t.entry_price - 1.0) * 100.0 if t.entry_price else 0.0,
                reason=t.exit_reason,
                regime="",
                strategy="swing_1h_4h",
            )
        )
    return out


def period_breakdown(
    trades: list[SwingTrade], bars: pd.DataFrame, cash0: float
) -> dict[str, float]:
    """Retorno neto % sobre capital fijo por trimestre."""
    if not trades or cash0 <= 0:
        return {}
    buckets: dict[str, float] = {}
    for t in trades:
        ts = bars.index[t.entry_idx]
        key = f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"
        buckets[key] = buckets.get(key, 0.0) + t.pnl_net
    return {k: v / cash0 * 100.0 for k, v in sorted(buckets.items())}


def summarize_run(
    symbol: str,
    trades: list[SwingTrade],
    equity: pd.Series,
    bars: pd.DataFrame,
    cash0: float,
) -> dict[str, object]:
    sim = swing_trades_to_simulated(trades, bars)
    metrics: PerformanceMetrics = compute_metrics(equity, sim, cash0)
    gross_wins = sum(t.pnl_net for t in trades if t.pnl_net > 0)
    gross_losses = sum(abs(t.pnl_net) for t in trades if t.pnl_net < 0)
    if gross_losses > 0:
        pf = gross_wins / gross_losses
    elif gross_wins > 0:
        pf = float("inf")
    else:
        pf = 0.0

    periods = period_breakdown(trades, bars, cash0)
    best_period = max(periods, key=periods.get) if periods else "—"
    worst_period = min(periods, key=periods.get) if periods else "—"

    ret_net = sum(t.pnl_net for t in trades) / cash0 * 100.0 if cash0 else 0.0
    wins = sum(1 for t in trades if t.pnl_net > 0)

    return {
        "symbol": symbol,
        "entry_tf": ENTRY_TF,
        "confirm_tf": CONFIRM_TF,
        "trades": len(trades),
        "win_rate_pct": (wins / len(trades) * 100.0) if trades else 0.0,
        "profit_factor": pf,
        "total_return_net_pct": ret_net,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "best_period": best_period,
        "best_period_pct": periods.get(best_period, 0.0) if periods else 0.0,
        "worst_period": worst_period,
        "worst_period_pct": periods.get(worst_period, 0.0) if periods else 0.0,
        "avg_tp_pct": (sum(t.tp_pct for t in trades) / len(trades) * 100.0) if trades else 0.0,
    }


def variant_key(params: SwingParams) -> str:
    if params.tp_fixed_pct is not None:
        return f"A_tp{params.tp_fixed_pct}_sl{params.sl_fixed_pct}"
    if params.double_breakout:
        return "B_double_breakout"
    return "baseline"


BASELINE = SwingParams()
VARIANT_A = SwingParams(tp_fixed_pct=0.05, sl_fixed_pct=0.02)
VARIANT_B = SwingParams(double_breakout=True)
