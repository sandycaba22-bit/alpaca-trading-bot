"""Sweep cripto por timeframe (15m / 30m / 1h) con costos Alpaca — offline, no toca producción.

Compara 1 etapa vs 2 etapas (0.4×ATR / 0.20 buffer) recalibrando períodos por TF de entrada.
Histórico: 2021-01-01 → hoy. Costos: 0.25%% comisión + 0.03%% slippage por lado (taker).
"""

from __future__ import annotations

import argparse
import logging
import pickle
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, Settings, load_settings
from bot.risk.stops import StopTakeProfitPolicy
from bot.strategy.base import Signal, StrategyContext
from bot.strategy.indicators import last_atr, momentum_pct
from bot.strategy.multi_strategy import MultiStrategyOrchestrator
from bot.strategy.multi_tf_analysis import analyze_trend
from bot.strategy.signal_filters import SignalFilterLayer
from bot.storage.breakout_state import BreakoutStateStore

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

CACHE = PROJECT_ROOT / "data" / "bars_cache"
OUT = PROJECT_ROOT / "logs" / "crypto_tf_sweep.csv"
SYMBOLS = ("BTC/USD", "ETH/USD")
ENTRY_TFS = ("15Min", "30Min", "1Hour")
HISTORY_START = pd.Timestamp("2021-01-01", tz="UTC")
BASE_ENTRY_MIN = 15
DEFAULT_CRYPTO_FEE_PCT = 0.25
DEFAULT_SLIPPAGE_PCT = 0.03


def _tf_minutes(tf: str) -> int:
    return {
        "5Min": 5,
        "15Min": 15,
        "30Min": 30,
        "1Hour": 60,
        "1Day": 1440,
    }[tf]


def _scale_period(period: int, entry_tf: str, *, base_entry_min: int = BASE_ENTRY_MIN) -> int:
    return max(2, round(int(period) * base_entry_min / _tf_minutes(entry_tf)))


def _confirm_tf(entry_tf: str) -> str:
    """TF de confirmación ~2.5× el de entrada."""
    mapping = {
        "5Min": "15Min",
        "15Min": "30Min",
        "30Min": "1Hour",
        "1Hour": "1Day",
    }
    return mapping.get(entry_tf, "15Min")


def _scaled_settings(base: Settings, entry_tf: str) -> Settings:
    sc = lambda p: _scale_period(p, entry_tf)
    lookback = max(sc(base.lookback_bars), 40)
    return replace(
        base,
        crypto_sma_fast=sc(base.crypto_sma_fast),
        crypto_sma_slow=sc(base.crypto_sma_slow),
        atr_period=sc(base.atr_period),
        adx_period=sc(base.adx_period),
        lookback_bars=lookback,
        momentum_bars=sc(base.momentum_bars),
        confirm_momentum_bars=sc(base.confirm_momentum_bars),
        bb_period=sc(base.bb_period),
        rsi_period=sc(base.rsi_period),
        volume_confirmation_period=sc(base.volume_confirmation_period),
        breakout_lookback_periods=sc(base.breakout_lookback_periods),
        breakout_cooldown_bars=sc(base.breakout_cooldown_bars),
        squeeze_width_lookback=sc(base.squeeze_width_lookback),
        trend_micro_lookback=sc(base.trend_micro_lookback),
        trend_range_lookback=sc(base.trend_range_lookback),
    )


def _cache_path(symbol: str, tf: str) -> Path:
    safe = symbol.replace("/", "-")
    return CACHE / f"{safe}_{tf}.pkl"


def _entries_cache_path(symbol: str, entry_tf: str, confirm_tf: str) -> Path:
    safe = symbol.replace("/", "-")
    return CACHE / f"{safe}_{entry_tf}_{confirm_tf}_entries.pkl"


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


def _policy(settings: Settings, *, activate: float, buffer: float, two_stage: bool) -> StopTakeProfitPolicy:
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


def _size_qty(settings: Settings, cash: float, price: float, atr: float | None) -> float:
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


def _load_entries_cache(symbol: str, entry_tf: str, confirm_tf: str) -> list[int] | None:
    path = _entries_cache_path(symbol, entry_tf, confirm_tf)
    if path.exists():
        data = pickle.loads(path.read_bytes())
        if isinstance(data, list):
            print(f"cache {symbol} {entry_tf}/{confirm_tf} entries={len(data)}")
            return data
    return None


def _precompute_signals(
    settings: Settings,
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    entry_tf: str,
    confirm_tf: str,
) -> list[int]:
    cached = _load_entries_cache(symbol, entry_tf, confirm_tf)
    if cached is not None:
        return cached

    orch = MultiStrategyOrchestrator(settings)
    filters = SignalFilterLayer(settings, BreakoutStateStore(persist=False))
    lookback = max(int(settings.lookback_bars), 40)
    start_i = max(
        lookback,
        settings.crypto_sma_slow + 1,
        settings.bb_period + 25,
        settings.adx_period * 2 + 2,
    )
    entries: list[int] = []
    in_pos_until = -1
    n = len(bars)
    htf_pos = 0
    htf_len = len(htf) if htf is not None and not htf.empty else 0
    hold = _policy(settings, activate=0.5, buffer=0.1, two_stage=False)
    print(f"{symbol} {entry_tf} signals on {n} bars lookback={lookback} start={start_i}")
    for i in range(start_i, n):
        if i % 10000 == 0:
            print(f"{symbol} {entry_tf} signal {i}/{n} entries={len(entries)}")
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
    print(f"{symbol} {entry_tf} entries={len(entries)}")
    out_path = _entries_cache_path(symbol, entry_tf, confirm_tf)
    out_path.write_bytes(pickle.dumps(entries, protocol=pickle.HIGHEST_PROTOCOL))
    return entries


def _simulate(
    settings: Settings,
    bars: pd.DataFrame,
    entry_idxs: list[int],
    policy: StopTakeProfitPolicy,
    *,
    fee_pct: float,
    slippage_pct: float,
) -> dict[str, float]:
    cash0 = float(settings.backtest_cash)
    trades = 0
    wins = 0
    pnl_gross_sum = 0.0
    pnl_net_sum = 0.0
    lookback = max(int(settings.lookback_bars), 40)
    friction = (fee_pct + slippage_pct) / 100.0

    for ei in entry_idxs:
        entry = float(bars["open"].iloc[ei])
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
            peak = max(peak, h, float(bars["close"].iloc[j]))
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

    win_rate = (wins / trades * 100.0) if trades else 0.0
    ret_gross = (pnl_gross_sum / cash0 * 100.0) if cash0 else 0.0
    ret_net = (pnl_net_sum / cash0 * 100.0) if cash0 else 0.0
    return {
        "trades": float(trades),
        "win_rate_pct": win_rate,
        "total_return_gross_pct": ret_gross,
        "total_return_net_pct": ret_net,
    }


def _sweep_combo(
    base_settings: Settings,
    market: MarketDataService,
    symbol: str,
    entry_tf: str,
    start,
    end,
    *,
    fee_pct: float,
    slippage_pct: float,
) -> list[str]:
    confirm_tf = _confirm_tf(entry_tf)
    settings = _scaled_settings(base_settings, entry_tf)
    rows: list[str] = []
    bars = _load_or_fetch(market, symbol, entry_tf, start, end)
    htf = _load_or_fetch(market, symbol, confirm_tf, start, end)
    if bars.empty or len(bars) < 200:
        return rows

    # Clip to history start
    bars = bars.loc[bars.index >= HISTORY_START]
    htf = htf.loc[htf.index >= HISTORY_START] if not htf.empty else htf
    if bars.empty or len(bars) < 200:
        return rows

    rng = f"{pd.Timestamp(bars.index.min()).date()}->{pd.Timestamp(bars.index.max()).date()}"
    scaled_note = (
        f"sma={settings.crypto_sma_fast}/{settings.crypto_sma_slow} "
        f"atr={settings.atr_period} adx={settings.adx_period} lb={settings.lookback_bars}"
    )
    entries = _precompute_signals(settings, symbol, bars, htf, entry_tf, confirm_tf)

    schemes = (
        ("one_stage", 0.5, 0.1, False),
        ("two_stage", 0.4, 0.20, True),
    )
    for scheme, act, buf, two_stage in schemes:
        pol = _policy(settings, activate=act, buffer=buf, two_stage=two_stage)
        res = _simulate(settings, bars, entries, pol, fee_pct=fee_pct, slippage_pct=slippage_pct)
        act_s = "-" if not two_stage else f"{act}"
        buf_s = "-" if not two_stage else f"{buf}"
        row = (
            f"{symbol},{scheme},{act_s},{buf_s},{entry_tf},{confirm_tf},{len(bars)},{rng},"
            f"{int(res['trades'])},{res['win_rate_pct']:.1f},"
            f"{res['total_return_gross_pct']:.2f},{res['total_return_net_pct']:.2f},"
            f"{scaled_note},{fee_pct},{slippage_pct}"
        )
        rows.append(row)
        print(
            f"{symbol} {entry_tf}/{confirm_tf} {scheme} "
            f"tr={int(res['trades'])} wr={res['win_rate_pct']:.1f}% "
            f"gross={res['total_return_gross_pct']:.2f}% net={res['total_return_net_pct']:.2f}%"
        )
    return rows


def _print_summary(rows: list[str]) -> None:
    print("\n=== RESUMEN (two_stage 0.4×0.20, neto con costos) ===")
    header = rows[0].split(",")
    for line in rows[1:]:
        parts = line.split(",")
        if len(parts) < 12:
            continue
        symbol, scheme, act, buf, entry_tf = parts[0], parts[1], parts[2], parts[3], parts[4]
        if scheme != "two_stage" or act != "0.4" or buf != "0.2":
            continue
        trades, wr, gross, net = parts[8], parts[9], parts[10], parts[11]
        print(f"{symbol} {entry_tf:6} | tr={trades:>5} wr={wr:>5}% gross={gross:>7}% net={net:>7}%")

    print("\n=== ¿Ambos símbolos neto positivo? (two_stage 0.4×0.20) ===")
    by_tf: dict[str, list[tuple[str, float]]] = {}
    for line in rows[1:]:
        parts = line.split(",")
        if len(parts) < 12 or parts[1] != "two_stage" or parts[2] != "0.4" or parts[3] != "0.2":
            continue
        sym, tf, net = parts[0], parts[4], float(parts[11])
        by_tf.setdefault(tf, []).append((sym, net))
    for tf in ENTRY_TFS:
        syms = by_tf.get(tf, [])
        if not syms:
            continue
        both_pos = all(n > 0 for _, n in syms)
        detail = ", ".join(f"{s} {n:+.2f}%" for s, n in syms)
        flag = "SI" if both_pos else "NO"
        print(f"{tf:6} | ambos + neto: {flag} | {detail}")


def main() -> int:
    global CACHE, OUT
    parser = argparse.ArgumentParser(description="Sweep cripto por timeframe con costos Alpaca")
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    parser.add_argument("--entry-tfs", nargs="*", default=["15Min", "30Min", "1Hour"])
    parser.add_argument("--crypto-fee-pct", type=float, default=DEFAULT_CRYPTO_FEE_PCT)
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT)
    args = parser.parse_args()

    entry_tfs = list(args.entry_tfs)

    base_settings = load_settings()
    client = AlpacaClient(base_settings)
    market = MarketDataService(client)
    end = datetime.now(timezone.utc)
    start = HISTORY_START.to_pydatetime()

    header = (
        "symbol,scheme,activate,buffer,entry_tf,confirm_tf,bars,range,trades,win_rate_pct,"
        "total_return_gross_pct,total_return_net_pct,scaled_periods,crypto_fee_pct,slippage_pct"
    )
    rows: list[str] = [header]
    print(
        f"crypto TF sweep {start.date()}->{end.date()} "
        f"TFs={entry_tfs} fee={args.crypto_fee_pct}% slip={args.slippage_pct}%/side"
    )

    for entry_tf in entry_tfs:
        confirm = _confirm_tf(entry_tf)
        sc = _scaled_settings(base_settings, entry_tf)
        print(
            f"\n--- {entry_tf} confirm={confirm} | "
            f"sma {sc.crypto_sma_fast}/{sc.crypto_sma_slow} atr={sc.atr_period} adx={sc.adx_period} ---"
        )
        for symbol in args.symbols:
            rows.extend(
                _sweep_combo(
                    base_settings,
                    market,
                    symbol,
                    entry_tf,
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
    _print_summary(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
