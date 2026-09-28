#!/usr/bin/env python3
"""Inicializa data-stocks/ y data-stocks-top50/ desde data/ híbrido legacy (solo acciones)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_stock_symbol(symbol: str) -> bool:
    raw = str(symbol or "").strip().upper()
    if not raw or "/" in raw:
        return False
    return True


def _split_positions(src: Path, stocks_dir: Path) -> None:
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
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", key))
        if _is_stock_symbol(sym):
            stock_rows[key] = row

    stocks_dir.mkdir(parents=True, exist_ok=True)
    out = stocks_dir / "open_positions.json"
    payload = {"updated_at": stamp, "positions": stock_rows}
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"OK {out} ({len(stock_rows)} posiciones acciones)")


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
    top50 = ROOT / "data-stocks-top50"
    stocks.mkdir(parents=True, exist_ok=True)
    top50.mkdir(parents=True, exist_ok=True)

    src_positions = hybrid / "open_positions.json"
    if not src_positions.is_file() and (stocks / "open_positions.json").is_file():
        src_positions = stocks / "open_positions.json"

    _split_positions(src_positions, stocks)

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
        _copy_if_missing(name, hybrid, top50)

    for name in ("control.json", "telegram_events.json"):
        _copy_if_missing(name, stocks, top50)

    print("Datos acciones listos (elite + top50).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
