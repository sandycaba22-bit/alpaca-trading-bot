"""Diagnóstico ETH régimen (research) — causas de ventanas OOS débiles.

Clasifica barras 1H y trades por: HTF lateral/bajista, ATR bajo mínimo, ATR extremo, gaps.

  .venv\\Scripts\\python.exe -u scripts\\_diagnose_crypto_regime_eth.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings
from bot.strategy.crypto_asymmetric import resample_4h_from_1h
from bot.strategy.crypto_regime_entry import resample_1d_from_1h
from bot.strategy.indicators import atr as atr_series
from bot.strategy.multi_tf_analysis import analyze_trend

from _crypto_asymmetric_backtest_lib import load_1h
from _crypto_regime_entry_backtest_lib import (
    CRYPTO_IS_START,
    gate_sma50_atr_exp,
    htf_trend_at,
    run_crypto_regime_period,
    walkforward_windows,
)

SYMBOL = "ETH/USD"
GATE = gate_sma50_atr_exp(1.20)
ATR_MIN = 1.20
ATR_EXTREME_MULT = 2.0  # candidato techo research
GAP_PCT = 0.035
OUT = PROJECT_ROOT / "logs" / "eth_regime_diagnostic_summary.txt"

# OOS pass conocido del walk-forward 1.20 (PF>1, net>0)
WF_OOS_PASS = {1: True, 2: True, 3: False, 4: False, 5: True}


def _atr_ratio(atr_s: pd.Series, i: int, period: int = 20) -> float | None:
    sig = i - 1
    if sig < period + 1 or sig >= len(atr_s):
        return None
    cur = float(atr_s.iloc[sig])
    avg = float(atr_s.iloc[sig - period : sig].mean())
    if avg <= 0:
        return None
    return cur / avg


def _bar_diagnostics(bars: pd.DataFrame, bars_4h: pd.DataFrame, bars_1d: pd.DataFrame) -> pd.DataFrame:
    atr_s = atr_series(bars, GATE.atr_period)
    rows: list[dict] = []
    for i in range(50, len(bars)):
        ts = bars.index[i - 1]
        ok_htf, htf_msg = htf_trend_at(bars_4h, bars_1d, ts, GATE)
        trend_4h, _ = analyze_trend(
            bars_4h.loc[bars_4h.index <= ts].tail(120),
            fast=GATE.htf_fast,
            slow=GATE.htf_slow,
        )
        ratio = _atr_ratio(atr_s, i, GATE.atr_expansion_period)
        prev_close = float(bars["close"].iloc[i - 2]) if i >= 2 else float("nan")
        close = float(bars["close"].iloc[i - 1])
        gap_pct = abs(close - prev_close) / prev_close if prev_close > 0 else 0.0
        atr_val = float(atr_s.iloc[i - 1]) if pd.notna(atr_s.iloc[i - 1]) else 0.0
        gap_atr = gap_pct * close / atr_val if atr_val > 0 else 0.0
        rows.append(
            {
                "ts": ts,
                "htf_bull": ok_htf,
                "trend_4h": trend_4h,
                "atr_ratio": ratio,
                "gap_pct": gap_pct,
                "gap_atr": gap_atr,
                "big_gap": gap_pct >= GAP_PCT,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    end = pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    windows = walkforward_windows(end)

    bars = load_1h(market, SYMBOL, CRYPTO_IS_START.to_pydatetime(), end.to_pydatetime())
    bars_4h = resample_4h_from_1h(bars)
    bars_1d = resample_1d_from_1h(bars)
    diag = _bar_diagnostics(bars, bars_4h, bars_1d)

    lines = [
        f"=== Diagnóstico {SYMBOL} | {GATE.name} | ATR min={ATR_MIN}x | techo research={ATR_EXTREME_MULT}x ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]

    # Histórico ratio ATR (referencia percentiles en todo el sample)
    ratios = diag["atr_ratio"].dropna()
    if not ratios.empty:
        p50, p75, p90, p95 = np.percentile(ratios, [50, 75, 90, 95])
        lines.extend(
            [
                "=== Distribución ATR ratio (1H, histórico completo) ===",
                f"  p50={p50:.2f} p75={p75:.2f} p90={p90:.2f} p95={p95:.2f} max={ratios.max():.2f}",
                f"  barras ratio>={ATR_EXTREME_MULT}x: {(ratios >= ATR_EXTREME_MULT).mean()*100:.1f}%",
                "",
            ]
        )

    for wid, _is0, _is1, oos_start, oos_end in windows:
        mask = (diag["ts"] >= oos_start) & (diag["ts"] <= oos_end)
        w = diag.loc[mask]
        n = len(w)
        if n == 0:
            continue
        pass_lbl = "PASS" if WF_OOS_PASS.get(wid) else "FAIL"
        lines.append(f"--- Ventana OOS {wid} ({oos_start.date()} -> {oos_end.date()}) [{pass_lbl}] ---")
        pct_not_bull = (~w["htf_bull"]).mean() * 100
        pct_sideways = (w["trend_4h"] == "sideways").mean() * 100
        pct_bear = (w["trend_4h"] == "bear").mean() * 100
        r = w["atr_ratio"].dropna()
        pct_below_min = (r < ATR_MIN).mean() * 100 if len(r) else 0
        pct_in_band = ((r >= ATR_MIN) & (r < ATR_EXTREME_MULT)).mean() * 100 if len(r) else 0
        pct_extreme = (r >= ATR_EXTREME_MULT).mean() * 100 if len(r) else 0
        pct_big_gap = w["big_gap"].mean() * 100
        lines.extend(
            [
                f"  barras 1H: {n}",
                f"  HTF no alcista (gate SMA50): {pct_not_bull:.1f}% | 4H sideways: {pct_sideways:.1f}% | 4H bear: {pct_bear:.1f}%",
                f"  ATR ratio <{ATR_MIN}x (bloqueo actual): {pct_below_min:.1f}%",
                f"  ATR ratio [{ATR_MIN}x, {ATR_EXTREME_MULT}x): {pct_in_band:.1f}%",
                f"  ATR ratio >={ATR_EXTREME_MULT}x (candidato techo): {pct_extreme:.1f}%",
                f"  gaps hora |ret|>={GAP_PCT*100:.1f}%: {pct_big_gap:.1f}% ({int(w['big_gap'].sum())} barras)",
            ]
        )
        if len(r):
            lines.append(
                f"  ratio OOS: mean={r.mean():.2f} p90={float(np.percentile(r, 90)):.2f} max={r.max():.2f}"
            )

        trades = run_crypto_regime_period(
            settings,
            bars,
            SYMBOL,
            GATE,
            period_start=oos_start,
            period_end=oos_end,
            fee_pct=0.25,
            slippage_pct=0.03,
        )
        if trades:
            wins = sum(1 for t in trades if t.pnl_net > 0)
            losses = [t for t in trades if t.pnl_net <= 0]
            lines.append(f"  trades: {len(trades)} | win={wins} | net sum={sum(t.pnl_net for t in trades):.2f}")
            for t in trades:
                ei = t.entry_idx
                ts_e = bars.index[ei - 1] if ei > 0 else bars.index[min(ei, len(bars) - 1)]
                ratio_e = _atr_ratio(atr_series(bars, GATE.atr_period), ei, GATE.atr_expansion_period)
                ok_e, _ = htf_trend_at(bars_4h, bars_1d, ts_e, GATE)
                tag = "W" if t.pnl_net > 0 else "L"
                re_s = f"{ratio_e:.2f}" if ratio_e is not None else "n/a"
                lines.append(
                    f"    {tag} entry={ts_e} ratio={re_s} htf_bull={ok_e} "
                    f"pnl_net={t.pnl_net:+.2f} exit={t.exit_reason}"
                )
            if losses:
                avg_l_ratio = np.mean(
                    [
                        _atr_ratio(atr_series(bars, GATE.atr_period), t.entry_idx, GATE.atr_expansion_period)
                        or 0
                        for t in losses
                    ]
                )
                lines.append(f"  ATR ratio medio entradas perdedoras: {avg_l_ratio:.2f}")
        else:
            lines.append("  trades: 0")
        lines.append("")

    # Hipótesis automática (solo texto, sin cambiar código)
    lines.append("=== Lectura (sin implementar fixes) ===")
    fail_ws = [wid for wid, ok in WF_OOS_PASS.items() if not ok]
    lines.append(f"Ventanas OOS FAIL walk-forward 1.20: {fail_ws} (2024 y mid-2025)")
    lines.append(
        "Comparar % HTF no alcista y % ATR extremo entre FAIL vs PASS; "
        "si FAIL tiene mas sideways/chop - filtro tendencia ya parcial; "
        "si perdedoras concentran ratio ATR muy alto - candidato ATR maximo; "
        "si pocos gaps grandes → outlier de noticia poco probable como causa principal."
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text, flush=True)
    print(f"Wrote {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
