"""Backtest acciones — baseline (TP fijo + SL actual) vs salidas asimétricas ATR."""

from __future__ import annotations

import math
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, Settings
from bot.risk.stops import StopTakeProfitPolicy
from bot.storage.params import load_symbol_params
from bot.strategy.base import Signal, StrategyContext
from bot.strategy.indicators import last_atr, momentum_pct
from bot.strategy.multi_strategy import MultiStrategyOrchestrator
from bot.strategy.multi_tf_analysis import analyze_trend
from bot.strategy.signal_filters import SignalFilterLayer
from bot.storage.breakout_state import BreakoutStateStore

CACHE = PROJECT_ROOT / "data" / "bars_cache"
OOS_START = pd.Timestamp("2025-01-01", tz="UTC")
ENTRY_TF = "5Min"
CONFIRM_TF = "15Min"
SYMBOLS = ("AAPL", "MSFT")


@dataclass
class SimTrade:
    symbol: str
    entry_idx: int
    pnl_gross: float
    pnl_net: float


def _cache_path(symbol: str, tf: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_{tf}.pkl"


def load_bars(
    market: MarketDataService, symbol: str, tf: str, start: datetime, end: datetime
) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, tf)
    if path.exists():
        bars = pickle.loads(path.read_bytes())
        if isinstance(bars, pd.DataFrame) and not bars.empty:
            lo = pd.Timestamp(start, tz="UTC") if start.tzinfo is None else pd.Timestamp(start)
            hi = pd.Timestamp(end, tz="UTC") if end.tzinfo is None else pd.Timestamp(end)
            if bars.index.min() <= lo and bars.index.max() >= hi - pd.Timedelta(days=1):
                print(f"cache {symbol} {tf} bars={len(bars)}")
                return bars
    print(f"fetch {symbol} {tf} ...")
    bars = market.get_bars_range(symbol, tf, start=start, end=end)
    if not bars.empty:
        path.write_bytes(pickle.dumps(bars, protocol=pickle.HIGHEST_PROTOCOL))
        print(f"saved {symbol} {tf} bars={len(bars)}")
    return bars


def _sma_slow_map(settings: Settings) -> dict[str, int]:
    stored = load_symbol_params()
    out: dict[str, int] = {}
    for symbol, row in stored.items():
        if row.sma_fast < row.sma_slow:
            out[str(symbol).upper()] = int(row.sma_slow)
    for sym in settings.stock_symbols:
        key = sym.upper()
        if key not in out:
            out[key] = int(settings.sma_slow)
    return out


def policy_baseline(settings: Settings) -> StopTakeProfitPolicy:
    """Producción actual: piso MIN_TP_PCT + STOCK_ATR_SL + trailing global."""
    return StopTakeProfitPolicy(
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        atr_stop_mult=settings.atr_stop_mult,
        atr_sl_mult=settings.stock_atr_sl_mult,
        atr_tp_mult=settings.atr_tp_mult,
        atr_trailing_mult=settings.atr_trailing_mult,
        breakeven_activate_pct=settings.breakeven_activate_pct,
        breakeven_activate_atr_mult=settings.breakeven_activate_atr_mult,
        breakeven_buffer=settings.breakeven_buffer,
        breakeven_buffer_atr_mult=settings.breakeven_buffer_atr_mult,
        min_tp_pct=settings.min_tp_pct,
        min_stop_pct=settings.stock_min_stop_pct,
        use_breakeven_lock=True,
    )


def policy_asymmetric(settings: Settings) -> StopTakeProfitPolicy:
    return StopTakeProfitPolicy(
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        atr_stop_mult=settings.atr_stop_mult,
        atr_sl_mult=settings.stock_asymmetric_sl_atr_mult,
        atr_tp_mult=99.0,
        max_tp_pct=99.0,
        atr_trailing_mult=settings.stock_asymmetric_trail_atr_mult,
        breakeven_activate_pct=settings.breakeven_activate_pct,
        breakeven_activate_atr_mult=settings.breakeven_activate_atr_mult,
        breakeven_buffer=settings.breakeven_buffer,
        breakeven_buffer_atr_mult=settings.breakeven_buffer_atr_mult,
        min_tp_pct=0.0,
        min_stop_pct=settings.stock_min_stop_pct,
        use_breakeven_lock=True,
    )


def _sl_mult_for_policy(settings: Settings, policy: StopTakeProfitPolicy) -> float:
    return float(policy.atr_sl_mult)


def _size_qty(
    settings: Settings, cash: float, price: float, atr: float | None, sl_mult: float
) -> float:
    if price <= 0:
        return 0.0
    cap = min(cash * settings.position_size_pct, settings.max_notional_per_order, cash)
    if settings.use_fixed_risk_sizing and atr and atr > 0:
        sl_dist = float(atr) * sl_mult
        if sl_dist > 0:
            risk = cash * settings.risk_percent_per_trade
            qty = risk / sl_dist
            return max(0.0, min(qty, cap / price))
    return cap / price


def _htf_until(htf: pd.DataFrame, ts, htf_pos: int | None = None) -> pd.DataFrame:
    if htf is None or htf.empty:
        return htf
    if htf_pos is not None:
        return htf.iloc[:htf_pos]
    return htf.loc[htf.index < ts]


def precompute_entries(
    settings: Settings,
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    sma_slow_by_symbol: dict[str, int],
) -> list[int]:
    orch = MultiStrategyOrchestrator(settings, sma_slow_by_symbol)
    filters = SignalFilterLayer(settings, BreakoutStateStore(persist=False))
    lookback = max(int(settings.lookback_bars), 80)
    start_i = max(
        lookback,
        settings.sma_slow + 1,
        settings.bb_period + 25,
        settings.adx_period * 2 + 2,
    )
    entries: list[int] = []
    in_pos_until = -1
    n = len(bars)
    htf_pos = 0
    htf_len = len(htf) if htf is not None and not htf.empty else 0
    hold = policy_baseline(settings)

    for i in range(start_i, n):
        if i <= in_pos_until:
            continue
        hist = bars.iloc[i - lookback : i]
        if hist.empty:
            continue
        ts = bars.index[i]
        last_price = float(hist["close"].iloc[-1])
        if htf_len:
            while htf_pos < htf_len and htf.index[htf_pos] < ts:
                htf_pos += 1
            htf_hist = _htf_until(htf, ts, htf_pos)
        else:
            htf_hist = None
        trend, _ = analyze_trend(htf_hist if htf_hist is not None and len(htf_hist) > 20 else hist)
        if trend == "bear":
            continue
        ctx = StrategyContext(
            symbol=symbol,
            bars=hist,
            has_long_position=False,
            has_short_position=False,
            last_price=last_price,
            spread_pct=0.0,
            momentum_pct=momentum_pct(hist["close"], settings.momentum_bars),
            atr=last_atr(hist, settings.atr_period),
            htf_trend=trend,
        )
        signal = orch.generate_signal(ctx)
        if signal is not Signal.BUY:
            continue
        last_st = getattr(orch, "last_strategy", None)
        name = getattr(last_st, "value", "") if last_st else ""
        if name in {"", "breakout", "sma_crossover", "sma_crossover_tuned"}:
            slow = int(orch.slow_period(symbol))
            if not filters.check_sma_bias(symbol, signal, hist, slow).allowed:
                continue
            if settings.adx_filter_enabled and not filters.check_adx(symbol, hist).allowed:
                continue
        if filters.settings.entry_confirmation_enabled:
            if not filters.check_entry_confirmation(symbol, hist, htf_hist).allowed:
                continue
        entries.append(i)
        entry_px = float(bars["open"].iloc[i])
        atr_e = last_atr(hist, settings.atr_period)
        lv = hold.levels(entry_px, 1.0, entry_px, atr_e)
        sl, tp, peak = lv.stop_price, lv.take_profit_price, entry_px
        exit_j = n - 1
        for j in range(i + 1, n):
            h = float(bars["high"].iloc[j])
            l = float(bars["low"].iloc[j])
            c = float(bars["close"].iloc[j])
            peak = max(peak, h, c)
            new_sl, _ = hold.trailing_candidate(entry_px, 1.0, peak, sl, atr_e)
            if new_sl is not None:
                sl = new_sl
            if sl > 0 and l <= sl:
                exit_j = j
                break
            if tp > 0 and h >= tp:
                exit_j = j
                break
        in_pos_until = exit_j
    print(f"{symbol} entries={len(entries)} (5Min multi-regimen)")
    return entries


def simulate_trades(
    settings: Settings,
    bars: pd.DataFrame,
    entry_idxs: list[int],
    policy: StopTakeProfitPolicy,
    *,
    symbol: str,
    fee_pct: float,
    slippage_pct: float,
    period_start: pd.Timestamp | None = None,
    period_end: pd.Timestamp | None = None,
) -> list[SimTrade]:
    cash0 = float(settings.backtest_cash)
    friction = (fee_pct + slippage_pct) / 100.0
    lookback = max(int(settings.lookback_bars), 80)
    sl_mult = _sl_mult_for_policy(settings, policy)
    trades: list[SimTrade] = []

    for ei in entry_idxs:
        ts = bars.index[ei]
        if period_start is not None and ts < period_start:
            continue
        if period_end is not None and ts > period_end:
            continue
        entry = float(bars["open"].iloc[ei])
        if entry <= 0:
            continue
        hist = bars.iloc[max(0, ei - lookback) : ei]
        atr = last_atr(hist, settings.atr_period)
        qty = _size_qty(settings, cash0, entry, atr, sl_mult)
        if qty <= 0:
            continue
        levels = policy.levels(entry, qty, entry, atr)
        sl = levels.stop_price
        tp = levels.take_profit_price if policy.min_tp_pct > 0 else 0.0
        if policy.min_tp_pct <= 0 and policy.atr_tp_mult >= 50:
            tp = 0.0
        peak = entry
        exit_px = None
        for j in range(ei + 1, len(bars)):
            h = float(bars["high"].iloc[j])
            l = float(bars["low"].iloc[j])
            c = float(bars["close"].iloc[j])
            peak = max(peak, h, c)
            new_sl, _ = policy.trailing_candidate(entry, qty, peak, sl, atr)
            if new_sl is not None:
                sl = new_sl
            if sl > 0 and l <= sl:
                exit_px = sl
                break
            if tp > 0 and h >= tp:
                exit_px = tp
                break
        if exit_px is None:
            exit_px = float(bars["close"].iloc[-1])
        notional_in = entry * qty
        notional_out = exit_px * qty
        pnl_gross = (exit_px - entry) * qty
        pnl_net = pnl_gross - (notional_in + notional_out) * friction
        trades.append(SimTrade(symbol=symbol, entry_idx=ei, pnl_gross=pnl_gross, pnl_net=pnl_net))
    return trades


def summarize_trades(trades: list[SimTrade], cash0: float) -> dict[str, float]:
    if not trades or cash0 <= 0:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_return_net_pct": 0.0,
            "avg_win_net": 0.0,
            "avg_loss_net": 0.0,
            "avg_win_r": 0.0,
        }
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gross_w = sum(t.pnl_net for t in wins)
    gross_l = sum(abs(t.pnl_net) for t in losses)
    pf = (gross_w / gross_l) if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)
    avg_win = (sum(t.pnl_net for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_net for t in losses) / len(losses)) if losses else 0.0
    return {
        "trades": float(len(trades)),
        "win_rate_pct": len(wins) / len(trades) * 100.0,
        "profit_factor": pf,
        "total_return_net_pct": sum(t.pnl_net for t in trades) / cash0 * 100.0,
        "avg_win_net": avg_win,
        "avg_loss_net": avg_loss,
        "avg_win_r": (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0,
    }


def run_symbol_comparison(
    settings: Settings,
    market: MarketDataService,
    symbol: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
    sma_slow_by_symbol: dict[str, int],
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
    start_dt = start.to_pydatetime()
    end_dt = end.to_pydatetime()
    bars = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
    htf = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)
    if bars.empty or len(bars) < 500:
        empty: dict[str, float] = {"symbol": symbol, "trades": 0}
        return empty, empty, empty, empty

    entries = precompute_entries(settings, symbol, bars, htf, sma_slow_by_symbol)
    base_pol = policy_baseline(settings)
    asym_pol = policy_asymmetric(settings)
    cash0 = float(settings.backtest_cash)

    is_end = OOS_START - pd.Timedelta(minutes=5)
    is_trades_b = simulate_trades(
        settings,
        bars,
        entries,
        base_pol,
        symbol=symbol,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        period_start=start,
        period_end=is_end,
    )
    is_trades_a = simulate_trades(
        settings,
        bars,
        entries,
        asym_pol,
        symbol=symbol,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        period_start=start,
        period_end=is_end,
    )
    oos_trades_b = simulate_trades(
        settings,
        bars,
        entries,
        base_pol,
        symbol=symbol,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        period_start=OOS_START,
        period_end=end,
    )
    oos_trades_a = simulate_trades(
        settings,
        bars,
        entries,
        asym_pol,
        symbol=symbol,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        period_start=OOS_START,
        period_end=end,
    )

    m_is_b = summarize_trades(is_trades_b, cash0)
    m_is_a = summarize_trades(is_trades_a, cash0)
    m_oos_b = summarize_trades(oos_trades_b, cash0)
    m_oos_a = summarize_trades(oos_trades_a, cash0)
    for m in (m_is_b, m_is_a, m_oos_b, m_oos_a):
        m["symbol"] = symbol
    return m_is_b, m_is_a, m_oos_b, m_oos_a


def pf_str(pf: float) -> str:
    if pf == float("inf"):
        return "inf"
    if math.isnan(pf):
        return "n/a"
    return f"{pf:.2f}"
