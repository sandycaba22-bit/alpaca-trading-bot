"""Compara el libro local de posiciones con GET /v2/positions de Alpaca."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot.alpaca.client import AlpacaClient
from bot.alpaca.execution import classify_order
from bot.notify.telegram import TelegramNotifier
from bot.security.audit import audit
from bot.security.exceptions import ValidationError
from bot.market.assets import is_crypto_symbol, normalize_symbol
from bot.market.dust import effective_qty
from bot.config import Settings
from bot.storage.pending_orders import PendingOrderBook, RestingOrder
from bot.storage.positions import OpenPositionBook

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

logger = logging.getLogger(__name__)

# Fees/rounding cripto: 0.025493 pedido vs ~0.025429 fill.
_QTY_ABS_TOL = 1e-6
_QTY_REL_TOL = 0.01


def canonical_symbol(raw: str) -> str:
    """BTCUSD (broker) y BTC/USD (libro) son el mismo activo."""
    return normalize_symbol(raw)


def qty_matches(local_qty: float, broker_qty: float) -> bool:
    local = abs(float(local_qty))
    broker = abs(float(broker_qty))
    if local == broker:
        return True
    diff = abs(local - broker)
    scale = max(local, broker, _QTY_ABS_TOL)
    return diff <= max(_QTY_ABS_TOL, _QTY_REL_TOL * scale)


@dataclass(frozen=True)
class ReconcileMismatch:
    only_broker: list[str]
    only_local: list[str]
    qty_diff: list[str]


def broker_qty_map(client: AlpacaClient, settings: Settings | None = None) -> dict[str, float]:
    client.limiter.acquire("trading_read")
    positions = list(client.trading.get_all_positions())
    out: dict[str, float] = {}
    for pos in positions:
        key = canonical_symbol(str(getattr(pos, "symbol", "")))
        if not key:
            continue
        raw_qty = abs(float(pos.qty))
        if settings is not None:
            raw_qty = effective_qty(settings, key, raw_qty, raw_qty)
        if raw_qty <= 1e-8:
            continue
        out[key] = raw_qty
    return out


def local_qty_map(book: OpenPositionBook, settings: Settings | None = None) -> dict[str, float]:
    out: dict[str, float] = {}
    skipped: list[str] = []
    for pos in book.list():
        key = canonical_symbol(pos.symbol)
        if not key:
            continue
        # Dry-run never hits Alpaca; those rows must not fail reconcile.
        if pos.dry_run:
            skipped.append(key)
            continue
        raw_qty = abs(float(pos.qty))
        ref = float(pos.opened_qty or pos.qty)
        if settings is not None:
            raw_qty = effective_qty(settings, key, raw_qty, ref)
        if raw_qty <= 1e-8:
            continue
        out[key] = raw_qty
    if skipped:
        logger.info(
            "Reconcile omite posiciones dry-run del libro | %s",
            ",".join(skipped),
        )
    return out


def _profile_symbol_keys(settings: Settings | None) -> set[str] | None:
    if settings is None or settings.bot_profile == "hybrid":
        return None
    if settings.bot_profile == "stocks":
        symbols = settings.stock_symbols
    elif settings.bot_profile == "crypto":
        symbols = settings.crypto_symbols
    else:
        return None
    return {normalize_symbol(sym) for sym in symbols if str(sym).strip()}


def _filter_qty_map(qty_map: dict[str, float], allowed: set[str] | None) -> dict[str, float]:
    if allowed is None:
        return qty_map
    return {symbol: qty for symbol, qty in qty_map.items() if symbol in allowed}


def _default_sl_tp(settings: Settings, symbol: str, entry: float) -> tuple[float, float, float, float]:
    if is_crypto_symbol(symbol):
        stop_pct = max(float(settings.stop_loss_pct), 0.012)
        take_pct = max(float(settings.take_profit_pct), float(settings.crypto_min_tp_pct))
    else:
        stop_pct = float(settings.stop_loss_pct)
        take_pct = float(settings.take_profit_pct)
    stop_price = entry * (1 - stop_pct)
    take_price = entry * (1 + take_pct)
    return stop_price, take_price, stop_pct, take_pct


def adopt_broker_positions_for_profile(
    client: AlpacaClient,
    book: OpenPositionBook,
    settings: Settings,
) -> int:
    """Incorpora al libro posiciones del broker ausentes localmente (solo símbolos del perfil)."""
    allowed = _profile_symbol_keys(settings)
    if allowed is None:
        return 0

    client.limiter.acquire("trading_read")
    positions = list(client.trading.get_all_positions())
    local = local_qty_map(book, settings)
    adopted = 0
    for pos in positions:
        key = canonical_symbol(str(getattr(pos, "symbol", "")))
        if not key or key not in allowed:
            continue
        qty = float(getattr(pos, "qty", 0) or 0)
        if abs(qty) <= 1e-8:
            continue
        if key in local:
            continue
        entry = float(getattr(pos, "avg_entry_price", 0) or 0)
        if entry <= 0:
            continue
        stop_price, take_price, stop_pct, take_pct = _default_sl_tp(settings, key, entry)
        book.open(
            key,
            abs(qty),
            entry,
            stop_price=stop_price,
            take_profit_price=take_price,
            stop_pct=stop_pct,
            take_profit_pct=take_pct,
            dry_run=False,
        )
        adopted += 1
        logger.info(
            "Reconcile adoptó posición del broker | %s qty=%s entry=%.4f",
            key,
            qty,
            entry,
        )
    return adopted


def diff_maps(broker: dict[str, float], local: dict[str, float]) -> ReconcileMismatch:
    only_broker = sorted(set(broker) - set(local))
    only_local = sorted(set(local) - set(broker))
    qty_diff: list[str] = []
    for symbol in sorted(set(broker) & set(local)):
        if not qty_matches(local[symbol], broker[symbol]):
            qty_diff.append(
                f"{symbol} libro={local[symbol]:g} broker={broker[symbol]:g}"
            )
    return ReconcileMismatch(only_broker=only_broker, only_local=only_local, qty_diff=qty_diff)


def format_mismatch(mismatch: ReconcileMismatch) -> str:
    lines = ["Discrepancia libro local vs Alpaca GET /v2/positions:"]
    if mismatch.only_broker:
        lines.append("En Alpaca, no en libro: " + ", ".join(mismatch.only_broker))
    if mismatch.only_local:
        lines.append("En libro, no en Alpaca: " + ", ".join(mismatch.only_local))
    if mismatch.qty_diff:
        lines.append("Qty distinta: " + "; ".join(mismatch.qty_diff))
    lines.append("El bot no arranca a operar. Alinea open_positions.json con el broker o cierra/abre a mano.")
    return "\n".join(lines)


def _order_side(order: Any) -> str:
    raw = getattr(order, "side", "") or ""
    text = str(raw)
    if "." in text:
        text = text.split(".")[-1]
    return text.lower()


def adopt_open_orders(
    client: AlpacaClient,
    book: OpenPositionBook,
    pending: PendingOrderBook,
) -> int:
    """
    Incorpora órdenes DAY/GTC abiertas del broker al registro de fill pendiente.
    No cierra el libro: una accepted sin fill no es un cierre.
    """
    try:
        client.limiter.acquire("trading_read")
        orders = list(
            client.trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        )
    except Exception as exc:
        logger.warning("Reconcile no pudo listar órdenes abiertas: %s", type(exc).__name__)
        return 0

    broker = broker_qty_map(client)
    adopted = 0
    for order in orders:
        order_id = str(getattr(order, "id", "") or "")
        symbol = canonical_symbol(str(getattr(order, "symbol", "")))
        if not order_id or not symbol:
            continue
        kind = classify_order(order)
        if kind == "failed":
            continue
        side = _order_side(order)
        qty = float(getattr(order, "qty", 0) or 0)
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        limit_price = getattr(order, "limit_price", None)
        status = str(getattr(order, "status", "") or "accepted")
        is_close = side == "sell" and symbol in broker
        tracked = book.get(symbol)
        existing = pending.get(order_id)
        pending.upsert(
            RestingOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                reason=(existing.reason if existing else ("mode_switch" if is_close else "signal")),
                is_close=is_close or bool(existing and existing.is_close),
                event_id=existing.event_id if existing else "",
                submitted_at=(
                    existing.submitted_at
                    if existing
                    else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                ),
                filled_qty=filled_qty,
                entry_price=(
                    existing.entry_price
                    if existing and existing.entry_price
                    else (float(tracked.avg_entry_price) if tracked else None)
                ),
                signal_price=existing.signal_price if existing else 0.0,
                order_type=str(getattr(order, "type", "limit") or "limit"),
                limit_price=float(limit_price) if limit_price else None,
                last_status=status,
            )
        )
        adopted += 1
        logger.info(
            "Reconcile orden en vuelo | %s %s id=%s status=%s filled=%s/%s limit=%s",
            side,
            symbol,
            order_id,
            status,
            f"{filled_qty:g}",
            f"{qty:g}",
            f"{float(limit_price):.4f}" if limit_price else "—",
        )
    return adopted


def assert_positions_match(
    client: AlpacaClient,
    book: OpenPositionBook | None = None,
    notifier: TelegramNotifier | None = None,
    settings: Settings | None = None,
) -> None:
    """
    Aborta el arranque si el libro y el broker no coinciden,
    o si no se pueden leer las posiciones.
    """
    book = book or OpenPositionBook()
    try:
        broker = broker_qty_map(client, settings)
    except Exception as exc:
        message = (
            "No se pudo leer GET /v2/positions "
            f"({type(exc).__name__}). El bot no arranca a ciegas."
        )
        logger.error("%s", message)
        audit("reconcile", "deny", reason="broker_positions_unavailable")
        if notifier is not None:
            notifier.notify_position_mismatch(message)
        raise ValidationError(message) from None

    if settings is not None and settings.bot_profile in {"stocks", "crypto"}:
        adopted = adopt_broker_positions_for_profile(client, book, settings)
        if adopted:
            logger.info("Reconcile adoptó %s posiciones del broker al libro local", adopted)

    local = local_qty_map(book, settings)
    allowed = _profile_symbol_keys(settings)
    broker_view = _filter_qty_map(broker, allowed)
    local_view = _filter_qty_map(local, allowed)
    mismatch = diff_maps(broker_view, local_view)
    if mismatch.only_broker or mismatch.only_local or mismatch.qty_diff:
        message = format_mismatch(mismatch)
        if allowed is not None:
            message += f"\nPerfil activo: {settings.bot_profile} ({','.join(sorted(allowed)) or '—'})"
        logger.error("%s", message)
        audit(
            "reconcile",
            "deny",
            reason="position_mismatch",
            only_broker=",".join(mismatch.only_broker) or "-",
            only_local=",".join(mismatch.only_local) or "-",
        )
        if notifier is not None:
            profile = settings.bot_profile if settings is not None else "hybrid"
            notifier.notify_position_mismatch(message, profile=profile)
        raise ValidationError(message)

    logger.info(
        "Reconcile OK | broker=%s | libro=%s | perfil=%s",
        ",".join(sorted(broker_view)) or "—",
        ",".join(sorted(local_view)) or "—",
        settings.bot_profile if settings is not None else "hybrid",
    )
    audit("reconcile", "allow", symbols=",".join(sorted(broker)) or "none")
