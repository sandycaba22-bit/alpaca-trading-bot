"""Walk-forward ETH/USD — variante trend_4h_sma50_atr_exp_1.2 (research only).

Ventanas secuenciales: IS expande desde 2021-01-01; cada OOS ~9 meses hasta hoy.
Misma entrada/salida/costos que _backtest_crypto_regime_entry.py.

  .venv\\Scripts\\python.exe -u scripts\\_walkforward_crypto_regime_eth.py

Salida: logs/crypto_regime_eth_walkforward.csv, logs/crypto_regime_eth_walkforward_summary.txt
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
    GATE_FULL_4H_ATR12,
    metrics_for_period,
)
from _stocks_asymmetric_backtest_lib import pf_str

SYMBOL = "ETH/USD"
VARIANT = "trend_4h_sma50_atr_exp_1.2"
GATE = GATE_FULL_4H_ATR12
CRYPTO_FEE = 0.25
CRYPTO_SLIP = 0.03

OUT_CSV = PROJECT_ROOT / "logs" / "crypto_regime_eth_walkforward.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "crypto_regime_eth_walkforward_summary.txt"

# OOS inicio (UTC inclusive) por ventana; IS = [CRYPTO_IS_START, oos_start - 1h)
WF_OOS_STARTS = (
    "2022-07-01",
    "2023-04-01",
    "2024-01-01",
    "2024-10-01",
    "2025-07-01",
)


def _walkforward_windows(end: pd.Timestamp) -> list[tuple[int, pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """(id, is_start, is_end, oos_start, oos_end) por ventana."""
    windows: list[tuple[int, pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]] = []
    oos_starts = [pd.Timestamp(s, tz="UTC") for s in WF_OOS_STARTS]
    for i, oos_start in enumerate(oos_starts):
        if oos_start >= end:
            break
        oos_end = oos_starts[i + 1] - pd.Timedelta(hours=1) if i + 1 < len(oos_starts) else end
        if oos_end <= oos_start:
            continue
        is_end = oos_start - pd.Timedelta(hours=1)
        if is_end <= CRYPTO_IS_START:
            continue
        windows.append((i + 1, CRYPTO_IS_START, is_end, oos_start, oos_end))
    return windows


def _fmt_ts(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d")


def main() -> int:
    end = pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)

    windows = _walkforward_windows(end)
    if not windows:
        print("No hay ventanas walk-forward con el rango configurado.", flush=True)
        return 1

    print("=" * 96, flush=True)
    print(f"WALK-FORWARD {SYMBOL} | {VARIANT}", flush=True)
    print(f"Histórico desde {CRYPTO_IS_START.date()} hasta {end.date()} | costos {CRYPTO_FEE}% + {CRYPTO_SLIP}%/lado", flush=True)
    print("=" * 96, flush=True)

    bars = load_1h(market, SYMBOL, CRYPTO_IS_START.to_pydatetime(), end.to_pydatetime())
    if bars.empty or len(bars) < 500:
        print(f"SKIP: datos insuficientes ({len(bars)} barras)", flush=True)
        return 1
    print(f"cache {SYMBOL} 1Hour bars={len(bars)}", flush=True)

    rows: list[dict] = []
    oos_pass = 0
    oos_windows = 0

    for wid, is_start, is_end, oos_start, oos_end in windows:
        print(f"\n--- Ventana {wid} | IS {_fmt_ts(is_start)} -> {_fmt_ts(is_end)} | OOS {_fmt_ts(oos_start)} -> {_fmt_ts(oos_end)} ---", flush=True)
        m_is = metrics_for_period(
            settings,
            bars,
            SYMBOL,
            GATE,
            scope_start=is_start,
            scope_end=is_end,
            fee_pct=CRYPTO_FEE,
            slippage_pct=CRYPTO_SLIP,
        )
        m_oos = metrics_for_period(
            settings,
            bars,
            SYMBOL,
            GATE,
            scope_start=oos_start,
            scope_end=oos_end,
            fee_pct=CRYPTO_FEE,
            slippage_pct=CRYPTO_SLIP,
        )
        for scope, m, p_start, p_end in (
            ("IS", m_is, is_start, is_end),
            ("OOS", m_oos, oos_start, oos_end),
        ):
            tr = int(m.get("trades", 0))
            win = float(m.get("win_rate_pct", 0))
            pf = float(m.get("profit_factor", 0))
            net = float(m.get("total_return_net_pct", 0))
            print(
                f"  {scope:<4} {_fmt_ts(p_start)} -> {_fmt_ts(p_end)} | tr={tr:>4} | win%={win:.1f} | PF={pf_str(pf)} | net%={net:+.2f}",
                flush=True,
            )
            rows.append(
                {
                    "window": wid,
                    "scope": scope,
                    "period_start": _fmt_ts(p_start),
                    "period_end": _fmt_ts(p_end),
                    "trades": tr,
                    "win_pct": round(win, 2),
                    "pf": round(pf, 4),
                    "net_pct": round(net, 4),
                }
            )
        oos_windows += 1
        tr_o = int(m_oos.get("trades", 0))
        pf_o = float(m_oos.get("profit_factor", 0))
        net_o = float(m_oos.get("total_return_net_pct", 0))
        if tr_o > 0 and pf_o > 1.0 and net_o > 0:
            oos_pass += 1

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["window", "scope", "period_start", "period_end", "trades", "win_pct", "pf", "net_pct"],
        )
        w.writeheader()
        w.writerows(rows)

    lines = [
        f"=== Walk-forward {SYMBOL} | {VARIANT} ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        f"Ventanas OOS: {oos_windows} | Criterio OOS (tr>0, PF>1, net%>0): {oos_pass}/{oos_windows}",
        "",
        f"{'Win':>3} {'Scope':<5} {'Desde':<12} {'Hasta':<12} {'tr':>5} {'win%':>6} {'PF':>6} {'net%':>8}",
        "-" * 62,
    ]
    for wid, _, _, _, _ in windows:
        for scope in ("IS", "OOS"):
            match = [r for r in rows if r["window"] == wid and r["scope"] == scope]
            if not match:
                continue
            r = match[0]
            lines.append(
                f"{r['window']:>3} {scope:<5} {r['period_start']:<12} {r['period_end']:<12} "
                f"{r['trades']:>5} {r['win_pct']:>6.1f} {r['pf']:>6.2f} {r['net_pct']:>+8.2f}"
            )

    if oos_pass == 0:
        verdict = "INCONSISTENTE: ninguna ventana OOS cumple PF>1 y net positivo — descartar para producción."
    elif oos_pass == oos_windows:
        verdict = "CONSISTENTE: todas las ventanas OOS pasan el criterio (revisar tamaño muestral por ventana)."
    elif oos_pass >= (oos_windows + 1) // 2:
        verdict = f"MIXTO: mayoría OOS positiva ({oos_pass}/{oos_windows}) — no conclusivo; ampliar muestra o paper."
    else:
        verdict = f"DÉBIL: minoría OOS positiva ({oos_pass}/{oos_windows}) — probable suerte del split único."

    lines.extend(["", "=== Veredicto ===", verdict])

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{verdict}", flush=True)
    print(f"Wrote {OUT_CSV}", flush=True)
    print(f"Wrote {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
