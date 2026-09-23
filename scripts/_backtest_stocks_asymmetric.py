"""Validación acciones — baseline TP fijo vs salidas asimétricas (6 años + OOS 2025).

Costos acciones: 0%% comisión Alpaca + 0.03%% slippage por lado (ajustable).
No activar STOCK_ASYMMETRIC_EXITS_ENABLED en live hasta neto OOS > baseline.

  .venv/bin/python scripts/_backtest_stocks_asymmetric.py
"""

from __future__ import annotations

import argparse
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

from _stocks_asymmetric_backtest_lib import (  # noqa: E402
    OOS_START,
    SYMBOLS,
    _sma_slow_map,
    pf_str,
    run_symbol_comparison,
)

YEARS = 6
FEE = 0.0
SLIP = 0.03


def _print_row(label: str, m: dict[str, float]) -> None:
    print(
        f"  {label:<12} {m.get('symbol', '?'):<6} | tr={int(m.get('trades', 0)):>4} | "
        f"win%={m.get('win_rate_pct', 0):.1f} | PF={pf_str(float(m.get('profit_factor', 0)))} | "
        f"net%={m.get('total_return_net_pct', 0):+.2f} | "
        f"avgW=${m.get('avg_win_net', 0):+.2f} | avgL=${m.get('avg_loss_net', 0):+.2f} | "
        f"W/L={m.get('avg_win_r', 0):.2f}x"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None, help="UTC end date YYYY-MM-DD (default: now)")
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    parser.add_argument("--fee-pct", type=float, default=FEE)
    parser.add_argument("--slippage-pct", type=float, default=SLIP)
    parser.add_argument("--years", type=float, default=YEARS)
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

    print("=" * 78)
    print("ACCIONES | baseline=TP fijo MIN_TP + STOCK_ATR_SL | asym=SL ceñido + trail ancho")
    print(f"IS: {start.date()} -> {OOS_START.date()} | OOS: {OOS_START.date()} -> {end.date()}")
    print(
        f"Entradas: 5Min multi-regimen + confirm {settings.confirm_higher_tf} | "
        f"SMA lenta: {sma_map or 'default'}"
    )
    print(
        f"Baseline SL={settings.stock_atr_sl_mult}x trail={settings.atr_trailing_mult}x "
        f"min_tp={settings.min_tp_pct * 100:.2f}% | "
        f"Asym SL={settings.stock_asymmetric_sl_atr_mult}x "
        f"trail={settings.stock_asymmetric_trail_atr_mult}x sin TP %"
    )
    print(f"Costos: {args.fee_pct}% + {args.slippage_pct}%/lado")
    print("=" * 78)

    out_path = PROJECT_ROOT / "logs" / "stocks_asymmetric_validation.txt"
    lines: list[str] = []

    for symbol in args.symbols:
        print(f"\n--- {symbol} ---")
        is_b, is_a, oos_b, oos_a = run_symbol_comparison(
            settings,
            market,
            symbol,
            start=start,
            end=end,
            fee_pct=args.fee_pct,
            slippage_pct=args.slippage_pct,
            sma_slow_by_symbol=sma_map,
        )
        print("IN-SAMPLE:")
        _print_row("baseline", is_b)
        _print_row("asymmetric", is_a)
        print("OUT-OF-SAMPLE (2025+):")
        _print_row("baseline", oos_b)
        _print_row("asymmetric", oos_a)
        delta_oos = float(oos_a.get("total_return_net_pct", 0)) - float(
            oos_b.get("total_return_net_pct", 0)
        )
        verdict = "MEJORA OOS" if delta_oos > 0 else "NO MEJORA OOS"
        print(f"  -> {verdict} (delta net OOS {delta_oos:+.2f} pp)")
        lines.append(
            f"{symbol},is,baseline,{is_b.get('trades',0)},{is_b.get('total_return_net_pct',0):.4f},"
            f"{is_b.get('win_rate_pct',0):.2f},{is_b.get('profit_factor',0):.4f},"
            f"{is_b.get('avg_win_net',0):.4f},{is_b.get('avg_loss_net',0):.4f}"
        )
        lines.append(
            f"{symbol},is,asymmetric,{is_a.get('trades',0)},{is_a.get('total_return_net_pct',0):.4f},"
            f"{is_a.get('win_rate_pct',0):.2f},{is_a.get('profit_factor',0):.4f},"
            f"{is_a.get('avg_win_net',0):.4f},{is_a.get('avg_loss_net',0):.4f}"
        )
        lines.append(
            f"{symbol},oos,baseline,{oos_b.get('trades',0)},{oos_b.get('total_return_net_pct',0):.4f},"
            f"{oos_b.get('win_rate_pct',0):.2f},{oos_b.get('profit_factor',0):.4f},"
            f"{oos_b.get('avg_win_net',0):.4f},{oos_b.get('avg_loss_net',0):.4f}"
        )
        lines.append(
            f"{symbol},oos,asymmetric,{oos_a.get('trades',0)},{oos_a.get('total_return_net_pct',0):.4f},"
            f"{oos_a.get('win_rate_pct',0):.2f},{oos_a.get('profit_factor',0):.4f},"
            f"{oos_a.get('avg_win_net',0):.4f},{oos_a.get('avg_loss_net',0):.4f}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = "symbol,scope,policy,trades,net_pct,win_pct,pf,avg_win,avg_loss"
    out_path.write_text(header + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path}")
    print(
        "\nLive: mantener STOCK_ASYMMETRIC_EXITS_ENABLED=false hasta OOS asymmetric > baseline."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
