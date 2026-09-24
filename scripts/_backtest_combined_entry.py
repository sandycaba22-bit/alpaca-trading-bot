"""Backtest entrada combinada (research) — NO activar producción sin OK explícito.

Combina: vol_2x + ADX + pullback EMA + vol pullback + skip apertura (acciones).
Cripto: mismos filtros sobre 1H/4H; salidas asimétricas sin cambios.

Compara vs baseline sweep vol_2x (multi-régimen legacy, solo referencia acciones).

  .venv/bin/python -u scripts/_backtest_combined_entry.py

Salida: logs/combined_entry_backtest.csv, logs/combined_entry_backtest_summary.txt
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings

from _combined_entry_backtest_lib import (  # noqa: E402
    CONFIRM_TF,
    ENTRY_TF,
    OOS_START,
    PROFILE_COMBINED_FULL,
    PROFILE_VOL2X_PULLBACK,
    CombinedEntryProfile,
    metrics_legacy_vol2x,
    metrics_stocks_scope,
    pf_str,
    precompute_stocks_combined,
    run_crypto_combined_period,
)
from _crypto_asymmetric_backtest_lib import (  # noqa: E402
    HISTORY_START as CRYPTO_HISTORY_START,
    load_1h,
    run_backtest,
    summarize_trades as summarize_crypto_trades,
)
from _stocks_asymmetric_backtest_lib import (  # noqa: E402
    _sma_slow_map,
    load_bars,
)

STOCK_SYMBOLS = ("AAPL", "MSFT")
CRYPTO_SYMBOLS = ("BTC/USD", "ETH/USD")
STOCK_FEE = 0.0
STOCK_SLIP = 0.03
CRYPTO_FEE = 0.25
CRYPTO_SLIP = 0.03
YEARS = 6

OUT_CSV = PROJECT_ROOT / "logs" / "combined_entry_backtest.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "combined_entry_backtest_summary.txt"


def _print_row(asset: str, scope: str, symbol: str, variant: str, m: dict[str, float]) -> None:
    print(
        f"  {asset:<7} {scope:<4} {symbol:<8} {variant:<22} | tr={int(m.get('trades', 0)):>4} | "
        f"win%={m.get('win_rate_pct', 0):.1f} | PF={pf_str(float(m.get('profit_factor', 0)))} | "
        f"net%={m.get('total_return_net_pct', 0):+.2f}"
    )


def _run_stocks(
    market: MarketDataService,
    settings,
    sma_map: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    rows: list[dict],
    *,
    skip_legacy: bool = False,
) -> None:
    is_end = OOS_START - pd.Timedelta(minutes=5)
    start_dt = start.to_pydatetime()
    end_dt = end.to_pydatetime()
    profiles: list[tuple[str, CombinedEntryProfile | None]] = [
        ("vol2x_pullback", PROFILE_VOL2X_PULLBACK),
        ("combined_full", PROFILE_COMBINED_FULL),
        *([] if skip_legacy else [("sweep_vol2x_legacy", None)]),
    ]
    for symbol in STOCK_SYMBOLS:
        print(f"  cargando {symbol} {ENTRY_TF}/{CONFIRM_TF}...", flush=True)
        bars = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
        htf = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)
        for variant_name, profile in profiles:
            print(f"  {symbol} variant={variant_name} ...", flush=True)
            if profile is None:
                m_is = metrics_legacy_vol2x(
                    settings,
                    bars,
                    htf,
                    symbol,
                    sma_map,
                    scope_start=start,
                    scope_end=is_end,
                    fee_pct=STOCK_FEE,
                    slippage_pct=STOCK_SLIP,
                )
                m_oos = metrics_legacy_vol2x(
                    settings,
                    bars,
                    htf,
                    symbol,
                    sma_map,
                    scope_start=OOS_START,
                    scope_end=end,
                    fee_pct=STOCK_FEE,
                    slippage_pct=STOCK_SLIP,
                )
            else:
                entries = precompute_stocks_combined(
                    settings, symbol, bars, htf, sma_map, profile
                )
                m_is = metrics_stocks_scope(
                    settings,
                    bars,
                    htf,
                    symbol,
                    sma_map,
                    entries,
                    scope_start=start,
                    scope_end=is_end,
                    fee_pct=STOCK_FEE,
                    slippage_pct=STOCK_SLIP,
                )
                m_oos = metrics_stocks_scope(
                    settings,
                    bars,
                    htf,
                    symbol,
                    sma_map,
                    entries,
                    scope_start=OOS_START,
                    scope_end=end,
                    fee_pct=STOCK_FEE,
                    slippage_pct=STOCK_SLIP,
                )
            for scope, m in (("IS", m_is), ("OOS", m_oos)):
                _print_row("stocks", scope, symbol, variant_name, m)
                rows.append(
                    {
                        "asset_class": "stocks",
                        "scope": scope,
                        "symbol": symbol,
                        "variant": variant_name,
                        "trades": int(m.get("trades", 0)),
                        "win_rate_pct": round(float(m.get("win_rate_pct", 0)), 2),
                        "pf": round(float(m.get("profit_factor", 0)), 4),
                        "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
                    }
                )


def _run_crypto(
    market: MarketDataService,
    settings,
    start: pd.Timestamp,
    end: pd.Timestamp,
    rows: list[dict],
    *,
    skip_legacy: bool = False,
) -> None:
    cash0 = float(settings.backtest_cash)
    profiles: list[tuple[str, CombinedEntryProfile | str]] = [
        ("vol2x_pullback", PROFILE_VOL2X_PULLBACK),
        ("combined_full", PROFILE_COMBINED_FULL),
        *([] if skip_legacy else [("breakout_asim_legacy", "legacy")]),
    ]
    start_dt = start.to_pydatetime()
    end_dt = end.to_pydatetime()
    for symbol in CRYPTO_SYMBOLS:
        print(f"  cargando {symbol} 1Hour...", flush=True)
        bars = load_1h(market, symbol, start_dt, end_dt)
        for variant_name, profile in profiles:
            print(f"  {symbol} variant={variant_name} ...", flush=True)
            if profile == "legacy":
                trades_is, m_is = run_backtest(
                    settings,
                    market,
                    symbol,
                    start=max(start, CRYPTO_HISTORY_START),
                    end=OOS_START - pd.Timedelta(hours=1),
                    fee_pct=CRYPTO_FEE,
                    slippage_pct=CRYPTO_SLIP,
                )
                _, m_oos = run_backtest(
                    settings,
                    market,
                    symbol,
                    start=OOS_START,
                    end=end,
                    fee_pct=CRYPTO_FEE,
                    slippage_pct=CRYPTO_SLIP,
                )
                m_is = summarize_crypto_trades(trades_is, cash0)
                m_is["symbol"] = symbol
            else:
                tr_is = run_crypto_combined_period(
                    settings,
                    bars,
                    symbol,
                    profile,
                    period_start=max(start, CRYPTO_HISTORY_START),
                    period_end=OOS_START - pd.Timedelta(hours=1),
                    fee_pct=CRYPTO_FEE,
                    slippage_pct=CRYPTO_SLIP,
                )
                tr_oos = run_crypto_combined_period(
                    settings,
                    bars,
                    symbol,
                    profile,
                    period_start=OOS_START,
                    period_end=end,
                    fee_pct=CRYPTO_FEE,
                    slippage_pct=CRYPTO_SLIP,
                )
                m_is = summarize_crypto_trades(tr_is, cash0)
                m_oos = summarize_crypto_trades(tr_oos, cash0)
                m_is["symbol"] = symbol
                m_oos["symbol"] = symbol
            for scope, m in (("IS", m_is), ("OOS", m_oos)):
                _print_row("crypto", scope, symbol, variant_name, m)
                rows.append(
                    {
                        "asset_class": "crypto",
                        "scope": scope,
                        "symbol": symbol,
                        "variant": variant_name,
                        "trades": int(m.get("trades", 0)),
                        "win_rate_pct": round(float(m.get("win_rate_pct", 0)), 2),
                        "pf": round(float(m.get("profit_factor", 0)), 4),
                        "net_pct": round(float(m.get("total_return_net_pct", 0)), 4),
                    }
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None)
    parser.add_argument("--years", type=float, default=YEARS)
    parser.add_argument(
        "--skip-legacy",
        action="store_true",
        help="Omitir sweep_vol2x_legacy y breakout_asim_legacy (más rápido)",
    )
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

    print("=" * 96)
    print("BACKTEST ENTRADA COMBINADA (RESEARCH) — sin cambios en producción")
    print("Acciones: 5Min + 15Min | salidas SL 1.2x ATR + trail 2.75x")
    print("Cripto: 1H + 4H | salidas asimétricas existentes")
    print(f"IS: {start.date()} -> {OOS_START.date()} | OOS: {OOS_START.date()} -> {end.date()}")
    print(
        f"Costos acciones {STOCK_FEE}%+{STOCK_SLIP}%/lado | "
        f"cripto {CRYPTO_FEE}%+{CRYPTO_SLIP}%/lado"
    )
    print("=" * 96)

    rows: list[dict] = []
    skip_legacy = bool(args.skip_legacy)
    print("\n--- ACCIONES ---")
    _run_stocks(market, settings, sma_map, start, end, rows, skip_legacy=skip_legacy)
    print("\n--- CRIPTO ---")
    _run_crypto(market, settings, start, end, rows, skip_legacy=skip_legacy)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "asset_class",
                "scope",
                "symbol",
                "variant",
                "trades",
                "win_rate_pct",
                "pf",
                "net_pct",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "=== Combined entry backtest (research only) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Variants:",
        "  sweep_vol2x_legacy — referencia sweep (multi-régimen + vol 2x)",
        "  vol2x_pullback — pullback EMA + vol 2x señal (sin ADX / skip / vol pullback 1.2x)",
        "  combined_full — vol2x + ADX>=20 + vol pullback + skip 30m NY + cripto skip 60m UTC hour0",
        "",
    ]
    for variant in ("vol2x_pullback", "combined_full", "sweep_vol2x_legacy", "breakout_asim_legacy"):
        lines.append(f"--- {variant} ---")
        for scope in ("IS", "OOS"):
            for symbol in (*STOCK_SYMBOLS, *CRYPTO_SYMBOLS):
                match = [
                    r
                    for r in rows
                    if r["variant"] == variant and r["scope"] == scope and r["symbol"] == symbol
                ]
                if not match:
                    continue
                r = match[0]
                lines.append(
                    f"{scope} {r['asset_class']} {symbol}: tr={r['trades']} PF={r['pf']} net%={r['net_pct']:+.2f}"
                )
        lines.append("")

    OUT_TXT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT_CSV}")
    print(f"Wrote {OUT_TXT}")
    print("\nNO activar live hasta revisar CSV y OK explícito del operador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
