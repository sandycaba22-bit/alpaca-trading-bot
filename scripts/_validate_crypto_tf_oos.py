"""Validación OOS (2025+) de cripto en 30Min y 1Hour — offline, no toca producción.

Comparable con scripts/_validate_trailing_oos.py (6Min) y scripts/_sweep_crypto_timeframes.py.
"""

from __future__ import annotations

import argparse
import logging
import pickle
import sys
from datetime import datetime, timezone

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

# Reutiliza helpers del sweep full-period (misma escala SMA/ADX/ATR)
from scripts._sweep_crypto_timeframes import (  # noqa: E402
    CACHE,
    DEFAULT_CRYPTO_FEE_PCT,
    DEFAULT_SLIPPAGE_PCT,
    REF_6M_OOS,
    _confirm_ok,
    _confirm_tf,
    _entries_cache_path,
    _htf_until,
    _load_entries_cache,
    _load_or_fetch,
    _policy,
    _scaled_settings,
    _simulate,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

OUT = PROJECT_ROOT / "logs" / "crypto_tf_sweep_oos2025.csv"
SYMBOLS = ("BTC/USD", "ETH/USD")
ENTRY_TFS = ("30Min", "1Hour")
OOS_START = pd.Timestamp("2025-01-01", tz="UTC")
HISTORY_START = pd.Timestamp("2021-01-01", tz="UTC")

SCHEMES: tuple[tuple[str, float, float, bool], ...] = (
    ("one_stage", 0.5, 0.1, False),
    ("two_stage", 0.4, 0.20, True),
)


def _oos_entries_cache_path(symbol: str, entry_tf: str, confirm_tf: str) -> Path:
    from pathlib import Path

    safe = symbol.replace("/", "-")
    return CACHE / f"{safe}_{entry_tf}_{confirm_tf}_oos2025_entries.pkl"


def _filter_oos_entries(bars: pd.DataFrame, entries: list[int], oos_start: pd.Timestamp) -> list[int]:
    return [i for i in entries if bars.index[i] >= oos_start]


def _precompute_oos_entries(
    settings: Settings,
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    entry_tf: str,
    confirm_tf: str,
    oos_start: pd.Timestamp,
) -> list[int]:
    """Señales OOS con warmup previo; no arrastra posiciones del train."""
    oos_path = _oos_entries_cache_path(symbol, entry_tf, confirm_tf)
    if oos_path.exists():
        data = pickle.loads(oos_path.read_bytes())
        if isinstance(data, list):
            print(f"cache {symbol} {entry_tf} OOS entries={len(data)}")
            return data

    orch = MultiStrategyOrchestrator(settings)
    filters = SignalFilterLayer(settings, BreakoutStateStore(persist=False))
    lookback = max(int(settings.lookback_bars), 40)
    start_i = max(
        lookback,
        settings.crypto_sma_slow + 1,
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
    print(f"{symbol} {entry_tf} OOS signals from {bars.index[start_i]} ({n - start_i} bars)")
    for i in range(start_i, n):
        if i % 5000 == 0:
            print(f"{symbol} {entry_tf} OOS signal {i}/{n} entries={len(entries)}")
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
        lv = hold.levels(entry_px, 1.0, entry_px, atr_e)
        sl, tp, peak = lv.stop_price, lv.take_profit_price, entry_px
        exit_j = n - 1
        for j in range(i + 1, n):
            h = float(bars["high"].iloc[j])
            l = float(bars["low"].iloc[j])
            peak = max(peak, h, float(bars["close"].iloc[j]))
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
    print(f"{symbol} {entry_tf} OOS entries={len(entries)}")
    oos_path.write_bytes(pickle.dumps(entries, protocol=pickle.HIGHEST_PROTOCOL))
    return entries


def _resolve_oos_entries(
    settings: Settings,
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    entry_tf: str,
    confirm_tf: str,
) -> list[int]:
    full = _load_entries_cache(symbol, entry_tf, confirm_tf)
    if full is not None:
        oos = _filter_oos_entries(bars, full, OOS_START)
        print(f"{symbol} {entry_tf} OOS entries={len(oos)} (filtered from {len(full)} full-period)")
        return oos
    return _precompute_oos_entries(settings, symbol, bars, htf, entry_tf, confirm_tf, OOS_START)


def _validate_symbol_tf(
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

    oos_mask = bars.index >= OOS_START
    if not oos_mask.any():
        print(f"{symbol} {entry_tf} no bars >= {OOS_START.date()}")
        return rows

    oos_bars = int(oos_mask.sum())
    oos_end = pd.Timestamp(bars.index[oos_mask][-1])
    rng = f"{OOS_START.date()}->{oos_end.date()}"
    scaled_note = (
        f"sma={settings.crypto_sma_fast}/{settings.crypto_sma_slow} "
        f"atr={settings.atr_period} adx={settings.adx_period} lb={settings.lookback_bars}"
    )
    entries = _resolve_oos_entries(settings, symbol, bars, htf, entry_tf, confirm_tf)

    for scheme, act, buf, two_stage in SCHEMES:
        pol = _policy(settings, activate=act, buffer=buf, two_stage=two_stage)
        res = _simulate(settings, bars, entries, pol, fee_pct=fee_pct, slippage_pct=slippage_pct)
        act_s = "-" if not two_stage else f"{act}"
        buf_s = "-" if not two_stage else f"{buf}"
        row = (
            f"{symbol},{scheme},{act_s},{buf_s},{entry_tf},{confirm_tf},{oos_bars},{rng},"
            f"{int(res['trades'])},{res['win_rate_pct']:.1f},"
            f"{res['total_return_gross_pct']:.2f},{res['total_return_net_pct']:.2f},"
            f"{scaled_note},{fee_pct},{slippage_pct}"
        )
        rows.append(row)
        print(
            f"{symbol} {entry_tf}/{confirm_tf} {scheme} "
            f"oos_tr={int(res['trades'])} wr={res['win_rate_pct']:.1f}% "
            f"gross={res['total_return_gross_pct']:.2f}% net={res['total_return_net_pct']:.2f}%"
        )
    return rows


def _print_summary(rows: list[str]) -> None:
    print("\n=== OOS 2025+ (two_stage 0.4×0.20) vs referencia 6Min ===")
    ref_line = (
        f"{'6Min REF':8} | {'BTC':>6} net {REF_6M_OOS['BTC/USD']['net_pct']:+.2f}% "
        f"({REF_6M_OOS['BTC/USD']['trades']} tr) | "
        f"{'ETH':>6} net {REF_6M_OOS['ETH/USD']['net_pct']:+.2f}% "
        f"({REF_6M_OOS['ETH/USD']['trades']} tr)"
    )
    print(ref_line)
    print("-" * 72)
    for line in rows[1:]:
        parts = line.split(",")
        if len(parts) < 12:
            continue
        sym, scheme, act, buf, entry_tf = parts[0], parts[1], parts[2], parts[3], parts[4]
        if scheme != "two_stage" or act != "0.4" or buf != "0.2":
            continue
        trades, wr, gross, net = parts[8], parts[9], parts[10], parts[11]
        print(f"{entry_tf:6} {sym:8} | tr={trades:>4} wr={wr:>5}% gross={gross:>7}% net={net:>7}%")

    print("\n=== one_stage OOS ===")
    for line in rows[1:]:
        parts = line.split(",")
        if len(parts) < 12 or parts[1] != "one_stage":
            continue
        sym, entry_tf = parts[0], parts[4]
        trades, wr, gross, net = parts[8], parts[9], parts[10], parts[11]
        print(f"{entry_tf:6} {sym:8} | tr={trades:>4} wr={wr:>5}% gross={gross:>7}% net={net:>7}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="OOS validation crypto 30Min/1Hour 2025+")
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    parser.add_argument("--entry-tfs", nargs="*", default=list(ENTRY_TFS))
    parser.add_argument("--crypto-fee-pct", type=float, default=DEFAULT_CRYPTO_FEE_PCT)
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT)
    args = parser.parse_args()

    base_settings = load_settings()
    client = AlpacaClient(base_settings)
    market = MarketDataService(client)
    end = datetime.now(timezone.utc)
    start = HISTORY_START.to_pydatetime()

    header = (
        "symbol,scheme,activate,buffer,entry_tf,confirm_tf,oos_bars,range,trades,win_rate_pct,"
        "total_return_gross_pct,total_return_net_pct,scaled_periods,crypto_fee_pct,slippage_pct"
    )
    rows: list[str] = [header]
    print(
        f"OOS crypto TF {OOS_START.date()}->{end.date()} "
        f"TFs={args.entry_tfs} fee={args.crypto_fee_pct}% slip={args.slippage_pct}%/side"
    )

    for entry_tf in args.entry_tfs:
        confirm = _confirm_tf(entry_tf)
        sc = _scaled_settings(base_settings, entry_tf)
        print(
            f"\n--- {entry_tf} confirm={confirm} | "
            f"sma {sc.crypto_sma_fast}/{sc.crypto_sma_slow} atr={sc.atr_period} adx={sc.adx_period} ---"
        )
        for symbol in args.symbols:
            rows.extend(
                _validate_symbol_tf(
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
