"""Compara retorno neto offline: costos taker (0.25%+0.03%) vs maker (0.15%+0.01%) por lado.

No toca producción. Reutiliza señales swing 1H/4H ya cacheadas.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.alpaca.crypto_maker import (
    DEFAULT_CRYPTO_MAKER_FEE_PCT,
    DEFAULT_CRYPTO_MAKER_SLIPPAGE_PCT,
    DEFAULT_CRYPTO_TAKER_FEE_PCT,
    round_trip_cost_pct,
)
from bot.config import PROJECT_ROOT, load_settings
from bot.strategy.indicators import atr as atr_series

from _swing_backtest_lib import (
    BASELINE,
    DEFAULT_END,
    DEFAULT_SLIPPAGE_PCT,
    HISTORY_START,
    OOS_START,
    SYMBOLS,
    load_1h_and_4h,
    precompute_entries,
    simulate_swing,
    summarize_run,
)

OUT = PROJECT_ROOT / "logs" / "maker_taker_cost_compare.csv"

SCENARIOS = (
    ("taker", DEFAULT_CRYPTO_TAKER_FEE_PCT, DEFAULT_SLIPPAGE_PCT),
    ("maker", DEFAULT_CRYPTO_MAKER_FEE_PCT, DEFAULT_CRYPTO_MAKER_SLIPPAGE_PCT),
    ("maker_opt", DEFAULT_CRYPTO_MAKER_FEE_PCT, 0.0),
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara costos maker vs taker (offline)")
    parser.add_argument("--end", default="2026-09-20")
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    args = parser.parse_args()

    end = pd.Timestamp(args.end, tz="UTC")
    settings = load_settings()
    market = MarketDataService(AlpacaClient(settings))
    start_dt = datetime(HISTORY_START.year, HISTORY_START.month, HISTORY_START.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)

    rows: list[str] = []
    header = (
        "scenario,scope,symbol,fee_pct,slippage_pct,round_trip_pct,trades,win_rate_pct,"
        "profit_factor,total_return_net_pct,max_drawdown_pct"
    )

    print(f"\n{'=' * 80}")
    print("COSTOS MAKER vs TAKER — swing 1H/4H baseline (mismas señales, distinta fricción)")
    print(f"{'=' * 80}")
    print(
        f"{'Scenario':<12} {'Scope':<5} {'Symbol':<10} {'RT%':>6} {'Trades':>7} "
        f"{'Win%':>6} {'PF':>6} {'Net%':>8}"
    )
    print("-" * 80)

    for scope, period_start in (("is", None), ("oos", OOS_START)):
        for symbol in args.symbols:
            bars, htf = load_1h_and_4h(market, symbol, start_dt, end_dt)
            if bars.empty:
                continue
            vkey = f"baseline_{scope}"
            entries = precompute_entries(
                symbol, bars, htf, BASELINE, variant_key=vkey, period_start=period_start
            )
            atr_s = atr_series(bars, BASELINE.atr_period)

            for name, fee, slip in SCENARIOS:
                trades, equity = simulate_swing(
                    settings,
                    symbol,
                    bars,
                    entries,
                    BASELINE,
                    atr_s,
                    fee_pct=fee,
                    slippage_pct=slip,
                    period_start=HISTORY_START if scope == "is" else OOS_START,
                    period_end=end,
                )
                summary = summarize_run(symbol, trades, equity, bars, float(settings.backtest_cash))
                rt = round_trip_cost_pct(fee, slip) * 100.0
                pf = summary["profit_factor"]
                pf_s = "inf" if pf == float("inf") else f"{float(pf):.2f}"
                print(
                    f"{name:<12} {scope:<5} {symbol:<10} {rt:>5.2f}% "
                    f"{int(summary['trades']):>7} {float(summary['win_rate_pct']):>5.1f}% "
                    f"{pf_s:>6} {float(summary['total_return_net_pct']):>+7.2f}%"
                )
                rows.append(
                    f"{name},{scope},{symbol},{fee},{slip},{rt:.4f},"
                    f"{summary['trades']},{float(summary['win_rate_pct']):.1f},"
                    f"{pf_s},{float(summary['total_return_net_pct']):.2f},"
                    f"{float(summary['max_drawdown_pct']):.2f}"
                )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    print(f"\nCSV: {OUT}")
    print(
        "\nNota: maker_opt usa slippage 0% (ideal). En paper, medir fee real vía Activities CFEE "
        "cuando CRYPTO_MAKER_FIRST_ENABLED=true."
    )


if __name__ == "__main__":
    main()
