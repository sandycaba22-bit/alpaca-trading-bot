"""Validación Fase 1 — cripto asimétrico 1H/4H (6 años IS + OOS 2025).

Costos: 0.25%% comisión + 0.03%% slippage por lado.
No desplegar paper/live hasta neto IS y OOS positivos.

  python scripts/_backtest_crypto_asymmetric.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings

from _crypto_asymmetric_backtest_lib import (  # noqa: E402
    HISTORY_START,
    OOS_START,
    run_backtest,
)

SYMBOLS = ("BTC/USD", "ETH/USD")
FEE = 0.25
SLIP = 0.03


def _print_metrics(scope: str, m: dict[str, float]) -> None:
    pf = m.get("profit_factor", 0.0)
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(
        f"  {m.get('symbol','?'):<10} | trades={int(m.get('trades',0)):>4} | "
        f"win%={m.get('win_rate_pct',0):.1f} | PF={pf_s} | net%={m.get('total_return_net_pct',0):+.2f} | "
        f"avg win=${m.get('avg_win_net',0):+.2f} | avg loss=${m.get('avg_loss_net',0):+.2f} | "
        f"W/L ratio={m.get('avg_win_r',0):.2f}x"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default="2026-09-22")
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    args = parser.parse_args()

    end = pd.Timestamp(args.end, tz="UTC")
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)

    print("=" * 72)
    print("CRIPTO ASIMÉTRICO 1H/4H | SL ATR ceñido | trailing ancho | SIN TP fijo %")
    print(f"IS: {HISTORY_START.date()} -> {end.date()} | OOS: {OOS_START.date()} -> {end.date()}")
    print(f"Costos: {FEE}% + {SLIP}%/lado | SMA/ATR/ADX períodos completos (no escala mínima 1H)")
    print("=" * 72)

    is_ok = True
    oos_ok = True

    print("\nIN-SAMPLE:")
    for symbol in args.symbols:
        _, m = run_backtest(
            settings,
            market,
            symbol,
            start=HISTORY_START,
            end=end,
            fee_pct=FEE,
            slippage_pct=SLIP,
        )
        _print_metrics("is", m)
        if float(m.get("total_return_net_pct", 0)) <= 0:
            is_ok = False

    print("\nOUT-OF-SAMPLE (2025-01-01 -> hoy):")
    for symbol in args.symbols:
        _, m = run_backtest(
            settings,
            market,
            symbol,
            start=OOS_START,
            end=end,
            fee_pct=FEE,
            slippage_pct=SLIP,
        )
        _print_metrics("oos", m)
        if float(m.get("total_return_net_pct", 0)) <= 0:
            oos_ok = False

    print("\n" + "=" * 72)
    if is_ok and oos_ok:
        print("RESULTADO: IS y OOS netos positivos — candidato a habilitar CRYPTO_ASYMMETRIC_LIVE_ENABLED")
    else:
        print("RESULTADO: NO desplegar — IS u OOS neto <= 0 (ajustar params o aceptar no-go)")
    print("Ver bot/strategy/DEPRECATED_CRYPTO_6M.md — pipeline 6m descartado.")
    print("=" * 72)

    out = PROJECT_ROOT / "logs" / "crypto_asymmetric_validation.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"is_ok={is_ok} oos_ok={oos_ok} end={end.date()}\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
