"""Backtest research: mean-reversion multi-activo (vol + horas muertas).

NO producción. Cripto: IS/OOS walk-forward 5 ventanas + split 335031.
Acciones: IS 2020-01-01 -> 2025-01-01 | OOS 2025-01-01 -> hoy.

  .venv\\Scripts\\python.exe -u scripts\\_backtest_multi_asset_meanrev.py
  .venv\\Scripts\\python.exe -u scripts\\_backtest_multi_asset_meanrev.py --stocks SLV TSLA

Salida: logs/multi_asset_meanrev_backtest.csv, logs/multi_asset_meanrev_backtest_summary.txt
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

from _multi_asset_meanrev_lib import default_stock_configs, metrics_stock_is_oos
from bot.universe import ELITE_STOCK_SYMBOLS
from _stocks_asymmetric_backtest_lib import CONFIRM_TF, ENTRY_TF, OOS_START, load_bars, pf_str

OUT_CSV = PROJECT_ROOT / "logs" / "multi_asset_meanrev_backtest.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "multi_asset_meanrev_backtest_summary.txt"

STOCKS_DEFAULT = ELITE_STOCK_SYMBOLS


def _row(
    asset: str,
    symbol: str,
    scope: str,
    window: int | None,
    m: dict[str, float],
) -> dict:
    return {
        "asset_class": asset,
        "symbol": symbol,
        "scope": scope,
        "window": window if window is not None else "",
        "trades": int(m.get("trades", 0)),
        "win_pct": round(float(m.get("win_rate_pct", 0)), 2),
        "pf": round(float(m.get("profit_factor", 0)), 4),
        "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
        "entries_signal": int(m.get("entries_signal", 0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Mean-rev multi-activo (research)")
    parser.add_argument("--stocks", nargs="*", default=list(STOCKS_DEFAULT))
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    market = MarketDataService(AlpacaClient(settings))
    print("=" * 96, flush=True)
    print("MULTI-ACTIVO MEAN-REV (BB+RSI) | vol>=MA | horas muertas | salidas asim", flush=True)
    print("Research only — no cablear a live/paper existente", flush=True)
    print("=" * 96, flush=True)

    rows: list[dict] = []

    for cfg in default_stock_configs(args.stocks):
        print(f"\n>> ACCIONES {cfg.symbol} tf={cfg.entry_tf}", flush=True)
        start_dt = cfg.is_start.to_pydatetime()
        end_dt = end.to_pydatetime()
        bars = load_bars(market, cfg.symbol, ENTRY_TF, start_dt, end_dt)
        htf = load_bars(market, cfg.symbol, CONFIRM_TF, start_dt, end_dt)
        if bars.empty or len(bars) < 500:
            print("  SKIP datos", flush=True)
            continue
        m_is, m_oos = metrics_stock_is_oos(settings, bars, htf, cfg, end=end)
        print(
            f"  IS tr={int(m_is['trades'])} PF={pf_str(float(m_is['profit_factor']))} net%={m_is['total_return_net_pct']:+.2f}",
            flush=True,
        )
        print(
            f"  OOS tr={int(m_oos['trades'])} PF={pf_str(float(m_oos['profit_factor']))} net%={m_oos['total_return_net_pct']:+.2f}",
            flush=True,
        )
        rows.append(_row("stock", cfg.symbol, "IS", None, m_is))
        rows.append(_row("stock", cfg.symbol, "OOS", None, m_oos))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "asset_class",
                "symbol",
                "scope",
                "window",
                "trades",
                "win_pct",
                "pf",
                "net_pct",
                "entries_signal",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    symbols_order = [s.upper() for s in args.stocks]
    lines = [
        "=== Multi-activo mean-rev backtest (research) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "Acciones: 5Min + 15Min | IS 2020-01-01->2025-01-01 | OOS 2025-01-01->hoy",
        "",
        f"{'Symbol':<10} {'Scope':<5} {'tr':>5} {'win%':>6} {'PF':>6} {'net%':>8}",
        "-" * 48,
    ]
    for sym in symbols_order:
        for scope in ("IS", "OOS"):
            match = [
                r
                for r in rows
                if r["symbol"] == sym and r["scope"] == scope and r["asset_class"] == "stock"
            ]
            if not match:
                continue
            r = match[0]
            lines.append(
                f"{sym:<10} {scope:<5} {r['trades']:>5} {r['win_pct']:>6.1f} "
                f"{r['pf']:>6.2f} {r['net_pct']:>+8.2f}"
            )
    lines.extend(["", "=== Candidatos OOS (PF>1 y net%>0, scope OOS split 335031) ==="])
    picks: list[str] = []
    for sym in symbols_order:
        oos = next((r for r in rows if r["symbol"] == sym and r["scope"] == "OOS" and r["asset_class"] == "stock"), None)
        if not oos or int(oos["trades"]) == 0:
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

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines), flush=True)
    print(f"\nWrote {OUT_CSV}", flush=True)
    print(f"Wrote {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
