"""Fase 1 — Calibración in-sample MIN_TP_TO_COST_RATIO (6 años, offline).

Barrido fino del ratio TP/costo para BTC/USD y ETH/USD en 6Min y 1Hour.
Costos: 0.25%% comisión + 0.03%% slippage por lado (0.56%% ida y vuelta).
Salida: logs/tp_cost_ratio_sweep.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SCRIPTS))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings

from _tp_cost_ratio_lib import (  # noqa: E402
    CSV_HEADER,
    DEFAULT_CRYPTO_FEE_PCT,
    DEFAULT_SLIPPAGE_PCT,
    ENTRY_TFS,
    RATIO_GRID,
    SYMBOLS,
    default_window,
    sweep_symbol_combo,
)

OUT = PROJECT_ROOT / "logs" / "tp_cost_ratio_sweep.csv"


def _print_summary(rows: list[str]) -> None:
    print("\n=== RESUMEN IN-SAMPLE (neto con costos, trailing prod 2 etapas) ===")
    print(
        f"{'symbol':10} {'tf':6} {'filter':8} {'ratio':8} {'trades':>6} {'filt%':>6} "
        f"{'wr%':>6} {'PF':>6} {'net%':>8}"
    )
    print("-" * 78)
    for line in rows[1:]:
        p = line.split(",")
        if len(p) < 16 or p[4] != "in_sample":
            continue
        sym, ratio, etf, fsrc = p[0], p[1], p[2], p[5]
        total, filt, trades, wr, pf, net = p[8], p[9], p[10], p[11], p[12], p[14]
        filt_pct = f"{100*int(filt)/int(total):.0f}" if int(total) else "0"
        print(f"{sym:10} {etf:6} {fsrc:8} {ratio:8} {trades:>6} {filt_pct:>5}% {wr:>6} {pf:>6} {net:>7}%")

    print("\n=== MEJOR ratio neto positivo por símbolo × TF × filter_source ===")
    best: dict[tuple[str, str, str], tuple[str, float, int]] = {}
    for line in rows[1:]:
        p = line.split(",")
        if len(p) < 16 or p[4] != "in_sample" or p[1] == "baseline":
            continue
        key = (p[0], p[2], p[5])
        net = float(p[14])
        trades = int(p[10])
        if trades < 10:
            continue
        prev = best.get(key)
        if prev is None or net > prev[1]:
            best[key] = (p[1], net, trades)
    if not best:
        print("Ningún ratio con neto positivo y >=10 trades.")
    for (sym, etf, fsrc), (ratio, net, tr) in sorted(best.items()):
        flag = "OK" if net > 0 else "—"
        print(f"{sym} {etf:6} {fsrc:8} | ratio={ratio} net={net:+.2f}% trades={tr} [{flag}]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep MIN_TP_TO_COST_RATIO in-sample (6 años)")
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    parser.add_argument("--entry-tfs", nargs="*", default=list(ENTRY_TFS))
    parser.add_argument("--ratios", nargs="*", type=float, default=list(RATIO_GRID))
    parser.add_argument("--crypto-fee-pct", type=float, default=DEFAULT_CRYPTO_FEE_PCT)
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT)
    parser.add_argument(
        "--filter-sources",
        nargs="*",
        default=["levels", "atr_raw"],
        choices=["levels", "atr_raw"],
        help="levels=TP con piso prod (Phase 2 exacto); atr_raw=ATR puro (diagnóstico)",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    start, end = default_window()
    ratio_grid = tuple(args.ratios)

    rows: list[str] = [CSV_HEADER]
    print(
        f"TP/cost sweep IN-SAMPLE {start.date()}->{end.date()} "
        f"ratios={ratio_grid} fee={args.crypto_fee_pct}% slip={args.slippage_pct}%/side"
    )

    for entry_tf in args.entry_tfs:
        print(f"\n--- {entry_tf} confirm={ {'6Min': '15Min', '1Hour': '1Day'}.get(entry_tf, '?')} ---")
        for filter_source in args.filter_sources:
            print(f"  filter_source={filter_source}")
            for symbol in args.symbols:
                rows.extend(
                    sweep_symbol_combo(
                        settings,
                        market,
                        symbol,
                        entry_tf,
                        start,
                        end,
                        fee_pct=args.crypto_fee_pct,
                        slippage_pct=args.slippage_pct,
                        scope="in_sample",
                        ratio_grid=ratio_grid,
                        filter_source=filter_source,
                    )
                )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(rows) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(f"\nwrote {OUT}")
    _print_summary(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
