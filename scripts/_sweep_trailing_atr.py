"""Sweep trailing 2 etapas (solo ATR) en TF de producción: 5m acciones + confirm 15m."""

from __future__ import annotations

import argparse
import logging
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings
from bot.market.assets import is_crypto_symbol
from bot.risk.stops import ExitReason, StopTakeProfitPolicy
from bot.strategy.base import Signal, StrategyContext
from bot.strategy.indicators import last_atr, momentum_pct
from bot.strategy.multi_strategy import MultiStrategyOrchestrator
from bot.strategy.multi_tf_analysis import analyze_trend
from bot.strategy.signal_filters import SignalFilterLayer
from bot.storage.breakout_state import BreakoutStateStore

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

CACHE = PROJECT_ROOT / "data" / "bars_cache"
OUT = PROJECT_ROOT / "logs" / "trailing_atr_sweep.csv"
SYMBOLS = ("AAPL", "MSFT", "BTC/USD", "ETH/USD")
ACTIVATE = (0.4, 0.5, 0.6, 0.75)
BUFFERS = (0.05, 0.1, 0.15, 0.2)
YEARS = 6


def _entry_tf(symbol: str) -> str:
    return "15Min" if is_crypto_symbol(symbol) else "5Min"


def _cache_path(symbol: str, tf: str) -> Path:
    safe = symbol.replace("/", "-")
    return CACHE / f"{safe}_{tf}.pkl"


def _entries_cache_path(symbol: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_entries.pkl"


def _load_or_fetch(market: MarketDataService, symbol: str, tf: str, start, end) -> pd.DataFrame:
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


def _policy(settings, *, activate: float, buffer: float, two_stage: bool) -> StopTakeProfitPolicy:
    return StopTakeProfitPolicy(
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        atr_stop_mult=settings.atr_stop_mult,
        atr_sl_mult=settings.atr_sl_mult,
        atr_tp_mult=settings.atr_tp_mult,
        atr_trailing_mult=settings.atr_trailing_mult,
        breakeven_activate_atr_mult=activate,
        breakeven_buffer_atr_mult=buffer,
        use_breakeven_lock=two_stage,
    )


def _size_qty(settings, cash: float, price: float, atr: float | None) -> float:
    if price <= 0:
        return 0.0
    cap = min(cash * settings.position_size_pct, settings.max_notional_per_order, cash)
    if settings.use_fixed_risk_sizing and atr and atr > 0:
        sl_dist = float(atr) * float(settings.atr_sl_mult)
        if sl_dist > 0:
            risk = cash * settings.risk_percent_per_trade
            qty = risk / sl_dist
            return max(0.0, min(qty, cap / price))
    return cap / price


def _confirm_ok(filters: SignalFilterLayer, symbol: str, entry_hist: pd.DataFrame, htf: pd.DataFrame | None) -> bool:
    if not filters.settings.entry_confirmation_enabled:
        return True
    return filters.check_entry_confirmation(symbol, entry_hist, htf).allowed


def _htf_until(htf: pd.DataFrame, ts, htf_pos: int | None = None) -> pd.DataFrame:
    if htf is None or htf.empty:
        return htf
    if htf_pos is not None:
        return htf.iloc[:htf_pos]
    return htf.loc[htf.index < ts]


def _load_entries(symbol: str) -> list[int] | None:
    path = _entries_cache_path(symbol)
    if not path.exists():
        return None
    data = pickle.loads(path.read_bytes())
    if isinstance(data, list):
        print(f"cache {symbol} entries={len(data)}")
        return data
    return None


def _save_entries(symbol: str, entries: list[int]) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    _entries_cache_path(symbol).write_bytes(pickle.dumps(entries, protocol=pickle.HIGHEST_PROTOCOL))
    print(f"saved {symbol} entries={len(entries)}")


def _precompute_signals(settings, symbol: str, bars: pd.DataFrame, htf: pd.DataFrame) -> list[int]:
    cached = _load_entries(symbol)
    if cached is not None:
        return cached
    """Índices de barra de ENTRADA (open de i) tras señal en i-1 + confirm 15m."""
    orch = MultiStrategyOrchestrator(settings)
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
    print(f"{symbol} signals on {n} bars lookback={lookback} start={start_i}")
    for i in range(start_i, n):
        if i % 20000 == 0:
            print(f"{symbol} signal {i}/{n} entries={len(entries)}")
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
        if not _confirm_ok(filters, symbol, hist, htf_hist):
            continue
        entries.append(i)
        hold = _policy(settings, activate=0.5, buffer=0.1, two_stage=False)
        entry_px = float(bars["open"].iloc[i])
        atr_e = last_atr(hist, settings.atr_period)
        qty = 1.0
        lv = hold.levels(entry_px, qty, entry_px, atr_e)
        sl, tp, peak = lv.stop_price, lv.take_profit_price, entry_px
        exit_j = n - 1
        for j in range(i + 1, n):
            h = float(bars["high"].iloc[j])
            l = float(bars["low"].iloc[j])
            c = float(bars["close"].iloc[j])
            peak = max(peak, h, c)
            new_sl, _ = hold.trailing_candidate(entry_px, qty, peak, sl, atr_e)
            if new_sl is not None:
                sl = new_sl
            if sl > 0 and l <= sl:
                exit_j = j
                break
            if tp > 0 and h >= tp:
                exit_j = j
                break
        in_pos_until = exit_j
    print(f"{symbol} entries={len(entries)}")
    _save_entries(symbol, entries)
    return entries


def _simulate(settings, bars: pd.DataFrame, entry_idxs: list[int], policy: StopTakeProfitPolicy):
    cash = float(settings.backtest_cash)
    trades = 0
    wins = 0
    pnl_sum = 0.0
    lookback = max(int(settings.lookback_bars), 80)
    for ei in entry_idxs:
        row = bars.iloc[ei]
        entry = float(row["open"])
        if entry <= 0:
            continue
        hist = bars.iloc[max(0, ei - lookback) : ei]
        atr = last_atr(hist, settings.atr_period)
        qty = _size_qty(settings, cash, entry, atr)
        if qty <= 0:
            continue
        levels = policy.levels(entry, qty, entry, atr)
        sl = levels.stop_price
        tp = levels.take_profit_price
        peak = entry
        exit_px = None
        for j in range(ei + 1, len(bars)):
            h = float(bars["high"].iloc[j])
            l = float(bars["low"].iloc[j])
            c = float(bars["close"].iloc[j])
            peak = max(peak, h, c)
            new_sl, _src = policy.trailing_candidate(entry, qty, peak, sl, atr)
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
        pnl = (exit_px - entry) * qty
        pnl_sum += pnl
        trades += 1
        if pnl > 0:
            wins += 1
    win_rate = (wins / trades * 100.0) if trades else 0.0
    avg = (pnl_sum / trades) if trades else 0.0
    ret = (pnl_sum / cash * 100.0) if cash else 0.0
    return trades, win_rate, avg, ret


def _sweep_symbol(settings, market, symbol: str, start, end) -> list[str]:
    rows: list[str] = []
    etf = _entry_tf(symbol)
    bars = _load_or_fetch(market, symbol, etf, start, end)
    htf = _load_or_fetch(market, symbol, "15Min", start, end)
    if bars.empty or len(bars) < 200:
        rows.append(f"{symbol},SKIP,,,,,0,,,,")
        return rows
    rng = f"{pd.Timestamp(bars.index.min()).date()}->{pd.Timestamp(bars.index.max()).date()}"
    entries = _precompute_signals(settings, symbol, bars, htf)
    baseline = _policy(settings, activate=0.5, buffer=0.1, two_stage=False)
    b_tr, b_wr, b_avg, b_ret = _simulate(settings, bars, entries, baseline)
    rows.append(
        f"{symbol},one_stage,-,-,{etf},15Min,{len(bars)},{rng},{b_tr},{b_wr:.1f},{b_avg:.4f},{b_ret:.2f}"
    )
    print(f"{symbol} baseline trades={b_tr} wr={b_wr:.1f} avg={b_avg:.4f} ret={b_ret:.2f}")
    for act in ACTIVATE:
        for buf in BUFFERS:
            pol = _policy(settings, activate=act, buffer=buf, two_stage=True)
            tr, wr, avg, ret = _simulate(settings, bars, entries, pol)
            rows.append(
                f"{symbol},two_stage,{act},{buf},{etf},15Min,{len(bars)},{rng},{tr},{wr:.1f},{avg:.4f},{ret:.2f}"
            )
            print(f"{symbol} A={act} B={buf} trades={tr} wr={wr:.1f} avg={avg:.4f} ret={ret:.2f}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    args = parser.parse_args()
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(YEARS * 365.25))
    header = "symbol,scheme,activate,buffer,entry_tf,confirm_tf,bars,range,trades,win_rate_pct,avg_pnl,total_return_pct"
    rows: list[str] = [header]
    print(f"sweep {start.date()}->{end.date()} activate={ACTIVATE} buffer={BUFFERS}")

    for symbol in args.symbols:
        rows.extend(_sweep_symbol(settings, market, symbol, start, end))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists() and args.symbols != list(SYMBOLS):
        prev = OUT.read_text(encoding="utf-8").strip().splitlines()
        keep = [prev[0]] if prev else [header]
        skip_syms = {s.split(",")[0] for s in rows[1:]}
        keep.extend(line for line in prev[1:] if line.split(",")[0] not in skip_syms)
        rows = keep + rows[1:]
    text = "\n".join(rows) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
