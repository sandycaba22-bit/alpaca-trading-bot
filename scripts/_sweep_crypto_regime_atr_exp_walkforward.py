"""Sweep ATR expansion ETH régimen — walk-forward 5 ventanas (research).

  .venv\\Scripts\\python.exe -u scripts\\_sweep_crypto_regime_atr_exp_walkforward.py
"""

from __future__ import annotations

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

from _crypto_asymmetric_backtest_lib import load_1h
from _crypto_regime_entry_backtest_lib import (
    CRYPTO_IS_START,
    gate_sma50_atr_exp,
    metrics_for_period,
    walkforward_windows,
)
from _stocks_asymmetric_backtest_lib import pf_str

SYMBOL = "ETH/USD"
ATR_EXP_VALUES = (1.20, 1.15, 1.10, 1.05)
CRYPTO_FEE = 0.25
CRYPTO_SLIP = 0.03
OUT_CSV = PROJECT_ROOT / "logs" / "crypto_regime_atr_exp_walkforward_sweep.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "crypto_regime_atr_exp_walkforward_sweep_summary.txt"


def _fmt_ts(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d")


def _oos_weeks(windows: list, end: pd.Timestamp) -> float:
    total_h = 0.0
    for _wid, _a, _b, oos_start, oos_end in windows:
        total_h += max(0.0, (oos_end - oos_start).total_seconds() / 3600.0)
    return max(total_h / (24.0 * 7.0), 1e-6)


def main() -> int:
    end = pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    windows = walkforward_windows(end)
    if not windows:
        print("Sin ventanas walk-forward.", flush=True)
        return 1

    bars = load_1h(market, SYMBOL, CRYPTO_IS_START.to_pydatetime(), end.to_pydatetime())
    if bars.empty or len(bars) < 500:
        print("Datos ETH insuficientes.", flush=True)
        return 1

    oos_weeks = _oos_weeks(windows, end)
    rows: list[dict] = []
    summary_rows: list[dict] = []

    print("=" * 96, flush=True)
    print(f"SWEEP ATR EXP | {SYMBOL} | walk-forward {len(windows)} ventanas | costos {CRYPTO_FEE}+{CRYPTO_SLIP}%/lado", flush=True)
    print("=" * 96, flush=True)

    for atr_exp in ATR_EXP_VALUES:
        gate = gate_sma50_atr_exp(atr_exp)
        oos_pass = 0
        oos_trades = 0
        print(f"\n### ATR_EXP={atr_exp:.2f} ({gate.name}) ###", flush=True)
        for wid, is_start, is_end, oos_start, oos_end in windows:
            m_is = metrics_for_period(
                settings,
                bars,
                SYMBOL,
                gate,
                scope_start=is_start,
                scope_end=is_end,
                fee_pct=CRYPTO_FEE,
                slippage_pct=CRYPTO_SLIP,
            )
            m_oos = metrics_for_period(
                settings,
                bars,
                SYMBOL,
                gate,
                scope_start=oos_start,
                scope_end=oos_end,
                fee_pct=CRYPTO_FEE,
                slippage_pct=CRYPTO_SLIP,
            )
            for scope, m, p0, p1 in (
                ("IS", m_is, is_start, is_end),
                ("OOS", m_oos, oos_start, oos_end),
            ):
                tr = int(m.get("trades", 0))
                win = float(m.get("win_rate_pct", 0))
                pf = float(m.get("profit_factor", 0))
                net = float(m.get("total_return_net_pct", 0))
                print(
                    f"  w{wid} {scope} {_fmt_ts(p0)}->{_fmt_ts(p1)} | tr={tr:>4} | win%={win:.1f} | "
                    f"PF={pf_str(pf)} | net%={net:+.2f}",
                    flush=True,
                )
                rows.append(
                    {
                        "atr_exp": atr_exp,
                        "window": wid,
                        "scope": scope,
                        "period_start": _fmt_ts(p0),
                        "period_end": _fmt_ts(p1),
                        "trades": tr,
                        "win_pct": round(win, 2),
                        "pf": round(pf, 4),
                        "net_pct": round(net, 4),
                    }
                )
            tr_o = int(m_oos.get("trades", 0))
            pf_o = float(m_oos.get("profit_factor", 0))
            net_o = float(m_oos.get("total_return_net_pct", 0))
            oos_trades += tr_o
            if tr_o > 0 and pf_o > 1.0 and net_o > 0:
                oos_pass += 1

        sig_per_week = oos_trades / oos_weeks
        summary_rows.append(
            {
                "atr_exp": atr_exp,
                "oos_pass": oos_pass,
                "oos_windows": len(windows),
                "oos_trades_total": oos_trades,
                "signals_per_week": round(sig_per_week, 3),
            }
        )
        print(
            f"  >> OOS pass {oos_pass}/{len(windows)} | trades OOS={oos_trades} | ~{sig_per_week:.2f} señales/semana",
            flush=True,
        )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "atr_exp",
                "window",
                "scope",
                "period_start",
                "period_end",
                "trades",
                "win_pct",
                "pf",
                "net_pct",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    lines = [
        "=== Sweep ATR expansion ETH walk-forward ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        f"Criterio OOS: tr>0, PF>1, net%>0 | meta: >=4/{len(windows)} ventanas",
        "",
        f"{'ATR':>5} {'pass':>8} {'OOS tr':>7} {'sig/wk':>8}",
        "-" * 32,
    ]
    best = None
    for s in summary_rows:
        lines.append(
            f"{s['atr_exp']:>5.2f} {s['oos_pass']}/{s['oos_windows']:>5} {s['oos_trades_total']:>7} "
            f"{s['signals_per_week']:>8.2f}"
        )
        if s["oos_pass"] >= 4 and (best is None or s["signals_per_week"] > best["signals_per_week"]):
            best = s

    lines.extend(["", "=== Detalle por ventana (OOS) ==="])
    for atr_exp in ATR_EXP_VALUES:
        lines.append(f"\nATR_EXP={atr_exp:.2f}")
        for r in rows:
            if r["atr_exp"] == atr_exp and r["scope"] == "OOS":
                lines.append(
                    f"  w{r['window']} tr={r['trades']} win%={r['win_pct']:.1f} PF={r['pf']:.2f} net%={r['net_pct']:+.2f}"
                )

    if best:
        lines.append(
            f"\nCandidato (>=4/5 OOS y más frecuencia): ATR_EXP={best['atr_exp']:.2f} "
            f"({best['signals_per_week']:.2f} sig/semana, pass {best['oos_pass']}/5)"
        )
    else:
        lines.append("\nNingún umbral cumple >=4/5 ventanas OOS positivas.")

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {OUT_CSV}", flush=True)
    print(f"Wrote {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
