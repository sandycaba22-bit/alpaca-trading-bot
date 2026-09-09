"""Compara el libro local de posiciones con GET /v2/positions de Alpaca."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.alpaca.client import AlpacaClient
from bot.notify.telegram import TelegramNotifier
from bot.security.audit import audit
from bot.security.exceptions import ValidationError
from bot.storage.positions import OpenPositionBook

logger = logging.getLogger(__name__)

# Fees/rounding cripto: 0.025493 pedido vs ~0.025429 fill.
_QTY_ABS_TOL = 1e-6
_QTY_REL_TOL = 0.01


def canonical_symbol(raw: str) -> str:
    """BTCUSD (broker) y BTC/USD (libro) son el mismo activo."""
    symbol = str(raw or "").strip().upper()
    if not symbol:
        return ""
    if "/" in symbol:
        return symbol
    if symbol.endswith("USD") and len(symbol) > 3 and symbol[:-3].isalpha():
        return f"{symbol[:-3]}/USD"
    return symbol


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


def broker_qty_map(client: AlpacaClient) -> dict[str, float]:
    client.limiter.acquire("trading_read")
    positions = list(client.trading.get_all_positions())
    out: dict[str, float] = {}
    for pos in positions:
        key = canonical_symbol(str(getattr(pos, "symbol", "")))
        if not key:
            continue
        out[key] = abs(float(pos.qty))
    return out


def local_qty_map(book: OpenPositionBook) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in book.list():
        key = canonical_symbol(pos.symbol)
        if not key:
            continue
        out[key] = abs(float(pos.qty))
    return out


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


def assert_positions_match(
    client: AlpacaClient,
    book: OpenPositionBook | None = None,
    notifier: TelegramNotifier | None = None,
) -> None:
    """
    Aborta el arranque si el libro y el broker no coinciden,
    o si no se pueden leer las posiciones.
    """
    book = book or OpenPositionBook()
    try:
        broker = broker_qty_map(client)
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

    local = local_qty_map(book)
    mismatch = diff_maps(broker, local)
    if mismatch.only_broker or mismatch.only_local or mismatch.qty_diff:
        message = format_mismatch(mismatch)
        logger.error("%s", message)
        audit(
            "reconcile",
            "deny",
            reason="position_mismatch",
            only_broker=",".join(mismatch.only_broker) or "-",
            only_local=",".join(mismatch.only_local) or "-",
        )
        if notifier is not None:
            notifier.notify_position_mismatch(message)
        raise ValidationError(message)

    logger.info(
        "Reconcile OK | broker=%s | libro=%s",
        ",".join(sorted(broker)) or "—",
        ",".join(sorted(local)) or "—",
    )
    audit("reconcile", "allow", symbols=",".join(sorted(broker)) or "none")
