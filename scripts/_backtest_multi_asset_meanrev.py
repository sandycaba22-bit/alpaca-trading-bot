"""Backtest research: mean-reversion multi-activo (vol + horas muertas).

NO producción. Cripto: IS/OOS walk-forward 5 ventanas + split 335031.
Acciones: IS 2020-01-01 -> 2025-01-01 | OOS 2025-01-01 -> hoy.

  .venv\\Scripts\\python.exe -u scripts\\_backtest_multi_asset_meanrev.py
  .venv\\Scripts\\python.exe -u scripts\\_backtest_multi_asset_meanrev.py --crypto ETH/USD --stocks AAPL MSFT

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

from _crypto_asymmetric_backtest_lib import load_1h, summarize_trades
from _crypto_regime_entry_backtest_lib import walkforward_windows
from _multi_asset_meanrev_lib import (
    default_crypto_configs,
    default_stock_configs,
    metrics_crypto_period,
    metrics_stock_is_oos,
    run_crypto_meanrev_period,
)
from _stocks_asymmetric_backtest_lib import CONFIRM_TF, ENTRY_TF, OOS_START, load_bars, pf_str

OUT_CSV = PROJECT_ROOT / "logs" / "multi_asset_meanrev_backtest.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "multi_asset_meanrev_backtest_summary.txt"

CRYPTO_DEFAULT = ("ETH/USD", "BTC/USD")
STOCKS_DEFAULT = ("AAPL", "MSFT", "NVDA", "GOOGL", "META", "TSLA", "SLV")


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
    parser.add_argument("--crypto", nargs="*", default=list(CRYPTO_DEFAULT))
    parser.add_argument("--stocks", nargs="*", default=list(STOCKS_DEFAULT))
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    market = MarketDataService(AlpacaClient(settings))
    windows = walkforward_windows(end)

    print("=" * 96, flush=True)
    print("MULTI-ACTIVO MEAN-REV (BB+RSI) | vol>=MA | horas muertas | salidas asim", flush=True)
    print("Research only — no cablear a live/paper existente", flush=True)
    print("=" * 96, flush=True)

    rows: list[dict] = []

    for cfg in default_crypto_configs(args.crypto):
        print(f"\n>> CRIPTO {cfg.symbol} tf={cfg.entry_tf}", flush=True)
        bars = load_1h(market, cfg.symbol, cfg.is_start.to_pydatetime(), end.to_pydatetime())
        if bars.empty or len(bars) < 500:
            print("  SKIP datos", flush=True)
            continue
        is_end = OOS_START - pd.Timedelta(hours=1)
        m_is = metrics_crypto_period(settings, bars, cfg, start=cfg.is_start, end=is_end)
        m_oos_single = metrics_crypto_period(settings, bars, cfg, start=OOS_START, end=end)
        print(
            f"  IS split335031 tr={int(m_is['trades'])} PF={pf_str(float(m_is['profit_factor']))} "
            f"net%={m_is['total_return_net_pct']:+.2f} signals={int(m_is.get('entries_signal',0))}",
            flush=True,
        )
        print(
            f"  OOS split335031 tr={int(m_oos_single['trades'])} PF={pf_str(float(m_oos_single['profit_factor']))} "
            f"net%={m_oos_single['total_return_net_pct']:+.2f}",
            flush=True,
        )
        rows.append(_row("crypto", cfg.symbol, "IS", None, m_is))
        rows.append(_row("crypto", cfg.symbol, "OOS", None, m_oos_single))
        for wid, _a, _b, oos_start, oos_end in windows:
            trs = run_crypto_meanrev_period(
                settings, bars, cfg, period_start=oos_start, period_end=oos_end
            )
            m_w = summarize_trades(trs, float(settings.backtest_cash))
            print(
                f"  WF w{wid} OOS tr={int(m_w.get('trades',0))} PF={pf_str(float(m_w.get('profit_factor',0)))} "
                f"net%={m_w.get('total_return_net_pct',0):+.2f}",
                flush=True,
            )
            rows.append(_row("crypto", cfg.symbol, f"WF_OOS_{wid}", wid, m_w))

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

    lines = [
        "=== Multi-activo mean-rev backtest (research) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"{'Cls':<7} {'Sym':<10} {'Scope':<10} {'tr':>5} {'win%':>6} {'PF':>6} {'net%':>8}",
        "-" * 56,
    ]
    for r in rows:
        if str(r["scope"]).startswith("WF"):
            continue
        lines.append(
            f"{r['asset_class']:<7} {r['symbol']:<10} {r['scope']:<10} {r['trades']:>5} "
            f"{r['win_pct']:>6.1f} {r['pf']:>6.2f} {r['net_pct']:>+8.2f}"
        )

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {OUT_CSV}", flush=True)
    print(f"Wrote {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
