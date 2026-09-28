#!/usr/bin/env python3
"""Compara lógicas de entrada acciones en Top 50 · IS/OOS 2022–2026.

Variantes: baseline (sin vol filter), vol_1.2x, vol_1.5x, vol_2x (producción bot secundario).

  python -u scripts/sweep_top50_logic_compare_2022_2026.py

Salida: logs/top50_logic_compare_2022_2026.csv + _summary.txt
"""

from __future__ import annotations

import argparse
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
from bot.universe import TOP50_US_STOCK_SYMBOLS

from _stocks_asymmetric_backtest_lib import (  # noqa: E402
    CONFIRM_TF,
    ENTRY_TF,
    EntryVariant,
    evaluate_variant_symbol,
    load_bars,
    pf_str,
)

DEFAULT_IS = "2022-01-01"
DEFAULT_OOS = "2025-01-01"
DEFAULT_END = "2026-12-31"
STOCK_FEE = 0.0
STOCK_SLIP = 0.03

VARIANTS: list[EntryVariant | None] = [
    None,
    EntryVariant(name="vol_1.2x", volume_mult=1.2, volume_period=20, sma_slow=50),
    EntryVariant(name="vol_1.5x", volume_mult=1.5, volume_period=20, sma_slow=50),
    EntryVariant(name="vol_2x", volume_mult=2.0, volume_period=20, sma_slow=50),
]
VARIANT_NAMES = ["baseline_entry", "vol_1.2x", "vol_1.5x", "vol_2x"]

OUT_CSV = PROJECT_ROOT / "logs" / "top50_logic_compare_2022_2026.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "top50_logic_compare_2022_2026_summary.txt"


def main() -> int:
    os.environ.setdefault("RESEARCH_FAST", "1")
    parser = argparse.ArgumentParser()
    parser.add_argument("--is-start", default=DEFAULT_IS)
    parser.add_argument("--oos-start", default=DEFAULT_OOS)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-symbols", type=int, default=0, help="0 = todos Top 50")
    args = parser.parse_args()

    is_start = pd.Timestamp(args.is_start, tz="UTC")
    oos_start = pd.Timestamp(args.oos_start, tz="UTC")
    cap = pd.Timestamp(DEFAULT_END, tz="UTC")
    now = pd.Timestamp(datetime.now(timezone.utc))
    end = pd.Timestamp(args.end, tz="UTC") if args.end else min(now, cap)

    symbols = list(TOP50_US_STOCK_SYMBOLS)
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    sma_map = {s: 50 for s in symbols}

    rows: list[dict] = []
    fetch0 = is_start.to_pydatetime()
    fetch1 = end.to_pydatetime()

    print(f"Top 50 logic compare | IS {is_start.date()} | OOS {oos_start.date()}->{end.date()}", flush=True)

    for symbol in symbols:
        print(f"\n>> {symbol}", flush=True)
        bars = load_bars(market, symbol, ENTRY_TF, fetch0, fetch1)
        htf = load_bars(market, symbol, CONFIRM_TF, fetch0, fetch1)
        if bars.empty or len(bars) < 400:
            print(f"  SKIP bars={len(bars)}", flush=True)
            continue
        # Recorte para research: warmup antes de OOS; IS/OOS se filtran en simulate_trades.
        warm = oos_start - pd.Timedelta(days=200)
        bars = bars.loc[bars.index >= warm]
        htf = htf.loc[htf.index >= warm]
        print(f"  bars trimmed={len(bars)}", flush=True)
        for vname, variant in zip(VARIANT_NAMES, VARIANTS):
            m_is, m_oos = evaluate_variant_symbol(
                settings,
                bars,
                htf,
                symbol,
                sma_map,
                variant,
                start=is_start,
                end=end,
                fee_pct=STOCK_FEE,
                slippage_pct=STOCK_SLIP,
                oos_start=oos_start,
            )
            print(
                f"  OOS {vname:<16} tr={int(m_oos.get('trades', 0)):>3} PF={pf_str(float(m_oos.get('profit_factor', 0)))} "
                f"net={m_oos.get('total_return_net_pct', 0):+.2f}%",
                flush=True,
            )
            for scope, m in (("IS", m_is), ("OOS", m_oos)):
                rows.append(
                    {
                        "symbol": symbol,
                        "logic": vname,
                        "scope": scope,
                        "trades": int(m.get("trades", 0)),
                        "win_pct": round(float(m.get("win_rate_pct", 0)), 2),
                        "pf": round(float(m.get("profit_factor", 0)), 4),
                        "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
                    }
                )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["symbol", "logic", "scope", "trades", "win_pct", "pf", "net_pct"],
        )
        w.writeheader()
        w.writerows(rows)

    lines = [
        "=== Top 50 · comparación lógicas de entrada (OOS) ===",
        f"OOS {oos_start.date()} -> {end.date()}",
        "",
    ]
    best_logic = ""
    best_count = -1
    for logic in VARIANT_NAMES:
        oos = [r for r in rows if r["logic"] == logic and r["scope"] == "OOS"]
        wins = [
            r["symbol"]
            for r in oos
            if int(r["trades"]) > 0 and float(r["pf"]) > 1.0 and float(r["net_pct"]) > 0
        ]
        traded = [r for r in oos if int(r["trades"]) > 0]
        avg_net = sum(float(r["net_pct"]) for r in traded) / len(traded) if traded else 0.0
        lines.append(
            f"{logic:<16} | OOS OK {len(wins):>2}/{len(symbols)} | "
            f"con trades {len(traded):>2} | avg net% {avg_net:+.2f}"
        )
        if len(wins) > best_count:
            best_count = len(wins)
            best_logic = logic
        if wins:
            lines.append(f"    tickers: {', '.join(wins)}")
    lines.extend(["", f"Recomendación research: {best_logic} ({best_count} símbolos OOS positivos)"])
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines), flush=True)
    print(f"Wrote {OUT_CSV}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
