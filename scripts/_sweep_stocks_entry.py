"""Sweep de filtros de entrada acciones (5Min + 15Min) — salidas asimétricas fijas.

  .venv/bin/python -u scripts/_sweep_stocks_entry.py

IS: ~2020-09 → 2025-01 | OOS: 2025-01 → hoy | costos 0.03%/lado | AAPL + MSFT
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings

from _stocks_asymmetric_backtest_lib import (  # noqa: E402
    CONFIRM_TF,
    ENTRY_TF,
    EntryVariant,
    OOS_START,
    SYMBOLS,
    _sma_slow_map,
    evaluate_variant_symbol,
    load_bars,
    pf_str,
)

FEE = 0.0
SLIP = 0.03
YEARS = 6
OUT_CSV = PROJECT_ROOT / "logs" / "stocks_entry_sweep.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "stocks_entry_sweep_summary.txt"


def _build_variants() -> list[EntryVariant]:
    variants: list[EntryVariant] = [
        EntryVariant(name="baseline_entry"),
    ]
    for mult in (1.2, 1.5, 2.0):
        variants.append(EntryVariant(name=f"vol_{mult:g}x", volume_mult=mult))
    for th in (20.0, 25.0, 30.0):
        variants.append(EntryVariant(name=f"adx_{int(th)}", adx_min=th))
    for slow in (40, 50, 60, 70, 80):
        variants.append(EntryVariant(name=f"sma_slow_{slow}", sma_slow=slow))
    return variants


def _print_metrics(scope: str, symbol: str, variant: str, m: dict[str, float]) -> None:
    print(
        f"  {scope:<4} {symbol:<5} {variant:<16} | tr={int(m.get('trades', 0)):>4} | "
        f"win%={m.get('win_rate_pct', 0):.1f} | PF={pf_str(float(m.get('profit_factor', 0)))} | "
        f"net%={m.get('total_return_net_pct', 0):+.2f} | "
        f"avgW=${m.get('avg_win_net', 0):+.2f} | avgL=${m.get('avg_loss_net', 0):+.2f} | "
        f"W/L={m.get('avg_win_r', 0):.2f}x"
    )


def _mean_oos_pf(rows: list[dict], variant: str) -> float:
    pfs = [
        float(r["pf"])
        for r in rows
        if r["variant"] == variant and r["scope"] == "OOS" and int(r["trades"]) > 0
    ]
    return sum(pfs) / len(pfs) if pfs else 0.0


def _combo_variants(rows: list[dict], base: list[EntryVariant]) -> list[EntryVariant]:
    singles = [v for v in base if v.name != "baseline_entry"]
    by_name = {v.name: v for v in singles}
    ranked = sorted(
        {r["variant"] for r in rows if r["variant"] != "baseline_entry"},
        key=lambda n: _mean_oos_pf(rows, n),
        reverse=True,
    )
    if len(ranked) < 2:
        return []
    combos: list[EntryVariant] = []

    def pick(prefix: str) -> str | None:
        for name in ranked:
            if name.startswith(prefix):
                return name
        return None

    v_vol = pick("vol_")
    v_adx = pick("adx_")
    v_sma = pick("sma_")

    def merge(name: str, parts: list[EntryVariant | None]) -> EntryVariant:
        vol = adx = sma = None
        vol_p = 20
        for p in parts:
            if p is None:
                continue
            if p.volume_mult is not None:
                vol = p.volume_mult
                vol_p = p.volume_period
            if p.adx_min is not None:
                adx = p.adx_min
            if p.sma_slow is not None:
                sma = p.sma_slow
        return EntryVariant(
            name=name,
            volume_mult=vol,
            volume_period=vol_p,
            adx_min=adx,
            sma_slow=sma,
        )

    if v_vol and v_adx:
        combos.append(merge("combo_vol_adx", [by_name.get(v_vol), by_name.get(v_adx)]))
    if v_vol and v_sma:
        combos.append(merge("combo_vol_sma", [by_name.get(v_vol), by_name.get(v_sma)]))
    if v_adx and v_sma:
        combos.append(merge("combo_adx_sma", [by_name.get(v_adx), by_name.get(v_sma)]))
    if v_vol and v_adx and v_sma:
        combos.append(
            merge(
                "combo_vol_adx_sma",
                [by_name.get(v_vol), by_name.get(v_adx), by_name.get(v_sma)],
            )
        )
    return combos


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None)
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    parser.add_argument("--years", type=float, default=YEARS)
    parser.add_argument("--skip-combos", action="store_true")
    args = parser.parse_args()

    end = (
        pd.Timestamp(args.end, tz="UTC")
        if args.end
        else pd.Timestamp(datetime.now(timezone.utc))
    )
    start = end - timedelta(days=int(args.years * 365.25))

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    sma_map = _sma_slow_map(settings)

    print("=" * 88)
    print("SWEEP ENTRADAS ACCIONES | salidas fijas: SL 1.2x ATR + trail 2.75x (sin TP %)")
    print(f"IS: {start.date()} -> {OOS_START.date()} | OOS: {OOS_START.date()} -> {end.date()}")
    print(f"SMA lenta baseline: {sma_map} | costos: {FEE}% + {SLIP}%/lado")
    print("=" * 88)

    bar_cache: dict[str, pd.DataFrame] = {}
    htf_cache: dict[str, pd.DataFrame] = {}
    start_dt = start.to_pydatetime()
    end_dt = end.to_pydatetime()
    for symbol in args.symbols:
        bar_cache[symbol] = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
        htf_cache[symbol] = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)

    variants = _build_variants()
    rows: list[dict] = []

    for variant in variants:
        print(f"\n--- {variant.name} ---")
        for symbol in args.symbols:
            bars = bar_cache[symbol]
            htf = htf_cache[symbol]
            if bars.empty or len(bars) < 500:
                continue
            m_is, m_oos = evaluate_variant_symbol(
                settings,
                bars,
                htf,
                symbol,
                sma_map,
                variant if variant.name != "baseline_entry" else None,
                start=start,
                end=end,
                fee_pct=FEE,
                slippage_pct=SLIP,
            )
            _print_metrics("IS", symbol, variant.name, m_is)
            _print_metrics("OOS", symbol, variant.name, m_oos)
            for scope, m in (("IS", m_is), ("OOS", m_oos)):
                rows.append(
                    {
                        "variant": variant.name,
                        "symbol": symbol,
                        "scope": scope,
                        "trades": int(m.get("trades", 0)),
                        "win_pct": round(float(m.get("win_rate_pct", 0)), 2),
                        "pf": round(float(m.get("profit_factor", 0)), 4),
                        "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
                        "avg_win": round(float(m.get("avg_win_net", 0)), 4),
                        "avg_loss": round(float(m.get("avg_loss_net", 0)), 4),
                        "wl_ratio": round(float(m.get("avg_win_r", 0)), 4),
                        "oos_pf": round(float(m_oos.get("profit_factor", 0)), 4),
                        "oos_trades": int(m_oos.get("trades", 0)),
                    }
                )

    if not args.skip_combos:
        combo_list = _combo_variants(rows, variants)
        for variant in combo_list:
            print(f"\n--- {variant.name} ---")
            for symbol in args.symbols:
                m_is, m_oos = evaluate_variant_symbol(
                    settings,
                    bar_cache[symbol],
                    htf_cache[symbol],
                    symbol,
                    sma_map,
                    variant,
                    start=start,
                    end=end,
                    fee_pct=FEE,
                    slippage_pct=SLIP,
                )
                _print_metrics("IS", symbol, variant.name, m_is)
                _print_metrics("OOS", symbol, variant.name, m_oos)
                for scope, m in (("IS", m_is), ("OOS", m_oos)):
                    rows.append(
                        {
                            "variant": variant.name,
                            "symbol": symbol,
                            "scope": scope,
                            "trades": int(m.get("trades", 0)),
                            "win_pct": round(float(m.get("win_rate_pct", 0)), 2),
                            "pf": round(float(m.get("profit_factor", 0)), 4),
                            "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
                            "avg_win": round(float(m.get("avg_win_net", 0)), 4),
                            "avg_loss": round(float(m.get("avg_loss_net", 0)), 4),
                            "wl_ratio": round(float(m.get("avg_win_r", 0)), 4),
                            "oos_pf": round(float(m_oos.get("profit_factor", 0)), 4),
                            "oos_trades": int(m_oos.get("trades", 0)),
                        }
                    )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "variant",
        "symbol",
        "scope",
        "trades",
        "win_pct",
        "pf",
        "net_pct",
        "avg_win",
        "avg_loss",
        "wl_ratio",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    summary_lines = _summary_table(rows, args.symbols)
    OUT_TXT.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary_lines))
    print(f"\nWrote {OUT_CSV} and {OUT_TXT}")
    return 0


def _summary_table(rows: list[dict], symbols: list[str]) -> list[str]:
    variants = sorted({r["variant"] for r in rows})
    lines = [
        "=== RESUMEN OOS (salidas asimétricas fijas) ===",
        f"{'Variante':<22} | {'AAPL PF':>7} {'AAPL net%':>9} | {'MSFT PF':>7} {'MSFT net%':>9} | "
        f"{'PF>OOS ambos':>12} {'PF>1 OOS ambos':>14}",
        "-" * 88,
    ]
    winners: list[str] = []
    for var in variants:
        cells: list[str] = []
        pf_oos_both = True
        pf_gt1_both = True
        for sym in symbols:
            oos = next(
                (r for r in rows if r["variant"] == var and r["symbol"] == sym and r["scope"] == "OOS"),
                None,
            )
            if not oos or int(oos["trades"]) == 0:
                cells.extend(["n/a", "n/a"])
                pf_oos_both = False
                pf_gt1_both = False
                continue
            pf = float(oos["pf"])
            net = float(oos["net_pct"])
            cells.append(f"{pf:.2f}")
            cells.append(f"{net:+.2f}")
            if pf <= 0:
                pf_oos_both = False
            if pf <= 1.0:
                pf_gt1_both = False
        flag = "YES" if pf_gt1_both else "no"
        if pf_gt1_both:
            winners.append(var)
        lines.append(
            f"{var:<22} | {cells[0]:>7} {cells[1]:>9} | {cells[2]:>7} {cells[3]:>9} | "
            f"{'ok' if pf_oos_both else '—':>12} {flag:>14}"
        )

    lines.extend(
        [
            "",
            "=== RECOMENDACIÓN (research only — no live) ===",
        ]
    )
    if winners:
        lines.append(
            f"Candidatos con PF>1 OOS en todos los símbolos: {', '.join(winners)}"
        )
    else:
        lines.append(
            "Ninguna variante alcanza PF>1 OOS en AAPL y MSFT a la vez. "
            "Priorizar la de mayor PF OOS medio sin empeorar net% vs baseline (~0.63 PF)."
        )
        ranked = sorted(
            variants,
            key=lambda v: _mean_oos_pf(rows, v),
            reverse=True,
        )
        top3 = ranked[:3]
        lines.append(f"Top OOS PF medio: {', '.join(top3)}")
        lines.append(
            "Implementar en vivo solo tras re-validar con .env.stocks real y walk-forward; "
            "mantener STOCK_ASYMMETRIC_EXITS_ENABLED=false hasta entonces."
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
