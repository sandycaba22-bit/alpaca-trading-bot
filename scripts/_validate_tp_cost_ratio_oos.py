"""Fase 1 — Validación OOS MIN_TP_TO_COST_RATIO (2025-01-01 → hoy, offline).

Misma grid y costos que el sweep in-sample. Confirma que el ratio ganador
no es sobreajuste. Salida: logs/tp_cost_ratio_validation_2025.csv
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
    OOS_START,
    RATIO_GRID,
    SYMBOLS,
    default_window,
    sweep_symbol_combo,
)

OUT = PROJECT_ROOT / "logs" / "tp_cost_ratio_validation_2025.csv"
SWEEP = PROJECT_ROOT / "logs" / "tp_cost_ratio_sweep.csv"


def _load_is_best() -> dict[tuple[str, str, str], str]:
    """Mejor ratio in-sample (neto max, excl. baseline, min 10 trades) por filter_source."""
    if not SWEEP.exists():
        return {}
    lines = SWEEP.read_text(encoding="utf-8").strip().splitlines()
    best: dict[tuple[str, str, str], tuple[str, float, int]] = {}
    for line in lines[1:]:
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
    return {k: v[0] for k, v in best.items()}


def _print_comparison(rows: list[str], is_best: dict[tuple[str, str, str], str]) -> None:
    print("\n=== IN-SAMPLE vs OOS (ratio óptimo IS por símbolo × TF × filter) ===")
    print(
        f"{'symbol':10} {'tf':6} {'filter':8} {'ratio':8} {'IS net%':>8} "
        f"{'OOS net%':>9} {'OOS tr':>7} {'OK?':>5}"
    )
    print("-" * 78)

    is_rows = {}
    if SWEEP.exists():
        for line in SWEEP.read_text(encoding="utf-8").strip().splitlines()[1:]:
            p = line.split(",")
            if len(p) >= 16 and p[4] == "in_sample":
                is_rows[(p[0], p[2], p[5], p[1])] = p

    oos_by_key: dict[tuple[str, str, str, str], str] = {}
    for line in rows[1:]:
        p = line.split(",")
        if len(p) >= 16 and p[4] == "oos":
            oos_by_key[(p[0], p[2], p[5], p[1])] = p

    for (sym, etf, fsrc), ratio in sorted(is_best.items()):
        is_p = is_rows.get((sym, etf, fsrc, ratio))
        oos_p = oos_by_key.get((sym, etf, fsrc, ratio))
        is_net = f"{float(is_p[14]):+.2f}" if is_p else "n/a"
        if oos_p:
            oos_net = float(oos_p[14])
            oos_tr = oos_p[10]
            ok = "SI" if oos_net > 0 else "NO"
            print(f"{sym:10} {etf:6} {fsrc:8} {ratio:8} {is_net:>8} {oos_net:>+8.2f}% {oos_tr:>7} {ok:>5}")
        else:
            print(f"{sym:10} {etf:6} {fsrc:8} {ratio:8} {is_net:>8} {'n/a':>9} {'n/a':>7} {'n/a':>5}")

    print("\n=== TABLA OOS COMPLETA (neto) ===")
    print(
        f"{'symbol':10} {'tf':6} {'filter':8} {'ratio':8} {'trades':>6} {'filt%':>6} "
        f"{'wr%':>6} {'PF':>6} {'net%':>8}"
    )
    print("-" * 78)
    for line in rows[1:]:
        p = line.split(",")
        if len(p) < 16 or p[4] != "oos":
            continue
        sym, ratio, etf, fsrc = p[0], p[1], p[2], p[5]
        total, filt, trades, wr, pf, net = p[8], p[9], p[10], p[11], p[12], p[14]
        filt_pct = f"{100*int(filt)/int(total):.0f}" if int(total) else "0"
        print(f"{sym:10} {etf:6} {fsrc:8} {ratio:8} {trades:>6} {filt_pct:>5}% {wr:>6} {pf:>6} {net:>7}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="OOS validation MIN_TP_TO_COST_RATIO 2025+")
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
    )
    args = parser.parse_args()

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    start, end = default_window()
    ratio_grid = tuple(args.ratios)

    rows: list[str] = [CSV_HEADER]
    print(
        f"TP/cost OOS {OOS_START.date()}->{end.date()} "
        f"ratios={ratio_grid} fee={args.crypto_fee_pct}% slip={args.slippage_pct}%/side"
    )

    for entry_tf in args.entry_tfs:
        print(f"\n--- {entry_tf} ---")
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
                        scope="oos",
                        ratio_grid=ratio_grid,
                        filter_source=filter_source,
                    )
                )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(rows) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(f"\nwrote {OUT}")
    _print_comparison(rows, _load_is_best())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
