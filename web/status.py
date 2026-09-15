"""Snapshot JSON para el panel web. No imprime secretos."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.alpaca.client import AccountSnapshot, AlpacaClient
from bot.alpaca.execution import OrderExecutor
from bot.config import load_settings
from bot.notify.telegram import TelegramNotifier
from bot.reporting.pnl import PnLEvent, compute_pnl
from bot.scheduler.multi_tf import SchedulerStateStore
from bot.storage.control import control_status
from bot.storage.journal import TradeJournal
from bot.storage.params import public_strategy_snapshot
from bot.market.assets import all_symbols
from bot.market.mode import resolve_trading_mode, trading_mode_label


def _empty_account(settings) -> AccountSnapshot:
    return AccountSnapshot(
        id="",
        status="unavailable",
        currency="USD",
        cash=0.0,
        buying_power=0.0,
        equity=0.0,
        portfolio_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        paper=settings.paper,
    )


def main() -> int:
    settings = load_settings()
    client = AlpacaClient(settings)
    account = client.snapshot_account_optional() or _empty_account(settings)
    clock = client.get_market_clock()
    executor = OrderExecutor(client, dry_run=settings.dry_run)
    journal = TradeJournal()
    positions = []
    try:
        for pos in executor.list_positions():
            qty = float(pos.qty)
            entry = float(pos.avg_entry_price)
            current = float(getattr(pos, "current_price", None) or entry)
            snap = compute_pnl(str(pos.symbol), qty, entry, current, PnLEvent.UPDATED)
            tracked = executor.position_book.get(str(pos.symbol))
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
                    "stop_price": round(tracked.stop_price, 4) if tracked else None,
                    "take_profit_price": round(tracked.take_profit_price, 4) if tracked else None,
                    "stop_pct": round(tracked.stop_pct * 100, 2) if tracked else None,
                    "take_profit_pct": round(tracked.take_profit_pct * 100, 2) if tracked else None,
                }
            )
    except Exception:
        positions = []
    trades = []
    for trade in journal.list_trades(200):
        trades.append(
            {
                "id": trade.id,
                "created_at": trade.created_at,
                "symbol": trade.symbol,
                "side": trade.side,
                "qty": trade.qty,
                "price": round(trade.price, 4),
                "pnl_abs": None if trade.pnl_abs is None else round(trade.pnl_abs, 2),
                "pnl_pct": None if trade.pnl_pct is None else round(trade.pnl_pct, 2),
                "reason": trade.reason,
                "dry_run": trade.dry_run,
            }
        )
    realized = journal.realized_pnl()
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    telegram_enabled = notifier.enabled
    scheduler = SchedulerStateStore.read_public()
    telegram_verified = bool(scheduler.get("telegram_enabled")) if scheduler else False
    if telegram_enabled and not telegram_verified:
        try:
            telegram_verified = notifier.verify()
        except Exception:
            telegram_verified = False
    mode, active_symbols = resolve_trading_mode(clock, settings)
    mode_label = trading_mode_label(mode)
    payload = {
        "paper": account.paper,
        "mode": "hybrid",
        "dry_run": settings.dry_run,
        "status": str(account.status),
        "currency": account.currency,
        "equity": round(account.equity, 2),
        "cash": round(account.cash, 2),
        "buying_power": round(account.buying_power, 2),
        "market_open": bool(clock.is_open),
        "stock_feed_ok": clock.stock_feed_ok,
        "next_open": str(clock.next_open) if clock.next_open else None,
        "next_close": str(clock.next_close) if clock.next_close else None,
        "symbols": settings.stock_symbols,
        "stock_symbols": settings.stock_symbols,
        "crypto_symbols": settings.crypto_symbols,
        "all_symbols": all_symbols(settings),
        "trading_mode": mode.value,
        "trading_mode_label": mode_label,
        "active_symbols": active_symbols,
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "positions": positions,
        "trades": trades,
        "realized_pnl": round(realized, 2),
        "trade_count": journal.count(),
        "bot": control_status(),
        "telegram_enabled": telegram_enabled,
        "telegram_verified": telegram_verified,
        "scheduler_tick_seconds": settings.scheduler_tick_seconds,
        "poll_interval_seconds": settings.scheduler_tick_seconds,
        "scheduler": scheduler,
        "timeframes": {
            "bar_timeframe": settings.bar_timeframe,
            "confirm_higher_tf": settings.confirm_higher_tf,
            "crypto_bar_timeframe": settings.crypto_bar_timeframe,
            "crypto_regime_timeframe": settings.crypto_regime_timeframe,
        },
        "strategy": public_strategy_snapshot(),
    }
    json.dump(payload, sys.stdout, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        json.dump({"error": "No se pudo obtener el estado de la cuenta."}, sys.stdout)
        raise SystemExit(1)
