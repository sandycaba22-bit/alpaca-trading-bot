"""Sweep vol_2x acciones — 5Min + 15Min, IS/OOS (research).

  python -u scripts/_sweep_vol2x_universe.py --universe top50
  python -u scripts/_sweep_vol2x_universe.py --universe elite
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
from bot.universe import UNIVERSE_CHOICES, symbols_for_universe

from _stocks_asymmetric_backtest_lib import (  # noqa: E402
    CONFIRM_TF,
    ENTRY_TF,
    EntryVariant,
    OOS_START,
    evaluate_variant_symbol,
    load_bars,
    pf_str,
)

STOCK_IS_START = pd.Timestamp("2020-01-01", tz="UTC")
VOL2X = EntryVariant(name="vol_2x", volume_mult=2.0, volume_period=20, sma_slow=50)
STOCK_FEE = 0.0
STOCK_SLIP = 0.03

OUT_PATHS = {
    "elite": (
        PROJECT_ROOT / "logs" / "vol2x_elite_sweep.csv",
        PROJECT_ROOT / "logs" / "vol2x_elite_sweep_summary.txt",
    ),
    "top50": (
        PROJECT_ROOT / "logs" / "vol2x_top50_sweep.csv",
        PROJECT_ROOT / "logs" / "vol2x_top50_sweep_summary.txt",
    ),
}


def _print_metrics(scope: str, symbol: str, m: dict[str, float]) -> None:
    print(
        f"  {scope:<4} {symbol:<8} vol_2x | tr={int(m.get('trades', 0)):>4} | "
        f"win%={m.get('win_rate_pct', 0):.1f} | PF={pf_str(float(m.get('profit_factor', 0)))} | "
        f"net%={m.get('total_return_net_pct', 0):+.2f}",
        flush=True,
    )


def _append_row(rows: list[dict], *, symbol: str, scope: str, m: dict[str, float]) -> None:
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


def _summary_lines(rows: list[dict], stock_symbols: list[str], universe: str) -> list[str]:
    lines = [
        f"=== vol_2x sweep ({universe}) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "IS 2020-01-01->2025-01-01 | OOS 2025-01-01->hoy | SMA50 | salidas 1.2/2.75",
        "",
        f"{'Symbol':<10} {'Scope':<5} {'tr':>5} {'win%':>6} {'PF':>6} {'net%':>8}",
        "-" * 48,
    ]
    for sym in stock_symbols:
        for scope in ("IS", "OOS"):
            match = [r for r in rows if r["symbol"] == sym and r["scope"] == scope]
            if not match:
                continue
            r = match[0]
            lines.append(
                f"{sym:<10} {scope:<5} {r['trades']:>5} {r['win_pct']:>6.1f} "
                f"{r['pf']:>6.2f} {r['net_pct']:>+8.2f}"
            )
    lines.extend(["", "=== Candidatos OOS (PF>1 y net%>0) ==="])
    picks: list[str] = []
    for sym in stock_symbols:
        oos = next((r for r in rows if r["symbol"] == sym and r["scope"] == "OOS"), None)
        if not oos or int(oos["trades"]) == 0:
            lines.append(f"  --  {sym:<10} (sin trades OOS)")
            continue
        if float(oos["pf"]) > 1.0 and float(oos["net_pct"]) > 0:
            picks.append(sym)
            lines.append(
                f"  OK  {sym:<10} tr={oos['trades']:>4} PF={oos['pf']:.2f} net%={oos['net_pct']:+.2f}"
            )
        else:
            lines.append(
                f"  --  {sym:<10} tr={oos['trades']:>4} PF={oos['pf']:.2f} net%={oos['net_pct']:+.2f}"
            )
    lines.append("")
    lines.append(f"Pasaron OOS: {', '.join(picks) if picks else '(ninguno)'}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None, help="Fin OOS UTC (default: ahora)")
    parser.add_argument(
        "--universe",
        choices=sorted(UNIVERSE_CHOICES),
        default="top50",
        help="elite=6 simbolos live | top50=large caps",
    )
    parser.add_argument("--stock-symbols", nargs="+", default=None, help="Override lista")
    args = parser.parse_args()

    universe = args.universe.strip().lower()
    out_csv, out_txt = OUT_PATHS[universe]
    stock_symbols = (
        [s.upper() for s in args.stock_symbols]
        if args.stock_symbols
        else list(symbols_for_universe(universe))
    )

    end = (
        pd.Timestamp(args.end, tz="UTC")
        if args.end
        else pd.Timestamp(datetime.now(timezone.utc))
    )

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    sma_map = {s.upper(): 50 for s in stock_symbols}

    print("=" * 88, flush=True)
    print(f"SWEEP vol_2x | universo={universe} | n={len(stock_symbols)}", flush=True)
    print(
        f"IS {STOCK_IS_START.date()} -> {OOS_START.date()} | OOS {OOS_START.date()} -> {end.date()}",
        flush=True,
    )
    print("=" * 88, flush=True)

    rows: list[dict] = []
    start_dt = STOCK_IS_START.to_pydatetime()
    end_dt = end.to_pydatetime()

    for symbol in stock_symbols:
        print(f"\n>> {symbol}", flush=True)
        bars = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
        htf = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)
        if bars.empty or len(bars) < 500:
            print(f"  SKIP {symbol}: datos insuficientes (bars={len(bars)})", flush=True)
            continue
        m_is, m_oos = evaluate_variant_symbol(
            settings,
            bars,
            htf,
            symbol,
            sma_map,
            VOL2X,
            start=STOCK_IS_START,
            end=end,
            fee_pct=STOCK_FEE,
            slippage_pct=STOCK_SLIP,
        )
        _print_metrics("IS", symbol, m_is)
        _print_metrics("OOS", symbol, m_oos)
        _append_row(rows, symbol=symbol, scope="IS", m=m_is)
        _append_row(rows, symbol=symbol, scope="OOS", m=m_oos)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["symbol", "scope", "variant", "trades", "win_pct", "pf", "net_pct"],
        )
        w.writeheader()
        w.writerows(rows)

    summary = _summary_lines(rows, stock_symbols, universe)
    out_txt.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary), flush=True)
    print(f"\nWrote {out_csv} and {out_txt}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
