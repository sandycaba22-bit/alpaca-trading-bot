"""Snapshot JSON para el panel web. No imprime secretos."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.execution import OrderExecutor
from bot.config import load_settings
from bot.reporting.pnl import PnLEvent, compute_pnl


def main() -> int:
    settings = load_settings()
    client = AlpacaClient(settings)
    account = client.snapshot_account()
    clock = client.get_clock()
    executor = OrderExecutor(client, dry_run=True)
    positions = []
    for pos in executor.list_positions():
        qty = float(pos.qty)
        entry = float(pos.avg_entry_price)
        current = float(getattr(pos, "current_price", None) or entry)
        snap = compute_pnl(str(pos.symbol), qty, entry, current, PnLEvent.UPDATED)
        positions.append(
            {
                "symbol": snap.symbol,
                "qty": snap.qty,
                "side": snap.side,
                "entry": round(snap.entry_price, 4),
                "last": round(snap.current_price, 4),
                "invested": round(snap.invested, 2),
                "pnl_abs": round(snap.pnl_abs, 2),
                "pnl_pct": round(snap.pnl_pct, 2),
                "in_profit": snap.in_profit,
                "color": snap.color_name,
            }
        )
    payload = {
        "paper": account.paper,
        "dry_run": settings.dry_run,
        "status": str(account.status),
        "currency": account.currency,
        "equity": round(account.equity, 2),
        "cash": round(account.cash, 2),
        "buying_power": round(account.buying_power, 2),
        "market_open": bool(clock.is_open),
        "next_open": str(clock.next_open) if clock.next_open else None,
        "next_close": str(clock.next_close) if clock.next_close else None,
        "symbols": settings.symbols,
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "positions": positions,
    }
    json.dump(payload, sys.stdout, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        json.dump({"error": "No se pudo obtener el estado de la cuenta."}, sys.stdout)
        raise SystemExit(1)
