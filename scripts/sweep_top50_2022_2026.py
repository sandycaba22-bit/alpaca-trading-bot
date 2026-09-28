#!/usr/bin/env python3
"""Barrido vol_2x — Top 50 large caps US, ventana 2022–2026 (research).

No es el índice S&P 500 completo (~500): usa bot/universe.py TOP50_US_STOCK_SYMBOLS
(mismo universo que el bot secundario PM2).

  python -u scripts/sweep_top50_2022_2026.py
  python -u scripts/sweep_top50_2022_2026.py --yearly
  python -u scripts/sweep_top50_2022_2026.py --is-start 2022-01-01 --oos-start 2025-01-01 --end 2026-12-31

Salida:
  logs/vol2x_top50_2022_2026_sweep.csv
  logs/vol2x_top50_2022_2026_summary.txt

Requiere APCA_* en .env (o ENV_FILE=.env.stocks_top50). Usa caché data/bars_cache.
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

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings
from bot.universe import TOP50_US_STOCK_SYMBOLS

from _stocks_asymmetric_backtest_lib import (  # noqa: E402
    CONFIRM_TF,
    ENTRY_TF,
    EntryVariant,
    evaluate_variant_scopes,
    load_bars,
    pf_str,
)

DEFAULT_IS = "2022-01-01"
DEFAULT_OOS = "2025-01-01"
DEFAULT_END = "2026-12-31"
VOL2X = EntryVariant(name="vol_2x", volume_mult=2.0, volume_period=20, sma_slow=50)
STOCK_FEE = 0.0
STOCK_SLIP = 0.03

OUT_CSV = PROJECT_ROOT / "logs" / "vol2x_top50_2022_2026_sweep.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "vol2x_top50_2022_2026_summary.txt"


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _print_row(scope: str, symbol: str, m: dict[str, float]) -> None:
    print(
        f"  {scope:<5} {symbol:<8} vol_2x | tr={int(m.get('trades', 0)):>4} | "
        f"win%={m.get('win_rate_pct', 0):.1f} | PF={pf_str(float(m.get('profit_factor', 0)))} | "
        f"net%={m.get('total_return_net_pct', 0):+.2f}",
        flush=True,
    )


def _summary(rows: list[dict], symbols: list[str], meta: str) -> list[str]:
    lines = [
        "=== Top 50 vol_2x · ventana 2022–2026 ===",
        meta,
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "Entrada 5Min + 15Min · SMA50 · salidas asimétricas 1.2/2.75 ATR",
        "",
    ]
    scopes = sorted({r["scope"] for r in rows}, key=lambda s: (s not in ("IS", "OOS", "FULL"), s))
    for scope in scopes:
        lines.append(f"--- {scope} ---")
        lines.append(f"{'Symbol':<10} {'tr':>5} {'win%':>6} {'PF':>6} {'net%':>8}")
        lines.append("-" * 40)
        ok: list[str] = []
        for sym in symbols:
            match = next((r for r in rows if r["symbol"] == sym and r["scope"] == scope), None)
            if not match:
                continue
            lines.append(
                f"{sym:<10} {match['trades']:>5} {match['win_pct']:>6.1f} "
                f"{match['pf']:>6.2f} {match['net_pct']:>+8.2f}"
            )
            if scope == "OOS" and int(match["trades"]) > 0 and match["pf"] > 1.0 and match["net_pct"] > 0:
                ok.append(sym)
        if scope == "OOS":
            lines.append(f"  Pasaron OOS: {', '.join(ok) if ok else '(ninguno)'}")
        lines.append("")

    # Promedio simple net% OOS (símbolos con trades)
    oos_rows = [r for r in rows if r["scope"] == "OOS" and int(r["trades"]) > 0]
    if oos_rows:
        avg_net = sum(float(r["net_pct"]) for r in oos_rows) / len(oos_rows)
        lines.append(f"Promedio net% OOS (con trades, n={len(oos_rows)}): {avg_net:+.2f}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep vol_2x Top 50 · 2022–2026")
    parser.add_argument("--is-start", default=DEFAULT_IS)
    parser.add_argument("--oos-start", default=DEFAULT_OOS)
    parser.add_argument("--end", default=None, help=f"Fin UTC (default: min(hoy, {DEFAULT_END}))")
    parser.add_argument("--yearly", action="store_true", help="Añade un scope por año calendario")
    parser.add_argument("--symbols", nargs="+", default=None, help="Override tickers")
    args = parser.parse_args()

    is_start = _ts(args.is_start)
    oos_start = _ts(args.oos_start)
    if oos_start <= is_start:
        print("ERROR: --oos-start debe ser posterior a --is-start", file=sys.stderr)
        return 1

    cap_end = _ts(DEFAULT_END)
    now = pd.Timestamp(datetime.now(timezone.utc))
    end = _ts(args.end) if args.end else min(now, cap_end)
    if end <= oos_start:
        print("ERROR: --end debe ser posterior a --oos-start", file=sys.stderr)
        return 1

    symbols = [s.upper() for s in args.symbols] if args.symbols else list(TOP50_US_STOCK_SYMBOLS)
    y_from = 2022 if args.yearly else None
    y_to = int(end.year) if args.yearly else None

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    sma_map = {s: 50 for s in symbols}

    meta = (
        f"IS {is_start.date()} -> {oos_start.date() - pd.Timedelta(days=1)} | "
        f"OOS {oos_start.date()} -> {end.date()} | FULL {is_start.date()} -> {end.date()}"
    )
    print("=" * 88, flush=True)
    print(f"SWEEP Top 50 vol_2x | n={len(symbols)} | {meta}", flush=True)
    if args.yearly:
        print(f"Años: {y_from}..{y_to}", flush=True)
    print("=" * 88, flush=True)

    rows: list[dict] = []
    fetch_start = is_start.to_pydatetime()
    fetch_end = end.to_pydatetime()

    for symbol in symbols:
        print(f"\n>> {symbol}", flush=True)
        bars = load_bars(market, symbol, ENTRY_TF, fetch_start, fetch_end)
        htf = load_bars(market, symbol, CONFIRM_TF, fetch_start, fetch_end)
        if bars.empty or len(bars) < 400:
            print(f"  SKIP {symbol}: datos insuficientes (bars={len(bars)})", flush=True)
            continue

        scopes = evaluate_variant_scopes(
            settings,
            bars,
            htf,
            symbol,
            sma_map,
            VOL2X,
            is_start=is_start,
            oos_start=oos_start,
            end=end,
            fee_pct=STOCK_FEE,
            slippage_pct=STOCK_SLIP,
            include_full=True,
            yearly_from=y_from,
            yearly_to=y_to,
        )
        for scope, m in scopes:
            _print_row(scope, symbol, m)
            rows.append(
                {
                    "symbol": symbol,
                    "scope": scope,
                    "variant": "vol_2x",
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
            fieldnames=["symbol", "scope", "variant", "trades", "win_pct", "pf", "net_pct"],
        )
        w.writeheader()
        w.writerows(rows)

    summary = _summary(rows, symbols, meta)
    OUT_TXT.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary), flush=True)
    print(f"\nWrote {OUT_CSV} and {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
