#!/usr/bin/env python3
"""Cierra posición(es) en Alpaca sin arrancar el motor (p. ej. BTCUSD huérfana en paper)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.reconcile import canonical_symbol
from bot.config import PROJECT_ROOT, load_settings
from bot.logging_setup import setup_logging
from bot.runtime_paths import configure_runtime_paths


def _load_env(env_file: str) -> None:
    os.environ["ENV_FILE"] = env_file


def _list_positions(client: AlpacaClient) -> list:
    client.limiter.acquire("trading_read")
    return list(client.trading.get_all_positions())


def _is_crypto_position(pos) -> bool:
    ac = str(getattr(pos, "asset_class", "") or "").lower()
    if ac == "crypto":
        return True
    sym = canonical_symbol(str(getattr(pos, "symbol", "") or ""))
    return sym.endswith("USD") and sym not in {"USD"}


def _purge_local_book(data_dir: Path, symbol: str) -> bool:
    path = data_dir / "open_positions.json"
    if not path.is_file():
        return False
    key = canonical_symbol(symbol)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    positions = payload.get("positions")
    if not isinstance(positions, dict) or key not in positions:
        return False
    positions.pop(key, None)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cierra posición en Alpaca (one-shot)")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("ENV_FILE", ".env.stocks_top50"),
        help="Env con APCA_* (default: paper Top 50)",
    )
    parser.add_argument("--symbol", default="", help="Símbolo broker (p. ej. BTCUSD)")
    parser.add_argument(
        "--crypto-orphans",
        action="store_true",
        help="Cierra todas las posiciones asset_class=crypto (o *USD cripto)",
    )
    parser.add_argument("--list", action="store_true", help="Solo listar posiciones abiertas")
    parser.add_argument("--dry-run", action="store_true", help="No enviar órdenes de cierre")
    args = parser.parse_args(argv)

    _load_env(args.env_file)
    setup_logging()
    settings = load_settings()
    configure_runtime_paths(settings)
    client = AlpacaClient(settings)

    positions = _list_positions(client)
    if args.list or (not args.symbol and not args.crypto_orphans):
        if not positions:
            print("Sin posiciones abiertas en la cuenta.")
            return 0
        for pos in positions:
            sym = str(getattr(pos, "symbol", ""))
            qty = getattr(pos, "qty", "")
            ac = getattr(pos, "asset_class", "")
            print(f"  {sym} qty={qty} asset_class={ac}")
        return 0

    targets: list[str] = []
    if args.crypto_orphans:
        for pos in positions:
            if _is_crypto_position(pos):
                targets.append(canonical_symbol(str(getattr(pos, "symbol", ""))))
    elif args.symbol:
        targets = [canonical_symbol(args.symbol)]

    if not targets:
        print("Nada que cerrar (revisa --list).")
        return 0

    for sym in targets:
        pos = next(
            (p for p in positions if canonical_symbol(str(getattr(p, "symbol", ""))) == sym),
            None,
        )
        if pos is None:
            print(f"{sym}: no hay posición en broker — omitido")
            continue
        qty = getattr(pos, "qty", "")
        print(f"{sym}: cerrar qty={qty} paper={settings.paper} dry_run={args.dry_run}")
        if args.dry_run:
            continue
        client.limiter.acquire("order")
        client.trading.close_position(sym)
        print(f"{sym}: orden de cierre enviada")

    if args.dry_run:
        return 0

    for sub in ("data-stocks", "data-stocks-top50"):
        data_dir = PROJECT_ROOT / sub
        for sym in targets:
            if _purge_local_book(data_dir, sym):
                print(f"Libro local limpiado: {sub}/open_positions.json [{sym}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
