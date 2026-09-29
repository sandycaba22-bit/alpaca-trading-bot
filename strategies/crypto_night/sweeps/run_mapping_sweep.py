#!/usr/bin/env python3
"""Mapeo offline V1–V5 — NO levanta PM2 ni run_bot."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.crypto_night_settings import load_crypto_night_settings
from strategies.crypto_night.backtest import run_variant_backtest, summarize_variants
from strategies.crypto_night.variants import SweepVariant

OOS_DEFAULT = "2025-01-01"


def _client(settings) -> AlpacaClient:
    cfg = SimpleNamespace(
        api_key_id=settings.api_key_id,
        api_secret_key=settings.api_secret_key,
        api_base_url=settings.api_base_url,
        paper=settings.paper,
        api_data_per_minute=120,
        order_per_minute=30,
        order_per_day=200,
    )
    return AlpacaClient(cfg)


def _load(symbol: str, market: MarketDataService, tf: str, start: datetime, end: datetime):
    return market.get_bars_range(symbol, tf, start, end)


def main() -> int:
    parser = argparse.ArgumentParser(description="Crypto Night Fortress mapping sweep")
    parser.add_argument("--years", type=int, default=2, help="Años de historia hacia atrás")
    parser.add_argument("--months", type=int, default=0, help="Si >0, ignora --years y usa N meses")
    parser.add_argument("--oos-start", default=OOS_DEFAULT)
    args = parser.parse_args()

    if (ROOT / "data" / "LOCAL_DISABLED").is_file():
        print("LOCAL_DISABLED — sweep offline permitido; no arranca PM2.", file=sys.stderr)

    os.environ.setdefault("ENV_FILE", ".env.crypto_night")
    os.environ.setdefault("BOT_PROFILE", "crypto_night")
    try:
        settings = load_crypto_night_settings()
    except Exception as exc:
        print(f"Config: {exc}", file=sys.stderr)
        print("Usa ENV_FILE=.env si tienes keys en .env local (solo research).", file=sys.stderr)
        return 1

    end = datetime.now(timezone.utc)
    if args.months and args.months > 0:
        start = end - timedelta(days=30 * int(args.months))
    else:
        start = end - timedelta(days=365 * max(1, args.years))
    print(f"Window {start.date()} -> {end.date()} | OOS from {args.oos_start}", flush=True)
    oos = datetime.fromisoformat(args.oos_start).replace(tzinfo=timezone.utc)

    client = _client(settings)
    market = MarketDataService(client)
    all_stats = []
    symbols = ("BTC/USD", "ETH/USD")

    for variant in (
        SweepVariant.V1,
        SweepVariant.V2,
        SweepVariant.V3,
        SweepVariant.V4,
        SweepVariant.V5,
    ):
        print(f"\n=== {variant.value} ===", flush=True)
        for symbol in symbols:
            print(f"  {symbol} fetch...", flush=True)
            b15 = _load(symbol, market, "15Min", start, end)
            b1h = _load(symbol, market, "1Hour", start, end)
            b4h = _load(symbol, market, "4Hour", start, end)
            b1d = _load(symbol, market, "1Day", start, end)
            stats = run_variant_backtest(
                variant,
                symbol=symbol,
                bars_15m=b15,
                bars_1h=b1h,
                bars_4h=b4h,
                bars_1d=b1d,
                oos_start=oos,
            )
            all_stats.append(stats)
            print(
                f"  trades={stats.trades} R_sum={stats.r_net_sum:.2f} rejects={dict(stats.rejections)}",
                flush=True,
            )

    summary, champion = summarize_variants(all_stats)
    out_dir = settings.log_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "crypto_night_mapping_sweep_summary.txt"
    csv_path.write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"\nWrote {csv_path}")
    print(f"CHAMPION={champion or 'NONE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
