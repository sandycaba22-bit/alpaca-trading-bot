#!/usr/bin/env python3
"""Sweep TP ATR (vol_2x + salidas asim) — élite + muestra Top 50.

Usa data/bars_cache/ si existe. Salida: logs/stock_tp_atr_sweep.csv

  ENV_FILE=.env.stocks python -u scripts/sweep_stock_tp_atr_vol2x.py
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings
from bot.universe import ELITE_STOCK_SYMBOLS, TOP50_US_STOCK_SYMBOLS

from _stocks_asymmetric_backtest_lib import (
    CONFIRM_TF,
    ENTRY_TF,
    EntryVariant,
    OOS_START,
    load_bars,
    policy_asymmetric,
    precompute_entries,
    simulate_trades,
    summarize_trades,
)

IS_START = pd.Timestamp("2020-01-01", tz="UTC")
VOL2X = EntryVariant(name="vol_2x", volume_mult=2.0, volume_period=20, sma_slow=50)
TP_GRID = (0.0, 3.0, 3.6, 4.2, 4.8, 5.4, 6.0)
OUT_CSV = PROJECT_ROOT / "logs" / "stock_tp_atr_sweep.csv"
SAMPLE_TOP50 = ("AAPL", "NVDA", "META", "TSLA", "MSFT")


def main() -> int:
    end = pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    symbols = list(ELITE_STOCK_SYMBOLS) + list(SAMPLE_TOP50)
    symbols = list(dict.fromkeys(symbols))
    sma_map = {s: 50 for s in symbols}

    print("=" * 96, flush=True)
    print("SWEEP TP ATR | vol_2x | SL=1.2x trail=2.75x (settings)", flush=True)
    print(f"IS {IS_START.date()} -> {OOS_START.date()} | OOS -> {end.date()}", flush=True)
    print(f"Símbolos: {len(symbols)} | TP grid: {TP_GRID}", flush=True)
    print("=" * 96, flush=True)

    rows: list[dict] = []
    start_dt = IS_START.to_pydatetime()
    end_dt = end.to_pydatetime()

    for symbol in symbols:
        print(f"\n>> {symbol}", flush=True)
        try:
            bars = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
            htf = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)
        except Exception as exc:
            print(f"skip {symbol}: {exc}", flush=True)
            continue
        if bars.empty or htf.empty:
            print(f"skip {symbol}: sin barras", flush=True)
            continue

        for tp_mult in TP_GRID:
            pol = policy_asymmetric(settings, tp_atr_mult=tp_mult)
            entries = precompute_entries(
                settings,
                symbol,
                bars,
                htf,
                sma_map,
                variant=VOL2X,
                exit_policy=pol,
                quiet=True,
            )
            rr_label = (
                f"{tp_mult / settings.stock_asymmetric_sl_atr_mult:.1f}R"
                if tp_mult > 0
                else "no_tp"
            )
            for scope, p0, p1 in (
                ("IS", IS_START, OOS_START - pd.Timedelta(minutes=5)),
                ("OOS", OOS_START, end),
            ):
                trades = simulate_trades(
                    settings,
                    bars,
                    entries,
                    pol,
                    symbol=symbol,
                    fee_pct=0.0,
                    slippage_pct=0.03,
                    period_start=p0,
                    period_end=p1,
                )
                m = summarize_trades(trades, float(settings.backtest_cash))
                row = {
                    "symbol": symbol,
                    "tp_atr_mult": tp_mult,
                    "rr_vs_sl": rr_label,
                    "scope": scope,
                    "trades": int(m["trades"]),
                    "win_rate_pct": round(m["win_rate_pct"], 2),
                    "profit_factor": round(m["profit_factor"], 3)
                    if m["profit_factor"] != float("inf")
                    else 999.0,
                    "return_net_pct": round(m["total_return_net_pct"], 3),
                    "avg_win_r": round(m.get("avg_win_r", 0.0), 3),
                }
                rows.append(row)
                print(
                    f"  TP={tp_mult:4.1f} {scope} trades={row['trades']} "
                    f"PF={row['profit_factor']} ret={row['return_net_pct']}%",
                    flush=True,
                )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nGuardado: {OUT_CSV}", flush=True)

        # Mejor TP OOS agregado (sum return, min trades)
        oos = [r for r in rows if r["scope"] == "OOS" and r["trades"] >= 3]
        by_tp: dict[float, float] = {}
        for r in oos:
            by_tp[r["tp_atr_mult"]] = by_tp.get(r["tp_atr_mult"], 0.0) + r["return_net_pct"]
        if by_tp:
            best = max(by_tp.items(), key=lambda x: x[1])
            print(
                f"\n>>> Sugerencia OOS (sum ret, símbolos con trades): "
                f"STOCK_ASYMMETRIC_TP_ATR_MULT={best[0]} (~{best[0]/1.2:.1f}R vs SL 1.2)",
                flush=True,
            )
    else:
        print("Sin filas — rellena data/bars_cache/ (warm_bars_cache.sh stocks_elite)", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
