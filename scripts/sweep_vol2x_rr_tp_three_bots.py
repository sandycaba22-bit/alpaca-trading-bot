#!/usr/bin/env python3
"""Sweep OOS vol_2x con TP 1:2 (R:R) — élite, Top 50 muestra; crypto night = N/A.

  python -u scripts/sweep_vol2x_rr_tp_three_bots.py

Salida: logs/vol2x_rr_tp_three_bots.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

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

VOL2X = EntryVariant(name="vol_2x", volume_mult=2.0, volume_period=20, sma_slow=50)
IS_START = pd.Timestamp("2020-01-01", tz="UTC")
OUT = PROJECT_ROOT / "logs" / "vol2x_rr_tp_three_bots.csv"
TOP50_SAMPLE = ("AAPL", "AMZN", "AMD")


def _run_stock_bot(label: str, symbols: tuple[str, ...], env_file: str) -> list[dict]:
    import os

    os.environ["ENV_FILE"] = env_file
    settings = load_settings()
    rows: list[dict] = []
    end = pd.Timestamp(datetime.now(timezone.utc))
    start_dt = IS_START.to_pydatetime()
    end_dt = end.to_pydatetime()
    pol = policy_asymmetric(settings)
    sma_map = {s: 50 for s in symbols}

    for symbol in symbols:
        try:
            from bot.alpaca.client import AlpacaClient
            from bot.alpaca.market_data import MarketDataService

            market = MarketDataService(AlpacaClient(settings))
            bars = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
            htf = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)
        except Exception as exc:
            rows.append(
                {
                    "bot": label,
                    "symbol": symbol,
                    "scope": "OOS",
                    "error": str(exc)[:120],
                }
            )
            continue
        if bars.empty:
            continue
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
        trades = simulate_trades(
            settings,
            bars,
            entries,
            pol,
            symbol=symbol,
            fee_pct=0.0,
            slippage_pct=0.03,
            period_start=OOS_START,
            period_end=end,
        )
        m = summarize_trades(trades, float(settings.backtest_cash))
        rows.append(
            {
                "bot": label,
                "symbol": symbol,
                "scope": "OOS",
                "trades": int(m["trades"]),
                "win_rate_pct": round(m["win_rate_pct"], 2),
                "profit_factor": round(m["profit_factor"], 3)
                if m["profit_factor"] != float("inf")
                else 999.0,
                "return_net_pct": round(m["total_return_net_pct"], 3),
                "tp_model": "RR_1_2_from_SL",
                "error": "",
            }
        )
        print(
            f"{label} {symbol} OOS tr={rows[-1]['trades']} PF={rows[-1]['profit_factor']} "
            f"ret={rows[-1]['return_net_pct']}%",
            flush=True,
        )
    return rows


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep OOS vol_2x con TP 1:2 (R:R desde SL).")
    p.add_argument(
        "--top50-full",
        action="store_true",
        help="Solo Top 50 completo (50 tickers); CSV en logs/vol2x_rr_tp_top50_full.csv",
    )
    p.add_argument(
        "--elite-only",
        action="store_true",
        help="Solo universo élite (6 tickers).",
    )
    p.add_argument(
        "--skip-crypto-row",
        action="store_true",
        help="No añadir fila informativa crypto_night al CSV.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    out_path = OUT
    all_rows: list[dict] = []

    if args.top50_full:
        print("=== Sweep TP 1:2 vol_2x | Top 50 completo ===", flush=True)
        out_path = PROJECT_ROOT / "logs" / "vol2x_rr_tp_top50_full.csv"
        all_rows.extend(
            _run_stock_bot(
                "top50_full",
                TOP50_US_STOCK_SYMBOLS,
                ".env.stocks_top50",
            )
        )
    elif args.elite_only:
        print("=== Sweep TP 1:2 vol_2x | élite ===", flush=True)
        out_path = PROJECT_ROOT / "logs" / "vol2x_rr_tp_elite.csv"
        all_rows.extend(_run_stock_bot("elite", ELITE_STOCK_SYMBOLS, ".env.stocks"))
    else:
        print("=== Sweep TP 1:2 vol_2x | 3 bots (muestra) ===", flush=True)
        all_rows.extend(_run_stock_bot("elite", ELITE_STOCK_SYMBOLS, ".env.stocks"))
        all_rows.extend(_run_stock_bot("top50_sample", TOP50_SAMPLE, ".env.stocks_top50"))

    if not args.skip_crypto_row and not args.top50_full and not args.elite_only:
        all_rows.append(
            {
                "bot": "crypto_night",
                "symbol": "BTC/USD,ETH/USD",
                "scope": "N/A",
                "trades": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "return_net_pct": 0.0,
                "tp_model": "asim 2.5R (motor crypto_night; no vol_2x acciones)",
                "error": "no usa vol_2x acciones ni RR 1:2 SL",
            }
        )
        print(
            "crypto_night: motor aparte (TP asimétrico crypto; no RR 1:2 stock)",
            flush=True,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if all_rows:
        keys = list(all_rows[0].keys())
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nCSV: {out_path}", flush=True)

    stock_rows = [r for r in all_rows if r.get("bot") != "crypto_night" and not r.get("error")]
    if stock_rows:
        avg_pf = sum(float(r["profit_factor"]) for r in stock_rows) / len(stock_rows)
        print(f"Media PF OOS acciones: {avg_pf:.3f} ({len(stock_rows)} símbolos)", flush=True)
    else:
        print("Sin filas acciones — calienta data/bars_cache/ en VPS", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
