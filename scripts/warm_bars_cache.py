#!/usr/bin/env python3
"""
Rellena data/bars_cache/ con ~6 años de velas Alpaca (offline / VPS / PC local).

Los bots live siguen usando API reciente; esto es para backtests y scripts en scripts/.

Ejemplos:
  python scripts/warm_bars_cache.py --preset crypto_night
  python scripts/warm_bars_cache.py --preset stocks_elite
  ENV_FILE=.env.stocks_top50 python scripts/warm_bars_cache.py --preset stocks_top50
  python scripts/warm_bars_cache.py --preset all --years 6
  python scripts/warm_bars_cache.py --preset stocks_top50 --force --years 4

Datos NO van en git (.gitignore data/*). En VPS: git pull + mismo comando, o copiar data/bars_cache/.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService, resolve_stock_data_feed
from bot.config import PROJECT_ROOT, load_settings
from bot.market.bars_cache import DEFAULT_YEARS, WARM_PRESETS, cache_dir, warm_many

_WARM_BOT_PROFILES = frozenset({"stocks", "stocks_top50", "hybrid"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Cache local de velas OHLC (Alpaca)")
    parser.add_argument(
        "--preset",
        action="append",
        dest="presets",
        metavar="NAME",
        help=f"Repetible. Valores: {', '.join(WARM_PRESETS)} | all",
    )
    parser.add_argument("--symbols", help="CSV extra (ej. AAPL,MSFT,BTC/USD)")
    parser.add_argument("--timeframes", help="CSV extra (ej. 5Min,15Min,1Hour)")
    parser.add_argument("--years", type=float, default=DEFAULT_YEARS, help="Años hacia atrás")
    parser.add_argument("--force", action="store_true", help="Re-descargar aunque exista .pkl")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("ENV_FILE", ".env"),
        help="Archivo env con APCA_API_* (default ENV_FILE o .env)",
    )
    args = parser.parse_args()

    if not args.presets and not args.symbols:
        parser.error("Indica --preset (uno o más) o --symbols")

    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = PROJECT_ROOT / env_path
    if args.env_file:
        os.environ["ENV_FILE"] = args.env_file
    load_dotenv(env_path if env_path.is_file() else PROJECT_ROOT / ".env")
    if os.getenv("BOT_PROFILE", "").strip() not in _WARM_BOT_PROFILES:
        os.environ["BOT_PROFILE"] = "stocks"
    settings = load_settings(env_path if env_path.is_file() else None)
    client = AlpacaClient(settings)
    feed = resolve_stock_data_feed(getattr(settings, "stock_data_feed", None))
    market = MarketDataService(client, feed=feed)

    symbols: set[str] = set()
    timeframes: set[str] = set()
    if args.presets:
        from bot.market.bars_cache import expand_presets

        ps, ts = expand_presets(args.presets)
        symbols.update(ps)
        timeframes.update(ts)
    if args.symbols:
        symbols.update(s.strip() for s in args.symbols.split(",") if s.strip())
    if args.timeframes:
        timeframes.update(s.strip() for s in args.timeframes.split(",") if s.strip())

    if not symbols or not timeframes:
        print("Sin símbolos o timeframes.", file=sys.stderr)
        return 1

    try:
        settings.validate()
    except Exception as exc:
        print(
            "No se puede descargar velas: revisa APCA_API_KEY_ID y APCA_API_SECRET_KEY "
            f"en {env_path if env_path.is_file() else PROJECT_ROOT / '.env'} ({exc})",
            file=sys.stderr,
        )
        return 1

    out_dir = cache_dir()
    print(f"Cache dir: {out_dir}")
    print(f"Símbolos: {len(symbols)} | TFs: {sorted(timeframes)} | años={args.years}")

    rows = warm_many(
        market,
        tuple(sorted(symbols)),
        tuple(sorted(timeframes)),
        years=args.years,
        force=args.force,
    )
    ok = sum(1 for _, _, n in rows if n > 0)
    print(f"Listo | pares con datos: {ok}/{len(rows)} | manifest: {out_dir / 'manifest.json'}")
    return 0 if ok > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
