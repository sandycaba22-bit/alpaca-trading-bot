"""Phase 2A: grid régimen ADX 1D + volumen + momentum sobre swing 1H/4H (offline).

IS: 2021-01-01 -> 2025-01-01 | OOS: 2025-01-01 -> end
Costos maker: 0.15% + 0.01%/lado (RT 0.32%)
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings

from _swing_regime_lib import (
    ADX_GRID,
    DEFAULT_END,
    HISTORY_START,
    MAKER_FEE_PCT,
    MAKER_SLIPPAGE_PCT,
    OOS_START,
    RegimeFilterParams,
    SYMBOLS,
    run_regime_combo,
)

OUT = PROJECT_ROOT / "logs" / "swing_regime_grid.csv"

CSV_HEADER = (
    "scope,filter_key,adx_threshold,volume_confirm,momentum_confirm,symbol,entry_tf,confirm_tf,"
    "trades,win_rate_pct,profit_factor,total_return_net_pct,max_drawdown_pct,"
    "best_period,best_period_pct,worst_period,worst_period_pct,fee_pct,slippage_pct,round_trip_pct"
)


def _row(s: dict[str, object]) -> str:
    pf = s.get("profit_factor", 0.0)
    pf_txt = "inf" if pf == float("inf") else f"{float(pf):.3f}"
    return (
        f"{s.get('scope')},{s.get('filter_key')},{s.get('adx_threshold')},"
        f"{int(bool(s.get('volume_confirm')))},{int(bool(s.get('momentum_confirm')))},"
        f"{s.get('symbol')},1Hour,4H,{s.get('trades')},{float(s.get('win_rate_pct', 0)):.1f},"
        f"{pf_txt},{float(s.get('total_return_net_pct', 0)):.2f},"
        f"{float(s.get('max_drawdown_pct', 0)):.2f},"
        f"{s.get('best_period')},{float(s.get('best_period_pct', 0)):.2f},"
        f"{s.get('worst_period')},{float(s.get('worst_period_pct', 0)):.2f},"
        f"{s.get('fee_pct')},{s.get('slippage_pct')},{float(s.get('round_trip_pct', 0)):.4f}"
    )


def _print_scope_table(rows: list[dict], scope: str, title: str) -> None:
    scoped = [r for r in rows if r.get("scope") == scope]
    if not scoped:
        return
    print(f"\n{'=' * 96}")
    print(title)
    print(f"{'=' * 96}")
    hdr = (
        f"{'ADX':>4} {'Vol':>3} {'Mom':>3} {'Symbol':<10} {'Trades':>7} {'Win%':>6} "
        f"{'PF':>6} {'Net%':>8} {'MaxDD%':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for s in sorted(
        scoped,
        key=lambda r: (
            float(r.get("adx_threshold", 0)),
            not r.get("volume_confirm"),
            not r.get("momentum_confirm"),
            str(r.get("symbol", "")),
        ),
    ):
        pf = s.get("profit_factor", 0.0)
        pf_s = "inf" if pf == float("inf") else f"{float(pf):.2f}"
        print(
            f"{int(float(s.get('adx_threshold', 0))):>4} "
            f"{'Y' if s.get('volume_confirm') else 'N':>3} "
            f"{'Y' if s.get('momentum_confirm') else 'N':>3} "
            f"{s.get('symbol',''):<10} "
            f"{int(s.get('trades', 0)):>7} "
            f"{float(s.get('win_rate_pct', 0)):>5.1f}% "
            f"{pf_s:>6} "
            f"{float(s.get('total_return_net_pct', 0)):>+7.2f}% "
            f"{float(s.get('max_drawdown_pct', 0)):>7.2f}%"
        )


def _oos_winners(rows: list[dict]) -> list[dict]:
    """Combos OOS neto positivo en BTC y ETH."""
    by_key: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.get("scope") != "oos":
            continue
        key = str(r.get("filter_key"))
        sym = str(r.get("symbol"))
        by_key.setdefault(key, {})[sym] = float(r.get("total_return_net_pct", 0))
    winners = []
    for key, nets in by_key.items():
        if all(nets.get(s, -1.0) > 0 for s in SYMBOLS):
            winners.append({"filter_key": key, **nets})
    return winners


def main() -> None:
    parser = argparse.ArgumentParser(description="Grid régimen swing 1H/4H Phase 2A")
    parser.add_argument("--end", default=str(DEFAULT_END.date()))
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    args = parser.parse_args()

    end = pd.Timestamp(args.end, tz="UTC")
    settings = load_settings()
    market = MarketDataService(AlpacaClient(settings))

    rt = 2.0 * (MAKER_FEE_PCT + MAKER_SLIPPAGE_PCT)
    print(
        f"Swing régimen 2A | IS {HISTORY_START.date()}->{OOS_START.date()} | "
        f"OOS {OOS_START.date()}->{end.date()} | maker RT={rt:.2f}% | "
        f"grid ADX={list(ADX_GRID)} x vol on/off x mom on/off"
    )

    rows: list[dict] = []
    combos = list(
        itertools.product(ADX_GRID, (False, True), (False, True))
    )
    for adx_th, vol_on, mom_on in combos:
        regime = RegimeFilterParams(
            adx_threshold=adx_th,
            volume_confirm=vol_on,
            momentum_confirm=mom_on,
        )
        for scope in ("is", "oos"):
            for symbol in args.symbols:
                summary = run_regime_combo(
                    settings,
                    market,
                    symbol,
                    regime,
                    scope=scope,
                    end=end,
                )
                if summary:
                    rows.append(summary)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(CSV_HEADER + "\n" + "\n".join(_row(r) for r in rows) + "\n", encoding="utf-8")

    _print_scope_table(
        rows,
        "is",
        f"IN-SAMPLE ({HISTORY_START.date()} -> {OOS_START.date()}) — maker RT {rt:.2f}%",
    )
    _print_scope_table(
        rows,
        "oos",
        f"OOS ({OOS_START.date()} -> {end.date()}) — maker RT {rt:.2f}%",
    )

    winners = _oos_winners(rows)
    print(f"\n{'=' * 96}")
    print("CRITERIO EXITO: neto OOS > 0 en BTC y ETH")
    print(f"{'=' * 96}")
    if winners:
        for w in winners:
            print(
                f"  PASS {w['filter_key']} | BTC {w.get('BTC/USD', 0):+.2f}% | "
                f"ETH {w.get('ETH/USD', 0):+.2f}%"
            )
    else:
        print("  Ningun combo cumple criterio — Fase 2B NO procede.")
        best: dict[str, dict] = {}
        for r in rows:
            if r.get("scope") != "oos":
                continue
            key = str(r.get("filter_key"))
            sym = str(r.get("symbol"))
            cur = best.get(key, {"btc": -999.0, "eth": -999.0, "trades_btc": 0, "trades_eth": 0})
            net = float(r.get("total_return_net_pct", 0))
            tr = int(r.get("trades", 0))
            if sym == "BTC/USD":
                cur["btc"] = net
                cur["trades_btc"] = tr
            else:
                cur["eth"] = net
                cur["trades_eth"] = tr
            best[key] = cur
        if best:
            top = max(best.items(), key=lambda kv: kv[1]["btc"] + kv[1]["eth"])
            k, v = top
            print(
                f"  Mejor combo OOS (suma neto): {k} | BTC {v['btc']:+.2f}% ({v['trades_btc']} tr) | "
                f"ETH {v['eth']:+.2f}% ({v['trades_eth']} tr)"
            )

    print(f"\nCSV: {OUT.name} ({len(rows)} filas)")


if __name__ == "__main__":
    main()
