"""Research aislado: chop/rango 4H vs entradas ETH régimen (NO producción).

1) ADX 4H + rango/ATR 4H en entradas ganadoras vs perdedoras (w3/w4 vs w1/w2/w5).
2) Si hay separación, sweep ADX mínimo y walk-forward 5 ventanas vs baseline.

  .venv\\Scripts\\python.exe -u scripts\\_research_eth_regime_chop_4h.py
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
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
from bot.strategy.crypto_asymmetric import (
    _htf_until,
    evaluate_entry_at_bar,
    params_from_settings,
    resample_4h_from_1h,
)
from bot.strategy.crypto_regime_entry import (
    atr_expansion_at_series,
    htf_trend_at,
    resample_1d_from_1h,
)
from bot.strategy.indicators import adx as adx_series
from bot.strategy.indicators import atr as atr_series

from _crypto_asymmetric_backtest_lib import (
    AsymmetricTrade,
    _simulate_bar_exits,
    _size_qty,
    load_1h,
    summarize_trades,
)
from _crypto_regime_entry_backtest_lib import (
    CRYPTO_IS_START,
    gate_sma50_atr_exp,
    walkforward_windows,
)
from _stocks_asymmetric_backtest_lib import pf_str

SYMBOL = "ETH/USD"
BASELINE_ATR = 1.20
GATE_BASE = gate_sma50_atr_exp(BASELINE_ATR)
FEE = 0.25
SLIP = 0.03
RANGE_LOOKBACK_4H = 14
ADX_PERIOD = 14

OUT_DIR = PROJECT_ROOT / "logs"
OUT_TRADES = OUT_DIR / "eth_regime_chop_trade_features.csv"
OUT_SWEEP = OUT_DIR / "eth_regime_chop_adx_walkforward.csv"
OUT_TXT = OUT_DIR / "eth_regime_chop_research_summary.txt"

WF_OOS_PASS_BASELINE = {1: True, 2: True, 3: False, 4: False, 5: True}
ADX_CANDIDATES = (0.0, 15.0, 18.0, 20.0, 22.0, 25.0)  # 0 = baseline sin filtro ADX


@dataclass(frozen=True)
class ChopMetrics:
    adx_4h: float
    range_atr_4h: float


def chop_metrics_4h_at(bars_4h: pd.DataFrame, ts: pd.Timestamp) -> ChopMetrics | None:
    htf = _htf_until(bars_4h, ts)
    need = max(RANGE_LOOKBACK_4H + 2, ADX_PERIOD + 5)
    if htf is None or len(htf) < need:
        return None
    adx_s = adx_series(htf, ADX_PERIOD).dropna()
    atr_s = atr_series(htf, ADX_PERIOD).dropna()
    if adx_s.empty or atr_s.empty:
        return None
    adx_v = float(adx_s.iloc[-1])
    window = htf.iloc[-RANGE_LOOKBACK_4H:]
    rng = float(window["high"].max() - window["low"].min())
    atr_v = float(atr_s.iloc[-1])
    if atr_v <= 0 or not np.isfinite(adx_v):
        return None
    return ChopMetrics(adx_4h=adx_v, range_atr_4h=rng / atr_v)


def run_regime_period_with_adx_min(
    settings,
    bars: pd.DataFrame,
    gate,
    *,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    min_adx_4h: float,
) -> list[AsymmetricTrade]:
    """Copia lógica backtest lib + filtro research min ADX 4H."""
    params = params_from_settings(settings)
    bars_4h = resample_4h_from_1h(bars)
    bars_1d = resample_1d_from_1h(bars)
    friction = (FEE + SLIP) / 100.0
    period_start_i = int(bars.index.searchsorted(period_start, side="left"))
    period_end_i = int(bars.index.searchsorted(period_end, side="right")) - 1
    period_end_i = max(period_start_i, min(period_end_i, len(bars) - 1))
    atr_1h = atr_series(bars, gate.atr_period)

    trades: list[AsymmetricTrade] = []
    trades_today: dict[str, int] = {}
    in_pos_until = period_start_i - 1

    for i in range(max(period_start_i, 2), period_end_i + 1):
        if i <= in_pos_until:
            continue
        sig_i = i - 1
        ts = bars.index[sig_i]
        ok_t, _ = htf_trend_at(bars_4h, bars_1d, ts, gate)
        if not ok_t:
            continue
        ok_a, _ = atr_expansion_at_series(atr_1h, i, gate)
        if not ok_a:
            continue
        if min_adx_4h > 0:
            cm = chop_metrics_4h_at(bars_4h, ts)
            if cm is None or cm.adx_4h < min_adx_4h:
                continue
        day_key = str(ts.date())
        n_day = trades_today.get(day_key, 0)
        sig = evaluate_entry_at_bar(
            bars, bars_4h, i, params, has_long=False, trades_today=n_day
        )
        if not sig.allowed:
            continue
        entry = float(bars["open"].iloc[i])
        atr_val = float(sig.atr_value or 0.0)
        qty = _size_qty(settings, float(settings.backtest_cash), entry, params.sl_atr_mult, atr_val)
        if qty <= 0:
            continue
        exit_px, exit_j, reason = _simulate_bar_exits(
            bars, i, entry, qty, atr_val, params, period_end_i
        )
        pnl_net = (exit_px - entry) * qty - (entry * qty + exit_px * qty) * friction
        trades.append(
            AsymmetricTrade(
                symbol=SYMBOL,
                entry_idx=i,
                exit_idx=exit_j,
                entry_price=entry,
                exit_price=exit_px,
                qty=qty,
                pnl_gross=(exit_px - entry) * qty,
                pnl_net=pnl_net,
                exit_reason=reason,
            )
        )
        trades_today[day_key] = n_day + 1
        in_pos_until = exit_j
    return trades


def _oos_ok(m: dict[str, float]) -> bool:
    return int(m.get("trades", 0)) > 0 and float(m.get("profit_factor", 0)) > 1.0 and float(
        m.get("total_return_net_pct", 0)
    ) > 0


def _group_label(window: int) -> str:
    return "FAIL_w34" if window in (3, 4) else "PASS_w125"


def main() -> int:
    end = pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    market = MarketDataService(AlpacaClient(settings))
    windows = walkforward_windows(end)
    bars = load_1h(market, SYMBOL, CRYPTO_IS_START.to_pydatetime(), end.to_pydatetime())
    bars_4h = resample_4h_from_1h(bars)

    trade_rows: list[dict] = []
    for wid, _a, _b, oos_start, oos_end in windows:
        trs = run_regime_period_with_adx_min(
            settings,
            bars,
            GATE_BASE,
            period_start=oos_start,
            period_end=oos_end,
            min_adx_4h=0.0,
        )
        for t in trs:
            ts = bars.index[t.entry_idx - 1]
            cm = chop_metrics_4h_at(bars_4h, ts)
            if cm is None:
                continue
            win = t.pnl_net > 0
            trade_rows.append(
                {
                    "window": wid,
                    "group": _group_label(wid),
                    "entry_ts": str(ts),
                    "win": int(win),
                    "pnl_net": round(t.pnl_net, 2),
                    "adx_4h": round(cm.adx_4h, 2),
                    "range_atr_4h": round(cm.range_atr_4h, 2),
                }
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_TRADES.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(trade_rows[0].keys()) if trade_rows else [])
        if trade_rows:
            w.writeheader()
            w.writerows(trade_rows)

    lines = [
        "=== Research chop 4H ETH (aislado, sin produccion) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        f"Baseline entrada: trend_4h_sma50_atr_exp_{BASELINE_ATR:.2f}",
        "",
        "=== Fase 1: ADX 4H y rango/ATR en entradas (solo OOS walk-forward) ===",
    ]

    if not trade_rows:
        lines.append("(sin trades)")
    else:
        df = pd.DataFrame(trade_rows)

        def _stats(sub: pd.DataFrame, col: str) -> str:
            if sub.empty:
                return "n=0"
            return f"n={len(sub)} mean={sub[col].mean():.1f} med={sub[col].median():.1f}"

        for outcome, label in ((1, "ganadoras"), (0, "perdedoras")):
            sub = df[df["win"] == outcome]
            lines.append(f"  {label}: ADX {_stats(sub, 'adx_4h')} | range/ATR {_stats(sub, 'range_atr_4h')}")

        lines.append("")
        for grp in ("FAIL_w34", "PASS_w125"):
            g = df[df["group"] == grp]
            lines.append(f"  [{grp}]")
            for outcome, label in ((1, "W"), (0, "L")):
                sub = g[g["win"] == outcome]
                lines.append(
                    f"    {label}: ADX {_stats(sub, 'adx_4h')} | range/ATR {_stats(sub, 'range_atr_4h')}"
                )

        w34_l = df[(df["group"] == "FAIL_w34") & (df["win"] == 0)]["adx_4h"]
        w125_w = df[(df["group"] == "PASS_w125") & (df["win"] == 1)]["adx_4h"]
        sep_adx = float(w34_l.mean() - w125_w.mean()) if len(w34_l) and len(w125_w) else 0.0
        lines.extend(
            [
                "",
                f"  Delta ADX (media L en w34 - media W en w125): {sep_adx:+.1f}",
                "  (negativo sugeriria perdedoras w34 con ADX mas bajo / mas chop)",
            ]
        )

        # Separación simple ganadoras vs perdedoras (todas ventanas)
        w_all = df[df["win"] == 1]["adx_4h"]
        l_all = df[df["win"] == 0]["adx_4h"]
        lines.append(
            f"  Global: ADX mean W={w_all.mean():.1f} L={l_all.mean():.1f} delta={w_all.mean()-l_all.mean():+.1f}"
        )

    clear_sep = False
    if trade_rows:
        df = pd.DataFrame(trade_rows)
        w_adx = df[df["win"] == 1]["adx_4h"].mean()
        l_adx = df[df["win"] == 0]["adx_4h"].mean()
        clear_sep = (w_adx - l_adx) >= 2.0

    lines.extend(["", "=== Fase 2: Walk-forward OOS por min ADX 4H (si aplica) ==="])
    sweep_rows: list[dict] = []
    for min_adx in ADX_CANDIDATES:
        tag = f"baseline" if min_adx <= 0 else f"adx4h_min_{min_adx:.0f}"
        pass_n = 0
        pass_fail = 0
        pass_pass = 0
        lines.append(f"\n--- {tag} ---")
        for wid, _a, _b, oos_start, oos_end in windows:
            trs = run_regime_period_with_adx_min(
                settings,
                bars,
                GATE_BASE,
                period_start=oos_start,
                period_end=oos_end,
                min_adx_4h=min_adx,
            )
            m = summarize_trades(trs, float(settings.backtest_cash))
            ok = _oos_ok(m)
            if ok:
                pass_n += 1
            if wid in (3, 4) and ok:
                pass_fail += 1
            if wid in (1, 2, 5) and ok:
                pass_pass += 1
            tr = int(m.get("trades", 0))
            pf = float(m.get("profit_factor", 0))
            net = float(m.get("total_return_net_pct", 0))
            win = float(m.get("win_rate_pct", 0))
            lines.append(
                f"  w{wid} OOS tr={tr} win%={win:.1f} PF={pf_str(pf)} net%={net:+.2f} {'OK' if ok else '--'}"
            )
            sweep_rows.append(
                {
                    "variant": tag,
                    "min_adx_4h": min_adx,
                    "window": wid,
                    "trades": tr,
                    "win_pct": round(win, 2),
                    "pf": round(pf, 4),
                    "net_pct": round(net, 4),
                    "oos_ok": int(ok),
                }
            )
        lines.append(
            f"  >> OOS pass total {pass_n}/5 | w3/w4 OK {pass_fail}/2 | w1/w2/w5 OK {pass_pass}/3"
        )

    with OUT_SWEEP.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "variant",
                "min_adx_4h",
                "window",
                "trades",
                "win_pct",
                "pf",
                "net_pct",
                "oos_ok",
            ],
        )
        w.writeheader()
        w.writerows(sweep_rows)

    lines.extend(["", "=== Criterio exito (research) ==="])
    best = None
    for min_adx in ADX_CANDIDATES:
        if min_adx <= 0:
            continue
        sub = [r for r in sweep_rows if r["min_adx_4h"] == min_adx]
        w34 = sum(r["oos_ok"] for r in sub if r["window"] in (3, 4))
        w125 = sum(r["oos_ok"] for r in sub if r["window"] in (1, 2, 5))
        base125 = sum(
            r["oos_ok"]
            for r in sweep_rows
            if r["min_adx_4h"] == 0.0 and r["window"] in (1, 2, 5)
        )
        if w34 >= 1 and w125 >= base125:
            best = (min_adx, w34, w125)
    if not clear_sep and best is None:
        lines.append(
            "Separacion ADX ganadoras/perdedoras DEBIL y ningun min ADX mejora w3/w4 "
            "sin perder w1/w2/w5 vs baseline -> techo estructural 2024 plausible; "
            "seguir paper baseline sin cambiar gates."
        )
    elif best:
        lines.append(
            f"Candidato research: min ADX 4H >= {best[0]:.0f} "
            f"(w3/w4 OK {best[1]}/2, w1/w2/w5 OK {best[2]}/3 vs baseline {base125}/3). "
            "Validar en paper separado antes de produccion."
        )
    else:
        lines.append(
            "Separacion parcial pero sweep ADX no cumple criterio (arreglar w34 sin romper w125). "
            "Mantener paper baseline."
        )

    text = "\n".join(lines) + "\n"
    OUT_TXT.write_text(text, encoding="utf-8")
    print(text, flush=True)
    print(f"Wrote {OUT_TRADES}", flush=True)
    print(f"Wrote {OUT_SWEEP}", flush=True)
    print(f"Wrote {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
