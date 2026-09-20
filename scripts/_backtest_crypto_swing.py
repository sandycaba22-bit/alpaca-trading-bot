"""Backtest swing cripto 1H/4H — IN-SAMPLE + OOS + variantes A/B si OOS negativo.

Uso:
  python scripts/_backtest_crypto_swing.py
  python scripts/_backtest_crypto_swing.py --scope is
  python scripts/_backtest_crypto_swing.py --scope oos
  python scripts/_backtest_crypto_swing.py --include-variants

Costos: 0.25%% comisión + 0.03%% slippage/lado = 0.56%% round-trip.
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
from bot.strategy.indicators import atr as atr_series

from _swing_backtest_lib import (  # noqa: E402
    BASELINE,
    CONFIRM_TF,
    DEFAULT_CRYPTO_FEE_PCT,
    DEFAULT_END,
    DEFAULT_SLIPPAGE_PCT,
    ENTRY_TF,
    HISTORY_START,
    OOS_START,
    SYMBOLS,
    SwingParams,
    load_1h_and_4h,
    precompute_entries,
    round_trip_cost_pct,
    simulate_swing,
    summarize_run,
    variant_key,
    VARIANT_A,
    VARIANT_B,
)

OUT_IS = PROJECT_ROOT / "logs" / "crypto_swing_is.csv"
OUT_OOS = PROJECT_ROOT / "logs" / "crypto_swing_oos.csv"
OUT_VARIANTS = PROJECT_ROOT / "logs" / "crypto_swing_variants.csv"

CSV_HEADER = (
    "scope,variant,symbol,entry_tf,confirm_tf,trades,win_rate_pct,profit_factor,"
    "total_return_net_pct,max_drawdown_pct,best_period,best_period_pct,"
    "worst_period,worst_period_pct,avg_tp_pct,crypto_fee_pct,slippage_pct"
)


def _run_symbol(
    settings,
    market: MarketDataService,
    symbol: str,
    params: SwingParams,
    *,
    scope: str,
    fee_pct: float,
    slippage_pct: float,
    end: pd.Timestamp,
) -> dict[str, object]:
    start_dt = datetime(HISTORY_START.year, HISTORY_START.month, HISTORY_START.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    bars, htf = load_1h_and_4h(market, symbol, start_dt, end_dt)
    if bars.empty or len(bars) < 300:
        print(f"skip {symbol}: insufficient bars")
        return {}

    vkey = variant_key(params)
    period_start = None
    period_end = None
    if scope == "is":
        period_start = HISTORY_START
        period_end = end
        # entries from full history but sim limited to IS window ending before OOS? 
        # User asked IS 2021->2026-09-20 full period as in-sample
    elif scope == "oos":
        period_start = OOS_START
        period_end = end
    else:
        period_start = HISTORY_START
        period_end = end

    entries = precompute_entries(
        symbol,
        bars,
        htf,
        params,
        variant_key=f"{vkey}_{scope}",
        period_start=OOS_START if scope == "oos" else None,
    )

    atr_s = atr_series(bars, params.atr_period)
    trades, equity = simulate_swing(
        settings,
        symbol,
        bars,
        entries,
        params,
        atr_s,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        period_start=period_start,
        period_end=period_end,
    )
    summary = summarize_run(symbol, trades, equity, bars, float(settings.backtest_cash))
    summary["scope"] = scope
    summary["variant"] = vkey
    summary["crypto_fee_pct"] = fee_pct
    summary["slippage_pct"] = slippage_pct
    return summary


def _row(summary: dict[str, object]) -> str:
    pf = summary.get("profit_factor", 0.0)
    pf_txt = "inf" if pf == float("inf") else f"{float(pf):.3f}"
    return (
        f"{summary.get('scope')},{summary.get('variant')},{summary.get('symbol')},"
        f"{summary.get('entry_tf')},{summary.get('confirm_tf')},"
        f"{summary.get('trades')},{float(summary.get('win_rate_pct', 0)):.1f},"
        f"{pf_txt},{float(summary.get('total_return_net_pct', 0)):.2f},"
        f"{float(summary.get('max_drawdown_pct', 0)):.2f},"
        f"{summary.get('best_period')},{float(summary.get('best_period_pct', 0)):.2f},"
        f"{summary.get('worst_period')},{float(summary.get('worst_period_pct', 0)):.2f},"
        f"{float(summary.get('avg_tp_pct', 0)):.2f},"
        f"{summary.get('crypto_fee_pct')},{summary.get('slippage_pct')}"
    )


def _print_table(rows: list[dict[str, object]], title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print(f"{'=' * 72}")
    hdr = (
        f"{'Scope':<6} {'Variant':<18} {'Symbol':<10} {'Trades':>6} {'Win%':>6} "
        f"{'PF':>6} {'Net%':>8} {'MaxDD%':>8} {'Best Q':>8} {'Worst Q':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for s in rows:
        pf = s.get("profit_factor", 0.0)
        pf_s = "inf" if pf == float("inf") else f"{float(pf):.2f}"
        print(
            f"{s.get('scope',''):<6} {str(s.get('variant','')):<18} {s.get('symbol',''):<10} "
            f"{int(s.get('trades', 0)):>6} {float(s.get('win_rate_pct', 0)):>5.1f}% "
            f"{pf_s:>6} {float(s.get('total_return_net_pct', 0)):>+7.2f}% "
            f"{float(s.get('max_drawdown_pct', 0)):>7.2f}% "
            f"{s.get('best_period',''):>8} {s.get('worst_period',''):>8}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest swing cripto 1H/4H")
    parser.add_argument("--scope", choices=("is", "oos", "all"), default="all")
    parser.add_argument("--end", default="2026-09-20", help="End date YYYY-MM-DD")
    parser.add_argument("--crypto-fee-pct", type=float, default=DEFAULT_CRYPTO_FEE_PCT)
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT)
    parser.add_argument(
        "--include-variants",
        action="store_true",
        help="Run variants A/B even if OOS is positive",
    )
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    args = parser.parse_args()

    end = pd.Timestamp(args.end, tz="UTC")
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)

    rt_cost = round_trip_cost_pct(args.crypto_fee_pct, args.slippage_pct)
    print(
        f"Swing 1H/4H | {HISTORY_START.date()}->{end.date()} | "
        f"OOS from {OOS_START.date()} | cost={rt_cost*100:.2f}% RT | "
        f"fee={args.crypto_fee_pct}% slip={args.slippage_pct}%/side"
    )

    is_rows: list[dict[str, object]] = []
    oos_rows: list[dict] = []
    variant_rows: list[dict] = []

    scopes = ("is", "oos") if args.scope == "all" else (args.scope,)
    for scope in scopes:
        for symbol in args.symbols:
            summary = _run_symbol(
                settings,
                market,
                symbol,
                BASELINE,
                scope=scope,
                fee_pct=args.crypto_fee_pct,
                slippage_pct=args.slippage_pct,
                end=end,
            )
            if summary:
                if scope == "is":
                    is_rows.append(summary)
                else:
                    oos_rows.append(summary)

    run_variants = args.include_variants
    if not run_variants and oos_rows:
        run_variants = any(float(r.get("total_return_net_pct", 0)) < 0 for r in oos_rows)

    if run_variants:
        print("\nOOS negativo o --include-variants: probando variantes A y B...")
        for params, label in ((VARIANT_A, "A"), (VARIANT_B, "B")):
            for symbol in args.symbols:
                for scope in scopes:
                    summary = _run_symbol(
                        settings,
                        market,
                        symbol,
                        params,
                        scope=scope,
                        fee_pct=args.crypto_fee_pct,
                        slippage_pct=args.slippage_pct,
                        end=end,
                    )
                    if summary:
                        summary["variant_label"] = label
                        variant_rows.append(summary)

    PROJECT_ROOT.joinpath("logs").mkdir(parents=True, exist_ok=True)
    if is_rows:
        OUT_IS.write_text(CSV_HEADER + "\n" + "\n".join(_row(r) for r in is_rows) + "\n", encoding="utf-8")
        _print_table(is_rows, f"IN-SAMPLE ({HISTORY_START.date()} -> {end.date()}) - baseline")
    if oos_rows:
        OUT_OOS.write_text(CSV_HEADER + "\n" + "\n".join(_row(r) for r in oos_rows) + "\n", encoding="utf-8")
        _print_table(oos_rows, f"OOS ({OOS_START.date()} -> {end.date()}) - baseline")
    if variant_rows:
        OUT_VARIANTS.write_text(
            CSV_HEADER + "\n" + "\n".join(_row(r) for r in variant_rows) + "\n",
            encoding="utf-8",
        )
        _print_table(variant_rows, "Variantes A (TP 5%/SL 2%) y B (doble breakout)")

    print(f"\nCSV: {OUT_IS.name}, {OUT_OOS.name}" + (f", {OUT_VARIANTS.name}" if variant_rows else ""))


if __name__ == "__main__":
    main()
