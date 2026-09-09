"""Envío y consulta de órdenes (+ libro local en dry-run)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from bot.alpaca.client import AlpacaClient
from bot.config import PROJECT_ROOT
from bot.market.assets import is_crypto_symbol
from bot.notify.telegram import TelegramNotifier
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, ValidationError
from bot.security.sanitize import sanitize_qty, sanitize_symbol
from bot.storage.journal import TradeJournal
from bot.storage.positions import OpenPositionBook, TrackedPosition

logger = logging.getLogger(__name__)

LIVE_CONFIRM_PATH = PROJECT_ROOT / "data" / "live_confirm.txt"
_CONFIRM_WORD = "CONFIRMO"


def _read_live_confirm_file(path: Path) -> tuple[str, str]:
    """Línea 1 = CONFIRMO, línea 2 = account id live."""
    if not path.is_file():
        return "", ""
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", ""
    word = raw_lines[0].strip() if raw_lines else ""
    account = raw_lines[1].strip() if len(raw_lines) > 1 else ""
    return word, account


def _as_broker_position(tracked: TrackedPosition) -> SimpleNamespace:
    """Compatibilidad con el motor (usa .symbol / .qty / .avg_entry_price)."""
    return SimpleNamespace(
        symbol=tracked.symbol,
        qty=str(tracked.qty),
        avg_entry_price=str(tracked.avg_entry_price),
        current_price=str(tracked.current_price or tracked.avg_entry_price),
    )


class OrderExecutor:
    def __init__(
        self,
        client: AlpacaClient,
        dry_run: bool = True,
        journal: TradeJournal | None = None,
        position_book: OpenPositionBook | None = None,
        notifier: TelegramNotifier | None = None,
        live_account_id: str = "",
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.journal = journal or TradeJournal()
        # Libro local: fuente de verdad en dry-run; en live guarda SL/TP de alerta
        self.position_book = position_book or OpenPositionBook()
        self.notifier = notifier
        self.live_account_id = str(live_account_id or "").strip()
        self._live_order_confirmed = False

    def get_position(self, symbol: str) -> Position | SimpleNamespace | None:
        symbol = sanitize_symbol(symbol)
        if self.dry_run:
            tracked = self.position_book.get(symbol)
            return _as_broker_position(tracked) if tracked else None
        try:
            self.client.limiter.acquire("trading_read")
            return self.client.trading.get_open_position(symbol)
        except ValidationError:
            raise
        except Exception:
            return None

    def list_positions(self) -> list[Position | SimpleNamespace]:
        if self.dry_run:
            return [_as_broker_position(p) for p in self.position_book.list()]
        self.client.limiter.acquire("trading_read")
        return list(self.client.trading.get_all_positions())

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        if self.dry_run:
            return []
        safe_symbol = sanitize_symbol(symbol) if symbol else None
        self.client.limiter.acquire("trading_read")
        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[safe_symbol] if safe_symbol else None,
        )
        return list(self.client.trading.get_orders(filter=request))

    def submit_market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        time_in_force: TimeInForce = TimeInForce.DAY,
        price: float | None = None,
        entry_price: float | None = None,
        reason: str = "signal",
        *,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
        stop_pct: float | None = None,
        take_profit_pct: float | None = None,
    ) -> Order | dict[str, Any]:
        symbol = sanitize_symbol(symbol)
        qty = sanitize_qty(qty, fractional=is_crypto_symbol(symbol))
        tif = TimeInForce.GTC if is_crypto_symbol(symbol) else time_in_force
        if side not in (OrderSide.BUY, OrderSide.SELL):
            raise ValidationError("Lado de orden no permitido")

        try:
            self.client.limiter.acquire("order")
        except RateLimitError:
            audit("order_submit", "deny", symbol=symbol, reason="rate_limit", dry_run=self.dry_run)
            raise

        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side.value,
            "type": "market",
            "time_in_force": tif.value,
        }

        if self.dry_run:
            logger.info("DRY_RUN | orden no enviada | %s", payload)
            audit("order_submit", "dry_run", symbol=symbol, qty=qty, side=side.value)
            fill_price = float(price or 0.0)
            if fill_price > 0:
                self.journal.record(
                    symbol,
                    side.value,
                    qty,
                    fill_price,
                    entry_price=entry_price,
                    reason=reason,
                    dry_run=True,
                    order_id="dry_run",
                )
            # Ciclo de vida local: registra compra / cierra venta en el libro
            if side is OrderSide.BUY and fill_price > 0:
                self.position_book.open(
                    symbol,
                    qty,
                    fill_price,
                    stop_price=float(stop_price or fill_price * 0.99),
                    take_profit_price=float(take_profit_price or fill_price * 1.015),
                    stop_pct=float(stop_pct or 0.01),
                    take_profit_pct=float(take_profit_pct or 0.015),
                    dry_run=True,
                )
            elif side is OrderSide.SELL:
                self.position_book.close(symbol)
            return {"dry_run": True, **payload}

        self._require_live_confirm(symbol, qty, side)

        try:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=tif,
            )
            order = self.client.trading.submit_order(order_data=request)
        except Exception as exc:
            log_caught(logger, "order_submit_failed", exc, symbol=symbol, side=side.value)
            audit("order_submit", "deny", symbol=symbol, qty=qty, side=side.value, reason="broker_error")
            raise RuntimeError("No se pudo enviar la orden.") from None

        fill_price = float(getattr(order, "filled_avg_price", None) or price or 0.0)
        self.journal.record(
            symbol,
            side.value,
            qty,
            fill_price if fill_price > 0 else float(price or 0.0),
            entry_price=entry_price,
            reason=reason,
            dry_run=False,
            order_id=str(order.id),
        )
        if side is OrderSide.BUY and fill_price > 0:
            self.position_book.open(
                symbol,
                qty,
                fill_price,
                stop_price=float(stop_price or fill_price * 0.99),
                take_profit_price=float(take_profit_price or fill_price * 1.015),
                stop_pct=float(stop_pct or 0.01),
                take_profit_pct=float(take_profit_pct or 0.015),
                dry_run=False,
            )
        elif side is OrderSide.SELL:
            self.position_book.close(symbol)
        logger.info(
            "Orden enviada | id=%s | %s %s qty=%s | status=%s",
            order.id,
            side.value,
            symbol,
            qty,
            order.status,
        )
        audit("order_submit", "allow", symbol=symbol, qty=qty, side=side.value, order_id=str(order.id))
        return order

    def _require_live_confirm(self, symbol: str, qty: float, side: OrderSide) -> None:
        """Primera orden live (no paper, no dry-run): CONFIRMO en TTY o data/live_confirm.txt."""
        if self.dry_run or self.client.settings.paper:
            return
        if self._live_order_confirmed:
            return

        account_id = self.live_account_id
        if not account_id:
            account_id = self._fetch_live_account_id()
            self.live_account_id = account_id

        tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        if tty:
            self._confirm_live_via_tty(symbol, qty, side, account_id)
        else:
            self._confirm_live_via_file(symbol, qty, side, account_id)
        self._live_order_confirmed = True
        logger.warning(
            "LIVE CONFIRMADO | primera orden permitida | %s %s qty=%s | account=%s",
            side.value,
            symbol,
            qty,
            account_id,
        )
        audit("live_confirm", "allow", symbol=symbol, side=side.value, account_id=account_id)

    def _fetch_live_account_id(self) -> str:
        try:
            self.client.limiter.acquire("trading_read")
            account = self.client.trading.get_account()
            return str(getattr(account, "id", "") or "").strip()
        except Exception as exc:
            log_caught(logger, "live_account_id_failed", exc)
            return ""

    def _confirm_live_via_tty(
        self, symbol: str, qty: float, side: OrderSide, account_id: str
    ) -> None:
        warning = (
            "\n*** ORDEN LIVE CON DINERO REAL ***\n"
            f"Cuenta: {account_id or '(id no leido)'}\n"
            f"{side.value.upper()} {symbol} qty={qty:g}\n"
            "Escribe CONFIRMO para enviar esta orden a api.alpaca.markets.\n"
            "Cualquier otra cosa cancela la orden.\n"
        )
        sys.stderr.write(warning)
        sys.stderr.flush()
        try:
            typed = input("CONFIRMO> ").strip()
        except EOFError:
            typed = ""
        if typed != _CONFIRM_WORD:
            self._block_live_order(symbol, qty, side, "No se escribio CONFIRMO en consola")
        self._write_live_confirm_file(account_id)

    def _confirm_live_via_file(
        self, symbol: str, qty: float, side: OrderSide, account_id: str
    ) -> None:
        word, file_account = _read_live_confirm_file(LIVE_CONFIRM_PATH)
        if word != _CONFIRM_WORD:
            self._block_live_order(
                symbol,
                qty,
                side,
                f"Falta {_CONFIRM_WORD} en {LIVE_CONFIRM_PATH} (linea 1)",
            )
        if not account_id:
            self._block_live_order(symbol, qty, side, "No se pudo leer el account id live")
        if file_account != account_id:
            self._block_live_order(
                symbol,
                qty,
                side,
                "live_confirm.txt no coincide con el account id live actual "
                f"(archivo={file_account or 'vacio'} cuenta={account_id})",
            )

    def _write_live_confirm_file(self, account_id: str) -> None:
        if not account_id:
            return
        LIVE_CONFIRM_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIVE_CONFIRM_PATH.write_text(
            f"{_CONFIRM_WORD}\n{account_id}\n",
            encoding="utf-8",
        )

    def _block_live_order(self, symbol: str, qty: float, side: OrderSide, reason: str) -> None:
        logger.error("LIVE BLOQUEADO | %s %s qty=%s | %s", side.value, symbol, qty, reason)
        audit("live_confirm", "deny", symbol=symbol, side=side.value, reason=reason)
        if self.notifier is not None:
            self.notifier.notify_live_order_blocked(symbol, side.value, qty, reason)
        raise ValidationError(f"Orden live bloqueada: {reason}")

    def close_position(
        self,
        symbol: str,
        price: float | None = None,
        reason: str = "close",
    ) -> Order | dict[str, Any] | None:
        symbol = sanitize_symbol(symbol)
        position = self.get_position(symbol)
        if position is None:
            logger.info("No hay posicion abierta en %s", symbol)
            audit("order_close", "deny", symbol=symbol, reason="no_position")
            return None

        qty = sanitize_qty(abs(float(position.qty)), fractional=is_crypto_symbol(symbol))
        side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
        entry = float(position.avg_entry_price)
        logger.info("Cerrando posicion %s qty=%s side=%s motivo=%s", symbol, qty, side.value, reason)
        audit("order_close", "allow", symbol=symbol, qty=qty, side=side.value, reason=reason)
        return self.submit_market_order(
            symbol,
            qty,
            side,
            price=price,
            entry_price=entry,
            reason=reason,
        )

    def cancel_open_orders(self, symbol: str | None = None) -> int:
        """Cancela órdenes abiertas en Alpaca. Se usa al detener el loop."""
        if self.dry_run:
            return 0
        try:
            orders = self.list_open_orders(symbol)
        except Exception as exc:
            log_caught(logger, "order_list_failed", exc, symbol=symbol or "*")
            return 0

        cancelled = 0
        for order in orders:
            order_id = str(getattr(order, "id", "") or "")
            order_symbol = str(getattr(order, "symbol", symbol or ""))
            if not order_id:
                continue
            try:
                self.client.limiter.acquire("order")
                self.client.trading.cancel_order_by_id(order_id)
                cancelled += 1
                logger.info("Orden pendiente cancelada | id=%s | %s", order_id, order_symbol)
                audit("order_cancel", "allow", symbol=order_symbol, order_id=order_id)
            except RateLimitError:
                audit("order_cancel", "deny", symbol=order_symbol, reason="rate_limit")
                raise
            except Exception as exc:
                log_caught(logger, "order_cancel_failed", exc, symbol=order_symbol)
                audit("order_cancel", "deny", symbol=order_symbol, reason="broker_error")
        return cancelled
