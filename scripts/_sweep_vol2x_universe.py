"""Sweep vol_2x multi-símbolo (research) — mismas reglas que AAPL/MSFT.

Acciones: 5Min + 15Min, multi-régimen, SMA lenta 50, vol señal ≥2× media 20 velas.
Salidas fijas SL 1.2× ATR + trail 2.75× (sin TP %). Costos 0% + 0.03%/lado.

Cripto BTC/USD: entrada 1H/4H asimétrica + mismo filtro vol_2x en vela de señal.
Salidas cripto asimétricas (SL ATR ceñido, trailing ancho, sin TP % fijo).
Costos 0.25% + 0.03%/lado.

IS acciones: 2020-01-01 → 2025-01-01 | OOS: 2025-01-01 → hoy
IS cripto:  2021-01-01 → 2025-01-01 | OOS: 2025-01-01 → hoy

  .venv/bin/python -u scripts/_sweep_vol2x_universe.py
  .venv/bin/python -u scripts/_sweep_vol2x_universe.py --skip-crypto
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

from _crypto_asymmetric_backtest_lib import (  # noqa: E402
    AsymmetricTrade,
    _simulate_bar_exits,
    _size_qty,
    load_1h,
    summarize_trades as summarize_crypto_trades,
)
from _stocks_asymmetric_backtest_lib import (  # noqa: E402
    CONFIRM_TF,
    ENTRY_TF,
    EntryVariant,
    OOS_START,
    _entry_bar_volume_ok,
    evaluate_variant_symbol,
    load_bars,
    pf_str,
)

from _research_universe import LIQUID_CRYPTO_SYMBOLS, LIQUID_STOCK_SYMBOLS  # noqa: E402

STOCK_IS_START = pd.Timestamp("2020-01-01", tz="UTC")
CRYPTO_IS_START = pd.Timestamp("2021-01-01", tz="UTC")

STOCK_SYMBOLS_DEFAULT = LIQUID_STOCK_SYMBOLS
CRYPTO_SYMBOLS_DEFAULT = LIQUID_CRYPTO_SYMBOLS
VOL2X = EntryVariant(name="vol_2x", volume_mult=2.0, volume_period=20, sma_slow=50)
STOCK_FEE = 0.0
STOCK_SLIP = 0.03
CRYPTO_FEE = 0.25
CRYPTO_SLIP = 0.03
OUT_CSV = PROJECT_ROOT / "logs" / "vol2x_universe_sweep.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "vol2x_universe_sweep_summary.txt"


def _print_metrics(asset: str, scope: str, symbol: str, m: dict[str, float]) -> None:
    print(
        f"  {asset:<7} {scope:<4} {symbol:<8} vol_2x | tr={int(m.get('trades', 0)):>4} | "
        f"win%={m.get('win_rate_pct', 0):.1f} | PF={pf_str(float(m.get('profit_factor', 0)))} | "
        f"net%={m.get('total_return_net_pct', 0):+.2f}",
        flush=True,
    )


def _sma_map_50(symbols: list[str]) -> dict[str, int]:
    return {s.upper(): 50 for s in symbols}


def run_crypto_vol2x_period(
    settings,
    market: MarketDataService,
    symbol: str,
    *,
    bars: pd.DataFrame | None,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
    volume_mult: float = 2.0,
    volume_period: int = 20,
) -> dict[str, float]:
    from bot.strategy.crypto_asymmetric import (
        evaluate_entry_at_bar,
        params_from_settings,
        resample_4h_from_1h,
    )

    params = params_from_settings(settings)
    if bars is None or bars.empty:
        end_dt = period_end.to_pydatetime()
        bars = load_1h(market, symbol, CRYPTO_IS_START.to_pydatetime(), end_dt)
    if bars.empty or len(bars) < 300:
        return summarize_crypto_trades([], float(settings.backtest_cash))

    bars_4h = resample_4h_from_1h(bars)
    cash0 = float(settings.backtest_cash)
    friction = (fee_pct + slippage_pct) / 100.0
    period_start_i = int(bars.index.searchsorted(period_start, side="left"))
    period_end_i = int(bars.index.searchsorted(period_end, side="right")) - 1
    period_end_i = max(period_start_i, min(period_end_i, len(bars) - 1))

    trades: list[AsymmetricTrade] = []
    trades_today: dict[str, int] = {}
    in_pos_until = period_start_i - 1

    for i in range(max(period_start_i, 2), period_end_i + 1):
        if i <= in_pos_until:
            continue
        sig_i = i - 1
        if not _entry_bar_volume_ok(bars, sig_i, volume_period, volume_mult):
            continue
        day_key = str(bars.index[sig_i].date())
        n_day = trades_today.get(day_key, 0)
        sig = evaluate_entry_at_bar(
            bars,
            bars_4h,
            i,
            params,
            has_long=False,
            trades_today=n_day,
        )
        if not sig.allowed:
            continue

        entry = float(bars["open"].iloc[i])
        atr_val = float(sig.atr_value or 0.0)
        qty = _size_qty(settings, cash0, entry, params.sl_atr_mult, atr_val)
        if qty <= 0:
            continue

        exit_px, exit_j, reason = _simulate_bar_exits(
            bars, i, entry, qty, atr_val, params, period_end_i
        )
        notional_in = entry * qty
        notional_out = exit_px * qty
        pnl_gross = (exit_px - entry) * qty
        pnl_net = pnl_gross - (notional_in + notional_out) * friction
        trades.append(
            AsymmetricTrade(
                symbol=symbol,
                entry_idx=i,
                exit_idx=exit_j,
                entry_price=entry,
                exit_price=exit_px,
                qty=qty,
                pnl_gross=pnl_gross,
                pnl_net=pnl_net,
                exit_reason=reason,
            )
        )
        trades_today[day_key] = n_day + 1
        in_pos_until = exit_j

    m = summarize_crypto_trades(trades, cash0)
    m["symbol"] = symbol
    return m


def _append_row(
    rows: list[dict],
    *,
    asset_class: str,
    symbol: str,
    scope: str,
    m: dict[str, float],
) -> None:
    rows.append(
        {
            "asset_class": asset_class,
            "symbol": symbol,
            "scope": scope,
            "variant": "vol_2x",
            "trades": int(m.get("trades", 0)),
            "win_pct": round(float(m.get("win_rate_pct", 0)), 2),
            "pf": round(float(m.get("profit_factor", 0)), 4),
            "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
        }
    )


def _summary_lines(rows: list[dict], stock_symbols: list[str], crypto_symbols: list[str]) -> list[str]:
    lines = [
        "=== vol_2x universe sweep (research) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Acciones: IS 2020-01-01->2025-01-01 | OOS 2025-01-01->hoy | SMA50 | salidas 1.2/2.75",
        "Cripto:   IS 2021-01-01->2025-01-01 | OOS 2025-01-01->hoy | salidas asimétricas 1H/4H",
        "",
        f"{'Symbol':<10} {'Scope':<5} {'tr':>5} {'win%':>6} {'PF':>6} {'net%':>8}",
        "-" * 48,
    ]
    for sym in stock_symbols + crypto_symbols:
        asset = "crypto" if "/" in sym else "stocks"
        for scope in ("IS", "OOS"):
            match = [
                r
                for r in rows
                if r["symbol"] == sym and r["scope"] == scope and r["asset_class"] == asset
            ]
            if not match:
                continue
            r = match[0]
            lines.append(
                f"{sym:<10} {scope:<5} {r['trades']:>5} {r['win_pct']:>6.1f} "
                f"{r['pf']:>6.2f} {r['net_pct']:>+8.2f}"
            )
    lines.extend(["", "=== Candidatos OOS (PF>1 y net%>0) ==="])
    picks: list[str] = []
    for sym in stock_symbols + crypto_symbols:
        asset = "crypto" if "/" in sym else "stocks"
        oos = next(
            (
                r
                for r in rows
                if r["symbol"] == sym and r["scope"] == "OOS" and r["asset_class"] == asset
            ),
            None,
        )
        if not oos or int(oos["trades"]) == 0:
            lines.append(f"  --  {sym:<10} (sin trades OOS)")
            continue
        if float(oos["pf"]) > 1.0 and float(oos["net_pct"]) > 0:
            picks.append(sym)
            lines.append(
                f"  OK  {sym:<10} tr={oos['trades']:>4} PF={oos['pf']:.2f} net%={oos['net_pct']:+.2f}"
            )
        else:
            lines.append(
                f"  --  {sym:<10} tr={oos['trades']:>4} PF={oos['pf']:.2f} net%={oos['net_pct']:+.2f}"
            )
    lines.append("")
    lines.append(f"Pasaron OOS: {', '.join(picks) if picks else '(ninguno)'}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None, help="Fin OOS UTC (default: ahora)")
    parser.add_argument("--stock-symbols", nargs="+", default=list(STOCK_SYMBOLS_DEFAULT))
    parser.add_argument(
        "--crypto-symbols",
        nargs="+",
        default=list(CRYPTO_SYMBOLS_DEFAULT),
        help="Pares cripto (default: BTC/USD). Ignorado si --skip-crypto.",
    )
    parser.add_argument(
        "--skip-crypto",
        action="store_true",
        help="Solo acciones 5Min/15Min (sin BTC ni otros pares cripto).",
    )
    args = parser.parse_args()

    end = (
        pd.Timestamp(args.end, tz="UTC")
        if args.end
        else pd.Timestamp(datetime.now(timezone.utc))
    )
    stock_symbols = [s.upper() for s in args.stock_symbols]
    crypto_symbols = [] if args.skip_crypto else list(args.crypto_symbols)

    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    sma_map = _sma_map_50(stock_symbols)
    is_end_stock = OOS_START - pd.Timedelta(minutes=5)
    is_end_crypto = OOS_START - pd.Timedelta(hours=1)

    print("=" * 88, flush=True)
    print("SWEEP vol_2x UNIVERSO (research — sin cambios en producción)", flush=True)
    print(
        f"Acciones IS {STOCK_IS_START.date()} -> {OOS_START.date()} | "
        f"OOS {OOS_START.date()} -> {end.date()}",
        flush=True,
    )
    if args.skip_crypto:
        print("Cripto: omitido (--skip-crypto)", flush=True)
    else:
        print(
            f"Cripto IS {CRYPTO_IS_START.date()} -> {OOS_START.date()} | "
            f"OOS {OOS_START.date()} -> {end.date()}",
            flush=True,
        )
    print("=" * 88, flush=True)

    rows: list[dict] = []

    print("\n--- ACCIONES (vol_2x, SMA 50) ---", flush=True)
    start_dt = STOCK_IS_START.to_pydatetime()
    end_dt = end.to_pydatetime()
    for symbol in stock_symbols:
        print(f"\n>> {symbol}", flush=True)
        bars = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
        htf = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)
        if bars.empty or len(bars) < 500:
            print(f"  SKIP {symbol}: datos insuficientes (bars={len(bars)})", flush=True)
            continue
        m_is, m_oos = evaluate_variant_symbol(
            settings,
            bars,
            htf,
            symbol,
            sma_map,
            VOL2X,
            start=STOCK_IS_START,
            end=end,
            fee_pct=STOCK_FEE,
            slippage_pct=STOCK_SLIP,
        )
        _print_metrics("stocks", "IS", symbol, m_is)
        _print_metrics("stocks", "OOS", symbol, m_oos)
        _append_row(rows, asset_class="stocks", symbol=symbol, scope="IS", m=m_is)
        _append_row(rows, asset_class="stocks", symbol=symbol, scope="OOS", m=m_oos)

    if not crypto_symbols:
        print("\n--- CRIPTO: omitido ---", flush=True)
    else:
        print("\n--- CRIPTO (vol_2x sobre entrada 1H/4H) ---", flush=True)
    for symbol in crypto_symbols:
        print(f"\n>> {symbol}", flush=True)
        bars_1h = load_1h(market, symbol, CRYPTO_IS_START.to_pydatetime(), end.to_pydatetime())
        m_is = run_crypto_vol2x_period(
            settings,
            market,
            symbol,
            bars=bars_1h,
            period_start=CRYPTO_IS_START,
            period_end=is_end_crypto,
            fee_pct=CRYPTO_FEE,
            slippage_pct=CRYPTO_SLIP,
        )
        m_oos = run_crypto_vol2x_period(
            settings,
            market,
            symbol,
            bars=bars_1h,
            period_start=OOS_START,
            period_end=end,
            fee_pct=CRYPTO_FEE,
            slippage_pct=CRYPTO_SLIP,
        )
        _print_metrics("crypto", "IS", symbol, m_is)
        _print_metrics("crypto", "OOS", symbol, m_oos)
        _append_row(rows, asset_class="crypto", symbol=symbol, scope="IS", m=m_is)
        _append_row(rows, asset_class="crypto", symbol=symbol, scope="OOS", m=m_oos)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "asset_class",
                "symbol",
                "scope",
                "variant",
                "trades",
                "win_pct",
                "pf",
                "net_pct",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    summary = _summary_lines(rows, stock_symbols, crypto_symbols)
    OUT_TXT.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary), flush=True)
    print(f"\nWrote {OUT_CSV} and {OUT_TXT}", flush=True)
    print("Research only — no activar símbolos en live hasta revisión conjunta.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
