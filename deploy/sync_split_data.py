#!/usr/bin/env python3
"""Separa data/ híbrido en data-stocks/ y data-crypto/ (open_positions.json por clase)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_crypto(symbol: str) -> bool:
    raw = str(symbol or "").strip().upper()
    return "/" in raw or (raw.endswith("USD") and len(raw) > 3 and raw[:-3].isalpha())


def _split_positions(src: Path, stocks_dir: Path, crypto_dir: Path) -> None:
    if not src.is_file():
        print(f"skip positions: no existe {src}")
        return
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"skip positions: {src} invalido ({exc})")
        return

    rows = raw.get("positions", {})
    if not isinstance(rows, dict):
        print(f"skip positions: formato inesperado en {src}")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stock_rows: dict = {}
    crypto_rows: dict = {}
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", key))
        target = crypto_rows if _is_crypto(sym) else stock_rows
        target[key] = row

    for dest_dir, subset in ((stocks_dir, stock_rows), (crypto_dir, crypto_rows)):
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / "open_positions.json"
        payload = {"updated_at": stamp, "positions": subset}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"OK {out} ({len(subset)} posiciones)")


def _copy_if_missing(name: str, src_dir: Path, dest_dir: Path) -> None:
    src = src_dir / name
    dest = dest_dir / name
    if dest.exists() or not src.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"OK copiado {name} -> {dest_dir.name}/")


def main() -> int:
    hybrid = ROOT / "data"
    stocks = ROOT / "data-stocks"
    crypto = ROOT / "data-crypto"
    stocks.mkdir(parents=True, exist_ok=True)
    crypto.mkdir(parents=True, exist_ok=True)

    src_positions = hybrid / "open_positions.json"
    if not src_positions.is_file() and (stocks / "open_positions.json").is_file():
        src_positions = stocks / "open_positions.json"

    _split_positions(src_positions, stocks, crypto)

    for name in (
        "trades.db",
        "control.json",
        "scheduler_state.json",
        "strategy_params.json",
        "breakout_state.json",
        "pending_orders.json",
        "telegram_events.json",
        "live_confirm.txt",
    ):
        _copy_if_missing(name, hybrid, stocks)

    for name in ("control.json", "telegram_events.json"):
        _copy_if_missing(name, hybrid, crypto)

    print("Datos split listos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
