"""Backtest entrada cripto régimen (HTF trend + ATR expansion) — research only.

Salidas: asimétrico 1H/4H (SL ATR ceñido + trail ancho, sin TP %).
Costos: 0.25% + 0.03%/lado | IS 2021-01-01->2025-01-01 | OOS 2025-01-01->hoy

  .venv/bin/python -u scripts/_backtest_crypto_regime_entry.py

Salida: logs/crypto_regime_entry_backtest.csv, logs/crypto_regime_entry_backtest_summary.txt
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

from _crypto_asymmetric_backtest_lib import load_1h
from _crypto_regime_entry_backtest_lib import (
    CRYPTO_IS_START,
    GATE_BASELINE,
    GATE_FULL_4H_ATR12,
    GATE_TREND_1D_ATR12,
    GATE_TREND_4H_ATR12,
    GATE_TREND_4H_ATR15,
    GATE_VOL2X_ONLY,
    CryptoRegimeGate,
    metrics_for_period,
)
from _stocks_asymmetric_backtest_lib import OOS_START, pf_str

CRYPTO_SYMBOLS = ("BTC/USD", "ETH/USD")
CRYPTO_FEE = 0.25
CRYPTO_SLIP = 0.03
OUT_CSV = PROJECT_ROOT / "logs" / "crypto_regime_entry_backtest.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "crypto_regime_entry_backtest_summary.txt"


def _variants() -> list[tuple[str, CryptoRegimeGate, bool]]:
    return [
        ("baseline_asim", GATE_BASELINE, False),
        ("vol2x_asim", GATE_VOL2X_ONLY, True),
        ("trend_4h_atr_exp_1.2", GATE_TREND_4H_ATR12, False),
        ("trend_1d_atr_exp_1.2", GATE_TREND_1D_ATR12, False),
        ("trend_4h_atr_exp_1.3", GATE_TREND_4H_ATR15, False),
        ("trend_4h_sma50_atr_exp_1.2", GATE_FULL_4H_ATR12, False),
    ]


def _print_row(scope: str, symbol: str, variant: str, m: dict[str, float]) -> None:
    print(
        f"  {scope:<4} {symbol:<8} {variant:<28} | tr={int(m.get('trades', 0)):>4} | "
        f"win%={m.get('win_rate_pct', 0):.1f} | PF={pf_str(float(m.get('profit_factor', 0)))} | "
        f"net%={m.get('total_return_net_pct', 0):+.2f}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    end = (
        pd.Timestamp(args.end, tz="UTC")
        if args.end
        else pd.Timestamp(datetime.now(timezone.utc))
    )
    is_end = OOS_START - pd.Timedelta(hours=1)

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)

    print("=" * 96, flush=True)
    print("BACKTEST CRIPTO REGIMEN ENTRADA (research — sin produccion)", flush=True)
    print(
        f"IS {CRYPTO_IS_START.date()} -> {OOS_START.date()} | OOS {OOS_START.date()} -> {end.date()}",
        flush=True,
    )
    print(f"Costos {CRYPTO_FEE}% + {CRYPTO_SLIP}%/lado | salidas asimetricas 1H/4H", flush=True)
    print("=" * 96, flush=True)

    rows: list[dict] = []
    for symbol in CRYPTO_SYMBOLS:
        print(f"\n>> {symbol}", flush=True)
        bars = load_1h(market, symbol, CRYPTO_IS_START.to_pydatetime(), end.to_pydatetime())
        if bars.empty or len(bars) < 500:
            print(f"  SKIP {symbol}: datos insuficientes", flush=True)
            continue
        for variant_name, gate, vol2x in _variants():
            print(f"  variant={variant_name} ...", flush=True)
            m_is = metrics_for_period(
                settings,
                bars,
                symbol,
                gate,
                scope_start=CRYPTO_IS_START,
                scope_end=is_end,
                fee_pct=CRYPTO_FEE,
                slippage_pct=CRYPTO_SLIP,
                vol2x=vol2x,
            )
            m_oos = metrics_for_period(
                settings,
                bars,
                symbol,
                gate,
                scope_start=OOS_START,
                scope_end=end,
                fee_pct=CRYPTO_FEE,
                slippage_pct=CRYPTO_SLIP,
                vol2x=vol2x,
            )
            _print_row("IS", symbol, variant_name, m_is)
            _print_row("OOS", symbol, variant_name, m_oos)
            for scope, m in (("IS", m_is), ("OOS", m_oos)):
                rows.append(
                    {
                        "symbol": symbol,
                        "scope": scope,
                        "variant": variant_name,
                        "trades": int(m.get("trades", 0)),
                        "win_pct": round(float(m.get("win_rate_pct", 0)), 2),
                        "pf": round(float(m.get("profit_factor", 0)), 4),
                        "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
                    }
                )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["symbol", "scope", "variant", "trades", "win_pct", "pf", "net_pct"],
        )
        w.writeheader()
        w.writerows(rows)

    lines = [
        "=== Crypto regime entry backtest (research) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"{'Symbol':<10} {'Scope':<5} {'Variant':<28} {'tr':>5} {'win%':>6} {'PF':>6} {'net%':>8}",
        "-" * 72,
    ]
    for symbol in CRYPTO_SYMBOLS:
        for scope in ("IS", "OOS"):
            for variant_name, _, _ in _variants():
                match = [
                    r
                    for r in rows
                    if r["symbol"] == symbol and r["scope"] == scope and r["variant"] == variant_name
                ]
                if not match:
                    continue
                r = match[0]
                lines.append(
                    f"{symbol:<10} {scope:<5} {variant_name:<28} {r['trades']:>5} "
                    f"{r['win_pct']:>6.1f} {r['pf']:>6.2f} {r['net_pct']:>+8.2f}"
                )
    lines.extend(["", "=== OOS candidatos (PF>1 y net%>0) ==="])
    picks = [
        f"{r['symbol']}:{r['variant']}"
        for r in rows
        if r["scope"] == "OOS" and r["trades"] > 0 and r["pf"] > 1.0 and r["net_pct"] > 0
    ]
    lines.append(", ".join(picks) if picks else "(ninguno)")

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {OUT_CSV}", flush=True)
    print(f"Wrote {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
