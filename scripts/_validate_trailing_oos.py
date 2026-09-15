"""Validación out-of-sample (2025+) del trailing 2 etapas 0.4×ATR + buffer ATR.

Offline: no modifica el bot ni el sweep original. Reutiliza barras/señales cacheadas.
"""

from __future__ import annotations

import argparse
import logging
import math
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings
from bot.market.assets import is_crypto_symbol
from bot.risk.stops import StopTakeProfitPolicy
from bot.strategy.base import Signal, StrategyContext
from bot.strategy.indicators import last_atr, momentum_pct
from bot.strategy.multi_strategy import MultiStrategyOrchestrator
from bot.strategy.multi_tf_analysis import analyze_trend
from bot.strategy.signal_filters import SignalFilterLayer
from bot.storage.breakout_state import BreakoutStateStore

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

CACHE = PROJECT_ROOT / "data" / "bars_cache"
OUT = PROJECT_ROOT / "logs" / "trailing_atr_validation_2025.csv"
SYMBOLS = ("BTC/USD", "ETH/USD")
OOS_START = pd.Timestamp("2025-01-01", tz="UTC")
YEARS = 6

# Alpaca tier-1 taker (paper bot, volumen <100k USD/30d). Override con --crypto-fee-pct.
DEFAULT_CRYPTO_FEE_PCT = 0.25
DEFAULT_SLIPPAGE_PCT = 0.03

SCHEMES: list[tuple[str, float | None, float | None, bool]] = [
    ("one_stage", None, None, False),
    ("two_stage", 0.4, 0.20, True),
    ("two_stage", 0.4, 0.25, True),
    ("two_stage", 0.4, 0.30, True),
]


def _entry_tf(symbol: str) -> str:
    return "6Min" if is_crypto_symbol(symbol) else "5Min"


def _cache_path(symbol: str, tf: str) -> Path:
    safe = symbol.replace("/", "-")
    return CACHE / f"{safe}_{tf}.pkl"


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


def _entries_cache_path(symbol: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_entries.pkl"


def _load_oos_entries(symbol: str, bars: pd.DataFrame, oos_start: pd.Timestamp) -> list[int] | None:
    path = _entries_cache_path(symbol)
    if not path.exists():
        return None
    data = pickle.loads(path.read_bytes())
    if not isinstance(data, list):
        return None
    oos = [i for i in data if bars.index[i] >= oos_start]
    print(f"cache {symbol} OOS entries={len(oos)} (from sweep entries)")
    return oos


def _precompute_oos_entries(settings, symbol: str, bars: pd.DataFrame, htf: pd.DataFrame, oos_start: pd.Timestamp) -> list[int]:
    """Señales desde oos_start con warmup previo; posiciones no arrastradas del train."""
    orch = MultiStrategyOrchestrator(settings)
    filters = SignalFilterLayer(settings, BreakoutStateStore(persist=False))
    lookback = max(int(settings.lookback_bars), 80)
    start_i = max(
        lookback,
        settings.sma_slow + 1,
        settings.bb_period + 25,
        settings.adx_period * 2 + 2,
    )
    oos_start_i = int(bars.index.searchsorted(oos_start, side="left"))
    start_i = max(start_i, oos_start_i)
    entries: list[int] = []
    in_pos_until = -1
    n = len(bars)
    htf_pos = 0
    htf_len = len(htf) if htf is not None and not htf.empty else 0
    hold = _policy(settings, activate=0.5, buffer=0.1, two_stage=False)
    print(f"{symbol} OOS signals from {bars.index[start_i]} bars={n - start_i}")
    for i in range(start_i, n):
        if i % 10000 == 0:
            print(f"{symbol} signal {i}/{n} entries={len(entries)}")
        if i <= in_pos_until:
            continue
        hist = bars.iloc[i - lookback : i]
        if hist.empty:
            continue
        ts = bars.index[i]
        if ts < oos_start:
            continue
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
    print(f"{symbol} OOS entries={len(entries)}")
    return entries


def _max_drawdown_pct(equity: list[float]) -> float:
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            max_dd = max(max_dd, dd)
    return max_dd


def _sharpe(trade_returns: list[float]) -> float:
    if len(trade_returns) < 2:
        return 0.0
    mean = sum(trade_returns) / len(trade_returns)
    var = sum((r - mean) ** 2 for r in trade_returns) / (len(trade_returns) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    return mean / std if std > 0 else 0.0


def _simulate(
    settings,
    bars: pd.DataFrame,
    entry_idxs: list[int],
    policy: StopTakeProfitPolicy,
    *,
    fee_pct: float = 0.0,
    slippage_pct: float = 0.0,
):
    """Sizing fijo (sin compounding), igual que el sweep. fee/slippage solo afectan neto."""
    cash0 = float(settings.backtest_cash)
    trades = 0
    wins = 0
    pnl_gross_sum = 0.0
    pnl_net_sum = 0.0
    gross_returns: list[float] = []
    net_returns: list[float] = []
    equity_gross = [cash0]
    equity_net = [cash0]
    lookback = max(int(settings.lookback_bars), 80)
    friction = (fee_pct + slippage_pct) / 100.0

    for ei in entry_idxs:
        row = bars.iloc[ei]
        entry = float(row["open"])
        if entry <= 0:
            continue
        hist = bars.iloc[max(0, ei - lookback) : ei]
        atr = last_atr(hist, settings.atr_period)
        qty = _size_qty(settings, cash0, entry, atr)
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

        notional_entry = entry * qty
        notional_exit = exit_px * qty
        pnl_gross = (exit_px - entry) * qty
        cost = (notional_entry + notional_exit) * friction
        pnl_net = pnl_gross - cost

        pnl_gross_sum += pnl_gross
        pnl_net_sum += pnl_net
        trades += 1
        if pnl_gross > 0:
            wins += 1
        if notional_entry > 0:
            gross_returns.append(pnl_gross / notional_entry)
            net_returns.append(pnl_net / notional_entry)
        equity_gross.append(cash0 + pnl_gross_sum)
        equity_net.append(cash0 + pnl_net_sum)

    win_rate = (wins / trades * 100.0) if trades else 0.0
    avg_gross = (pnl_gross_sum / trades) if trades else 0.0
    avg_net = (pnl_net_sum / trades) if trades else 0.0
    ret_gross = (pnl_gross_sum / cash0 * 100.0) if cash0 else 0.0
    ret_net = (pnl_net_sum / cash0 * 100.0) if cash0 else 0.0
    dd_gross = _max_drawdown_pct(equity_gross)
    dd_net = _max_drawdown_pct(equity_net)
    sharpe_gross = _sharpe(gross_returns)
    sharpe_net = _sharpe(net_returns)
    out = {
        "trades": trades,
        "win_rate_pct": win_rate,
        "avg_pnl_gross": avg_gross,
        "total_return_gross_pct": ret_gross,
        "max_drawdown_gross_pct": dd_gross,
        "sharpe_gross": sharpe_gross,
    }
    if friction > 0:
        out.update(
            {
                "avg_pnl_net": avg_net,
                "total_return_net_pct": ret_net,
                "max_drawdown_net_pct": dd_net,
                "sharpe_net": sharpe_net,
            }
        )
    else:
        out.update(
            {
                "avg_pnl_net": avg_gross,
                "total_return_net_pct": ret_gross,
                "max_drawdown_net_pct": dd_gross,
                "sharpe_net": sharpe_gross,
            }
        )
    return out


def _validate_symbol(
    settings,
    market: MarketDataService,
    symbol: str,
    start,
    end,
    *,
    fee_pct: float,
    slippage_pct: float,
) -> list[str]:
    rows: list[str] = []
    etf = _entry_tf(symbol)
    bars = _load_or_fetch(market, symbol, etf, start, end)
    htf = _load_or_fetch(market, symbol, "15Min", start, end)
    if bars.empty or len(bars) < 200:
        return rows

    oos_mask = bars.index >= OOS_START
    if not oos_mask.any():
        print(f"{symbol} no bars >= {OOS_START.date()}")
        return rows

    oos_bars = int(oos_mask.sum())
    rng = f"{OOS_START.date()}->{pd.Timestamp(bars.index[oos_mask][-1]).date()}"
    entries = _load_oos_entries(symbol, bars, OOS_START)
    if entries is None:
        entries = _precompute_oos_entries(settings, symbol, bars, htf, OOS_START)

    for scheme, act, buf, two_stage in SCHEMES:
        if two_stage:
            pol = _policy(settings, activate=float(act), buffer=float(buf), two_stage=True)
            act_s, buf_s = f"{act}", f"{buf}"
        else:
            pol = _policy(settings, activate=0.5, buffer=0.1, two_stage=False)
            act_s, buf_s = "-", "-"

        res = _simulate(settings, bars, entries, pol, fee_pct=fee_pct, slippage_pct=slippage_pct)

        row = (
            f"{symbol},{scheme},{act_s},{buf_s},{etf},15Min,{oos_bars},{rng},"
            f"{res['trades']},{res['win_rate_pct']:.1f},"
            f"{res['avg_pnl_gross']:.4f},{res['total_return_gross_pct']:.2f},"
            f"{res['avg_pnl_net']:.4f},{res['total_return_net_pct']:.2f},"
            f"{res['max_drawdown_gross_pct']:.2f},{res['max_drawdown_net_pct']:.2f},"
            f"{res['sharpe_gross']:.4f},{res['sharpe_net']:.4f},"
            f"{fee_pct},{slippage_pct}"
        )
        rows.append(row)
        print(
            f"{symbol} {scheme} A={act_s} B={buf_s} "
            f"tr={res['trades']} wr={res['win_rate_pct']:.1f}% "
            f"ret_g={res['total_return_gross_pct']:.2f}% ret_n={res['total_return_net_pct']:.2f}% "
            f"dd_g={res['max_drawdown_gross_pct']:.2f}% sharpe_g={res['sharpe_gross']:.3f}"
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="OOS validation trailing ATR 2025+")
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    parser.add_argument(
        "--crypto-fee-pct",
        type=float,
        default=DEFAULT_CRYPTO_FEE_PCT,
        help="Alpaca crypto fee %% per side (tier-1 taker default 0.25)",
    )
    parser.add_argument(
        "--slippage-pct",
        type=float,
        default=DEFAULT_SLIPPAGE_PCT,
        help="Slippage %% per side (entry and exit)",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(YEARS * 365.25))

    header = (
        "symbol,scheme,activate,buffer,entry_tf,confirm_tf,bars,range,trades,win_rate_pct,"
        "avg_pnl_gross,total_return_gross_pct,avg_pnl_net,total_return_net_pct,"
        "max_drawdown_gross_pct,max_drawdown_net_pct,sharpe_gross,sharpe_net,"
        "crypto_fee_pct,slippage_pct"
    )
    rows: list[str] = [header]
    print(
        f"OOS validation {OOS_START.date()}->{end.date()} "
        f"fee={args.crypto_fee_pct}% slip={args.slippage_pct}%/side"
    )

    for symbol in args.symbols:
        rows.extend(
            _validate_symbol(
                settings,
                market,
                symbol,
                start,
                end,
                fee_pct=args.crypto_fee_pct,
                slippage_pct=args.slippage_pct,
            )
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(rows) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
