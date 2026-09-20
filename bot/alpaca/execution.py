"""Envío y consulta de órdenes (+ libro local en dry-run)."""

from __future__ import annotations

import logging
import math
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from bot.alpaca.client import AlpacaClient
from bot.config import PROJECT_ROOT
from bot.market.assets import is_crypto_symbol, normalize_symbol
from bot.market.dust import dust_threshold_for
from bot.notify.telegram import TelegramNotifier
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, ValidationError
from bot.security.sanitize import sanitize_symbol
from bot.security.secrets import redact_text
from bot.storage.journal import TradeJournal
from bot.storage.pending_orders import PendingOrderBook
from bot.storage.positions import OpenPositionBook, TrackedPosition

logger = logging.getLogger(__name__)

LIVE_CONFIRM_PATH = PROJECT_ROOT / "data" / "live_confirm.txt"
_CONFIRM_WORD = "CONFIRMO"


@dataclass
class OrderSubmitResult:
    accepted: bool
    order: Order | dict[str, Any] | None = None
    order_type: str = "market"
    limit_price: float | None = None
    spread_pct: float | None = None
    broker_detail: str = ""
    filled_qty: float = 0.0
    fill_price: float = 0.0
    wide_spread_market: bool = False
    filled: bool = False
    resting: bool = False
    order_id: str = ""
    submitted_qty: float = 0.0
    liquidity_role: str = ""
    fee_pct_estimate: float | None = None


@dataclass(frozen=True)
class AssetRules:
    symbol: str
    tradable: bool | None = None
    fractionable: bool | None = None
    min_order_size: float | None = None
    min_trade_increment: float | None = None
    price_increment: float | None = None
    asset_class: str = ""
    status: str = ""


def _asset_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _asset_decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def _floor_to_increment(value: float, increment: float | None) -> float:
    if value <= 0:
        return 0.0
    raw = _asset_decimal(value)
    step = _asset_decimal(increment)
    if raw is None:
        return 0.0
    if step is None:
        return float(raw)
    floored = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
    return float(floored)


def _floor_decimal_to_increment(value: Decimal, increment: float | str | Decimal | None) -> Decimal:
    if value <= 0:
        return Decimal("0")
    step = _asset_decimal(increment)
    if step is None:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _floor_decimal(value: Decimal, decimals: int = 6) -> Decimal:
    quant = Decimal(1).scaleb(-int(decimals))
    return value.quantize(quant, rounding=ROUND_DOWN)


def _retry_qty_step(rules: AssetRules | None) -> Decimal:
    step = _asset_decimal(getattr(rules, "min_trade_increment", None) if rules else None)
    return step if step is not None else Decimal("0.000001")


def fetch_asset_rules(client: AlpacaClient, symbol: str) -> AssetRules:
    normalized = normalize_symbol(sanitize_symbol(symbol))
    candidates = [normalized]
    compact = normalized.replace("/", "")
    if compact not in candidates:
        candidates.append(compact)
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            client.limiter.acquire("trading_read")
            asset = client.trading.get_asset(candidate)
            return AssetRules(
                symbol=normalized,
                tradable=getattr(asset, "tradable", None),
                fractionable=getattr(asset, "fractionable", None),
                min_order_size=_asset_float(getattr(asset, "min_order_size", None)),
                min_trade_increment=_asset_float(getattr(asset, "min_trade_increment", None)),
                price_increment=_asset_float(getattr(asset, "price_increment", None)),
                asset_class=str(getattr(asset, "asset_class", "") or ""),
                status=str(getattr(asset, "status", "") or ""),
            )
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        logger.debug("%s | metadata de activo no disponible: %s", normalized, type(last_exc).__name__)
    return AssetRules(symbol=normalized)


def normalize_limit_price_for_asset(price: float, symbol: str, rules: AssetRules | None = None) -> float:
    px = _round_limit_price(price, symbol)
    active_rules = rules or AssetRules(symbol=normalize_symbol(symbol))
    if active_rules.price_increment:
        px = _floor_to_increment(px, active_rules.price_increment)
    return _round_limit_price(px, symbol) if px > 0 else 0.0


def normalize_order_qty_for_asset(
    qty: float | str,
    *,
    symbol: str,
    side: OrderSide,
    rules: AssetRules | None,
    fractional: bool,
    sellable_qty: float | str | None = None,
) -> float:
    active_rules = rules or AssetRules(symbol=normalize_symbol(symbol))
    safe_qty_dec = _asset_decimal(qty)
    if safe_qty_dec is None:
        raise ValidationError("Cantidad de orden invalida")
    if sellable_qty is not None:
        sellable_cap = _asset_decimal(sellable_qty)
        if sellable_cap is not None:
            safe_qty_dec = min(safe_qty_dec, sellable_cap)
    if fractional:
        if active_rules.min_trade_increment:
            safe_qty_dec = _floor_decimal_to_increment(safe_qty_dec, active_rules.min_trade_increment)
        else:
            safe_qty_dec = _floor_decimal(safe_qty_dec, decimals=6)
        if safe_qty_dec <= 0:
            raise ValidationError("Cantidad de orden invalida tras aplicar min_trade_increment")
        safe_qty = float(safe_qty_dec)
    else:
        safe_qty = float(safe_qty_dec)
        if active_rules.fractionable is False and abs(safe_qty - round(safe_qty)) > 1e-9:
            raise ValidationError(f"{symbol} no admite cantidades fraccionarias")
        safe_qty = float(math.floor(float(safe_qty))) if abs(safe_qty - round(safe_qty)) > 1e-9 else float(safe_qty)
    min_qty = active_rules.min_order_size
    if min_qty is not None and safe_qty + 1e-12 < min_qty:
        raise ValidationError(
            f"{symbol} qty={safe_qty:g} por debajo de min_order_size={min_qty:g}"
        )
    if safe_qty <= 0:
        raise ValidationError("Cantidad de orden invalida")
    return float(safe_qty)


def alpaca_reject_detail(exc: BaseException) -> str:
    """Motivo exacto de Alpaca (sin secretos) para logs y Telegram."""
    if isinstance(exc, APIError):
        try:
            return redact_text(
                f"Alpaca HTTP {exc.status_code} code={exc.code} {exc.message}"
            )[:400]
        except Exception:
            raw = getattr(exc, "_error", None) or str(exc)
            return redact_text(str(raw))[:400]
    if isinstance(exc, RateLimitError):
        return redact_text(str(exc) or "rate_limit")[:400]
    return redact_text(str(exc) or type(exc).__name__)[:400]


def is_insufficient_balance_reject(detail: str) -> bool:
    """Alpaca 40301000 / insufficient balance por polvo de decimales en crypto."""
    text = (detail or "").lower()
    return any(
        token in text
        for token in (
            "insufficient balance",
            "insufficient qty",
            "insufficient quantity",
            "40301000",
            "not enough",
        )
    )


def _position_field_raw(position: Any, *names: str) -> str:
    for name in names:
        value = getattr(position, name, None)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def sellable_qty_raw_from_position(position: Any) -> str:
    """String crudo de qty vendible (qty_available preferido)."""
    return _position_field_raw(position, "qty_available", "available", "qty")


def sellable_qty_from_position(position: Any) -> float:
    """
    Qty vendible según el broker: prefiere qty_available; si no, qty.

    Usa el string crudo del API para no perder decimales por float.
    """
    raw = sellable_qty_raw_from_position(position)
    if not raw:
        return 0.0
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return 0.0


def _round_limit_price(price: float, symbol: str) -> float:
    if price <= 0:
        return 0.0
    if is_crypto_symbol(symbol):
        if price >= 1000:
            return round(price, 2)
        if price >= 1:
            return round(price, 4)
        return round(price, 6)
    if price >= 1:
        return round(price, 2)
    return round(price, 4)


def choose_limit_price(
    side: OrderSide,
    last_price: float,
    bid: float | None,
    ask: float | None,
    tolerance_pct: float,
    symbol: str,
) -> float:
    last = float(last_price or 0.0)
    if last <= 0 and bid and ask:
        last = (float(bid) + float(ask)) / 2.0
    if last <= 0:
        return 0.0
    if side is OrderSide.BUY:
        cap = last * (1.0 + tolerance_pct)
        px = min(float(ask), cap) if ask and ask > 0 else cap
        if bid and px < float(bid):
            px = float(bid)
    else:
        floor = last * (1.0 - tolerance_pct)
        px = max(float(bid), floor) if bid and bid > 0 else floor
        if ask and px > float(ask):
            px = float(ask)
    return _round_limit_price(px, symbol)


def _order_status_name(order: Any) -> str:
    status = getattr(order, "status", "") or ""
    text = str(status)
    if "." in text:
        text = text.split(".")[-1]
    return text.lower()


def _order_remaining(order: Any) -> float:
    qty = float(getattr(order, "qty", 0) or 0)
    filled = float(getattr(order, "filled_qty", 0) or 0)
    return max(0.0, qty - filled)


def time_in_force_for(
    symbol: str,
    *,
    use_limit: bool,
    stock_tif: TimeInForce = TimeInForce.DAY,
) -> TimeInForce:
    """Cripto: GTC (market) o IOC (limit). Acciones: DAY (market) o IOC (limit)."""
    if is_crypto_symbol(symbol):
        return TimeInForce.IOC if use_limit else TimeInForce.GTC
    return TimeInForce.IOC if use_limit else stock_tif


def classify_order(order: Any) -> str:
    """filled | partial | resting | failed."""
    status = _order_status_name(order)
    filled = float(getattr(order, "filled_qty", 0) or 0)
    remaining = _order_remaining(order)
    rejected = str(
        getattr(order, "rejected_reason", None)
        or getattr(order, "cancel_reason", None)
        or ""
    ).strip()
    if "reject" in status:
        return "failed"
    terminal_dead = any(token in status for token in ("cancel", "expir", "done_for_day"))
    if terminal_dead:
        if remaining <= 1e-9 and filled > 0:
            return "filled"
        if filled > 0:
            return "partial"
        return "failed"
    if status == "filled" or (filled > 0 and remaining <= 1e-9):
        return "filled"
    if filled > 0:
        return "partial"
    if status in {
        "new",
        "accepted",
        "pending_new",
        "pending_replace",
        "held",
        "replaced",
        "partially_filled",
        "pending_cancel",
    }:
        return "resting"
    if rejected:
        return "failed"
    return "resting"


def _order_was_accepted(order: Any) -> tuple[bool, str]:
    status = _order_status_name(order)
    filled = float(getattr(order, "filled_qty", 0) or 0)
    kind = classify_order(order)
    if kind == "failed":
        rejected = str(
            getattr(order, "rejected_reason", None)
            or getattr(order, "cancel_reason", None)
            or ""
        ).strip()
        return False, rejected or f"status={status} (sin fill)"
    if kind == "filled":
        return True, f"status={status} filled_qty={filled:g}"
    if kind == "partial":
        return True, f"status={status} filled_qty={filled:g} (parcial)"
    return True, f"status={status}"


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
        pending_fills: PendingOrderBook | None = None,
        notifier: TelegramNotifier | None = None,
        live_account_id: str = "",
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self._crypto_maker_config: Any | None = None
        self.journal = journal or TradeJournal()
        # Libro local: fuente de verdad en dry-run; en live guarda SL/TP de alerta
        self.position_book = position_book or OpenPositionBook()
        self.pending_fills = pending_fills or PendingOrderBook()
        self.notifier = notifier
        self.live_account_id = str(live_account_id or "").strip()
        self._live_order_confirmed = False
        self._asset_rules_cache: dict[str, AssetRules] = {}

    def asset_rules(self, symbol: str, *, force_refresh: bool = False) -> AssetRules:
        key = normalize_symbol(sanitize_symbol(symbol))
        if not force_refresh and key in self._asset_rules_cache:
            return self._asset_rules_cache[key]
        rules = fetch_asset_rules(self.client, key)
        self._asset_rules_cache[key] = rules
        return rules

    def get_position(self, symbol: str) -> Position | SimpleNamespace | None:
        symbol = normalize_symbol(sanitize_symbol(symbol))
        if self.dry_run:
            tracked = self.position_book.get(symbol)
            return _as_broker_position(tracked) if tracked else None
        try:
            self.client.limiter.acquire("trading_read")
            return self.client.trading.get_open_position(symbol)
        except ValidationError:
            raise
        except Exception:
            compact = symbol.replace("/", "")
            if compact != symbol:
                try:
                    self.client.limiter.acquire("trading_read")
                    return self.client.trading.get_open_position(compact)
                except Exception:
                    pass
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

    def get_order(self, order_id: str) -> Order | None:
        if self.dry_run or not order_id:
            return None
        self.client.limiter.acquire("trading_read")
        return self.client.trading.get_order_by_id(order_id)

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
        result = self.submit_smart_order(
            symbol,
            qty,
            side,
            time_in_force=time_in_force,
            price=price,
            entry_price=entry_price,
            reason=reason,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            stop_pct=stop_pct,
            take_profit_pct=take_profit_pct,
        )
        if not result.accepted:
            raise RuntimeError(result.broker_detail or "No se pudo enviar la orden.")
        return result.order if result.order is not None else {"dry_run": True}

    def submit_smart_order(
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
        bid: float | None = None,
        ask: float | None = None,
        spread_pct: float | None = None,
        limit_spread_pct: float = 0.0015,
        attempt: int = 1,
        force_market: bool = False,
    ) -> OrderSubmitResult:
        symbol = normalize_symbol(sanitize_symbol(symbol))
        fractional = is_crypto_symbol(symbol)
        rules = self.asset_rules(symbol)
        if side not in (OrderSide.BUY, OrderSide.SELL):
            raise ValidationError("Lado de orden no permitido")
        if rules.tradable is False:
            raise ValidationError(f"{symbol} no está tradable en Alpaca")

        sellable_qty: float | None = None
        if side is OrderSide.SELL and not self.dry_run:
            broker_position = self.get_position(symbol)
            if broker_position is None:
                raise ValidationError(f"No hay posición abierta en {symbol}")
            sellable_qty = sellable_qty_from_position(broker_position)
            if sellable_qty <= 0:
                raise ValidationError(f"No hay qty vendible en {symbol}")

        qty = normalize_order_qty_for_asset(
            qty,
            symbol=symbol,
            side=side,
            rules=rules,
            fractional=fractional,
            sellable_qty=sellable_qty,
        )

        last_price = float(price or 0.0)
        use_limit = False
        limit_price: float | None = None
        wide_spread_market = False
        if force_market:
            logger.info(
                "%s | %s de emergencia — market (sin limit)",
                symbol,
                reason,
            )
        elif spread_pct is not None and spread_pct > float(limit_spread_pct):
            fractional_stock = (not is_crypto_symbol(symbol)) and abs(qty - round(qty)) > 1e-9
            if fractional_stock:
                wide_spread_market = True
                logger.info(
                    "%s | spread %.4f%% > límite %.4f%% pero qty fraccionaria en acciones "
                    "no admite limit — se envía market",
                    symbol,
                    spread_pct * 100.0,
                    limit_spread_pct * 100.0,
                )
            else:
                limit_price = choose_limit_price(
                    side, last_price, bid, ask, limit_spread_pct, symbol
                )
                use_limit = bool(limit_price and limit_price > 0)

        if use_limit and limit_price is not None:
            limit_price = normalize_limit_price_for_asset(limit_price, symbol, rules)
            use_limit = bool(limit_price and limit_price > 0)

        order_type = "limit" if use_limit else "market"
        tif = time_in_force_for(symbol, use_limit=use_limit, stock_tif=time_in_force)

        try:
            self.client.limiter.acquire("order")
        except RateLimitError as exc:
            detail = alpaca_reject_detail(exc)
            logger.warning(
                "%s | orden intento %s | %s qty=%s type=%s spread=%s | RECHAZADA | %s",
                symbol,
                attempt,
                side.value,
                qty,
                order_type,
                f"{spread_pct:.4%}" if spread_pct is not None else "n/a",
                detail,
            )
            audit("order_submit", "deny", symbol=symbol, reason="rate_limit", dry_run=self.dry_run)
            return OrderSubmitResult(
                accepted=False,
                order_type=order_type,
                limit_price=limit_price,
                spread_pct=spread_pct,
                broker_detail=detail,
                wide_spread_market=wide_spread_market,
            )

        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side.value,
            "type": order_type,
            "time_in_force": tif.value,
        }
        if use_limit:
            payload["limit_price"] = limit_price

        if self.dry_run:
            logger.info(
                "%s | orden intento %s | DRY_RUN %s qty=%s type=%s spread=%s limit=%s | aceptada",
                symbol,
                attempt,
                side.value,
                qty,
                order_type,
                f"{spread_pct:.4%}" if spread_pct is not None else "n/a",
                f"{limit_price:.4f}" if limit_price else "—",
            )
            audit("order_submit", "dry_run", symbol=symbol, qty=qty, side=side.value)
            self._record_fill(
                symbol,
                qty,
                side,
                last_price,
                entry_price=entry_price,
                reason=reason,
                dry_run=True,
                order_id="dry_run",
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                stop_pct=stop_pct,
                take_profit_pct=take_profit_pct,
            )
            return OrderSubmitResult(
                accepted=True,
                order={"dry_run": True, **payload},
                order_type=order_type,
                limit_price=limit_price,
                spread_pct=spread_pct,
                broker_detail="dry_run",
                filled_qty=qty,
                fill_price=last_price,
                wide_spread_market=wide_spread_market,
                filled=True,
                resting=False,
                order_id="dry_run",
            )

        self._require_live_confirm(symbol, qty, side)

        try:
            order = self._broker_submit(symbol, qty, side, tif, use_limit, limit_price)
        except ValidationError:
            raise
        except Exception as exc:
            detail = alpaca_reject_detail(exc)
            logger.warning(
                "%s | orden intento %s | %s qty=%s type=%s spread=%s limit=%s | RECHAZADA | %s",
                symbol,
                attempt,
                side.value,
                qty,
                order_type,
                f"{spread_pct:.4%}" if spread_pct is not None else "n/a",
                f"{limit_price:.4f}" if limit_price else "—",
                detail,
            )
            audit(
                "order_submit",
                "deny",
                symbol=symbol,
                qty=qty,
                side=side.value,
                reason=detail[:180],
            )
            return OrderSubmitResult(
                accepted=False,
                order_type=order_type,
                limit_price=limit_price,
                spread_pct=spread_pct,
                broker_detail=detail,
                wide_spread_market=wide_spread_market,
            )

        ok, status_detail = _order_was_accepted(order)
        fill_price = float(getattr(order, "filled_avg_price", None) or last_price or 0.0)
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        order_id = str(getattr(order, "id", "") or "")
        kind = classify_order(order)
        if ok:
            logger.info(
                "%s | orden intento %s | %s qty=%s type=%s spread=%s limit=%s | "
                "aceptada id=%s | %s",
                symbol,
                attempt,
                side.value,
                qty,
                order_type,
                f"{spread_pct:.4%}" if spread_pct is not None else "n/a",
                f"{limit_price:.4f}" if limit_price else "—",
                order_id or "—",
                status_detail,
            )
            if filled_qty > 0:
                self._record_fill(
                    symbol,
                    filled_qty,
                    side,
                    fill_price,
                    entry_price=entry_price,
                    reason=reason,
                    dry_run=False,
                    order_id=order_id,
                    stop_price=stop_price,
                    take_profit_price=take_profit_price,
                    stop_pct=stop_pct,
                    take_profit_pct=take_profit_pct,
                )
            elif kind == "resting":
                logger.info(
                    "%s | orden en vuelo (sin fill) | id=%s | libro intacto",
                    symbol,
                    order_id or "—",
                )
            audit(
                "order_submit",
                "allow",
                symbol=symbol,
                qty=qty,
                side=side.value,
                order_id=order_id,
            )
            return OrderSubmitResult(
                accepted=True,
                order=order,
                order_type=order_type,
                limit_price=limit_price,
                spread_pct=spread_pct,
                broker_detail=status_detail,
                filled_qty=filled_qty,
                fill_price=fill_price if filled_qty > 0 else 0.0,
                wide_spread_market=wide_spread_market,
                filled=kind == "filled",
                resting=kind in {"resting", "partial"},
                order_id=order_id,
                submitted_qty=float(qty),
            )

        logger.warning(
            "%s | orden intento %s | %s qty=%s type=%s spread=%s limit=%s | RECHAZADA | %s",
            symbol,
            attempt,
            side.value,
            qty,
            order_type,
            f"{spread_pct:.4%}" if spread_pct is not None else "n/a",
            f"{limit_price:.4f}" if limit_price else "—",
            status_detail,
        )
        audit("order_submit", "deny", symbol=symbol, qty=qty, side=side.value, reason=status_detail[:180])
        return OrderSubmitResult(
            accepted=False,
            order=order,
            order_type=order_type,
            limit_price=limit_price,
            spread_pct=spread_pct,
            broker_detail=status_detail,
            filled_qty=filled_qty,
            wide_spread_market=wide_spread_market,
        )

    def submit_crypto_maker_first_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        *,
        price: float | None = None,
        entry_price: float | None = None,
        reason: str = "signal",
        stop_price: float | None = None,
        take_profit_price: float | None = None,
        stop_pct: float | None = None,
        take_profit_pct: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        spread_pct: float | None = None,
        limit_spread_pct: float = 0.0015,
        attempt: int = 1,
        maker_config: Any | None = None,
        urgent_fallback: bool = False,
    ) -> OrderSubmitResult:
        """Cripto: limit GTC en bid/ask (maker), poll, fallback configurable. Acciones: smart order."""
        from bot.alpaca.crypto_maker import (
            CryptoMakerConfig,
            choose_maker_limit_price,
            estimate_liquidity_role,
            estimated_fee_pct,
            log_crypto_execution,
        )

        cfg = maker_config if maker_config is not None else CryptoMakerConfig()
        if not cfg.enabled or not is_crypto_symbol(symbol):
            return self.submit_smart_order(
                symbol,
                qty,
                side,
                price=price,
                entry_price=entry_price,
                reason=reason,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                stop_pct=stop_pct,
                take_profit_pct=take_profit_pct,
                bid=bid,
                ask=ask,
                spread_pct=spread_pct,
                limit_spread_pct=limit_spread_pct,
                attempt=attempt,
                force_market=urgent_fallback,
            )

        symbol = normalize_symbol(sanitize_symbol(symbol))
        rules = self.asset_rules(symbol)
        last_price = float(price or 0.0)
        retries = max(0, int(cfg.max_retries))
        tick_inside = int(cfg.tick_inside)

        for maker_attempt in range(retries + 1):
            limit_price = choose_maker_limit_price(
                side,
                bid,
                ask,
                symbol,
                rules,
                tick_inside=tick_inside + maker_attempt,
            )
            if limit_price <= 0:
                logger.warning("%s | maker-first sin precio válido — fallback smart", symbol)
                break

            role_hint = estimate_liquidity_role(side, limit_price, bid, ask)
            fee_est = estimated_fee_pct(role_hint, cfg)

            if self.dry_run:
                log_crypto_execution(
                    symbol,
                    side,
                    order_id="dry_run",
                    limit_price=limit_price,
                    fill_price=limit_price,
                    filled_qty=qty,
                    role=role_hint,
                    fee_pct=fee_est,
                    tif=TimeInForce.GTC.value,
                    attempt=attempt,
                    reason=reason,
                )
                self._record_fill(
                    symbol,
                    qty,
                    side,
                    limit_price,
                    entry_price=entry_price,
                    reason=reason,
                    dry_run=True,
                    order_id="dry_run_maker",
                    stop_price=stop_price,
                    take_profit_price=take_profit_price,
                    stop_pct=stop_pct,
                    take_profit_pct=take_profit_pct,
                )
                return OrderSubmitResult(
                    accepted=True,
                    order={"dry_run": True, "type": "limit", "tif": "gtc"},
                    order_type="limit",
                    limit_price=limit_price,
                    spread_pct=spread_pct,
                    broker_detail="dry_run_maker",
                    filled_qty=qty,
                    fill_price=limit_price,
                    filled=True,
                    resting=False,
                    order_id="dry_run_maker",
                    submitted_qty=float(qty),
                    liquidity_role=role_hint,
                    fee_pct_estimate=fee_est,
                )

            self._require_live_confirm(symbol, qty, side)
            try:
                order = self._broker_submit(
                    symbol,
                    qty,
                    side,
                    TimeInForce.GTC,
                    True,
                    limit_price,
                )
            except Exception as exc:
                detail = alpaca_reject_detail(exc)
                logger.warning("%s | maker-first rechazada | %s", symbol, detail)
                break

            ok, status_detail = _order_was_accepted(order)
            order_id = str(getattr(order, "id", "") or "")
            if not ok:
                return OrderSubmitResult(
                    accepted=False,
                    order_type="limit",
                    limit_price=limit_price,
                    spread_pct=spread_pct,
                    broker_detail=status_detail,
                    liquidity_role=role_hint,
                    fee_pct_estimate=fee_est,
                )

            deadline = time.monotonic() + float(cfg.timeout_seconds)
            final_order = order
            while time.monotonic() < deadline:
                kind = classify_order(final_order)
                if kind == "filled":
                    break
                if kind == "failed":
                    break
                time.sleep(max(0.5, float(cfg.poll_interval_seconds)))
                refreshed = self.get_order(order_id)
                if refreshed is not None:
                    final_order = refreshed

            kind = classify_order(final_order)
            filled_qty = float(getattr(final_order, "filled_qty", 0) or 0)
            fill_price = float(getattr(final_order, "filled_avg_price", None) or limit_price or last_price)
            remaining = _order_remaining(final_order)

            if kind != "filled" and remaining > 0:
                try:
                    self.client.limiter.acquire("order")
                    self.client.trading.cancel_order_by_id(order_id)
                    logger.info("%s | maker-first timeout — cancel id=%s filled=%s", symbol, order_id, filled_qty)
                except Exception as exc:
                    log_caught(logger, "maker_cancel_failed", exc, symbol=symbol)

            if filled_qty > 0 and (kind == "filled" or remaining <= 1e-9):
                role = estimate_liquidity_role(side, limit_price, bid, ask)
                fee_est = estimated_fee_pct(role, cfg)
                log_crypto_execution(
                    symbol,
                    side,
                    order_id=order_id,
                    limit_price=limit_price,
                    fill_price=fill_price,
                    filled_qty=filled_qty,
                    role=role,
                    fee_pct=fee_est,
                    tif=TimeInForce.GTC.value,
                    attempt=attempt,
                    reason=reason,
                )
                self._record_fill(
                    symbol,
                    filled_qty,
                    side,
                    fill_price,
                    entry_price=entry_price,
                    reason=reason,
                    dry_run=False,
                    order_id=order_id,
                    stop_price=stop_price,
                    take_profit_price=take_profit_price,
                    stop_pct=stop_pct,
                    take_profit_pct=take_profit_pct,
                )
                return OrderSubmitResult(
                    accepted=True,
                    order=final_order,
                    order_type="limit",
                    limit_price=limit_price,
                    spread_pct=spread_pct,
                    broker_detail=f"maker_first filled qty={filled_qty:g}",
                    filled_qty=filled_qty,
                    fill_price=fill_price,
                    filled=True,
                    resting=False,
                    order_id=order_id,
                    submitted_qty=float(qty),
                    liquidity_role=role,
                    fee_pct_estimate=fee_est,
                )
            if filled_qty > 0:
                logger.warning(
                    "%s | maker-first fill parcial qty=%s/%s — continúa fallback",
                    symbol,
                    f"{filled_qty:g}",
                    f"{qty:g}",
                )
                qty = max(0.0, float(qty) - filled_qty)
                if qty <= 0:
                    return OrderSubmitResult(
                        accepted=True,
                        order=final_order,
                        order_type="limit",
                        limit_price=limit_price,
                        spread_pct=spread_pct,
                        broker_detail="maker_first partial complete",
                        filled_qty=filled_qty,
                        fill_price=fill_price,
                        filled=True,
                        resting=False,
                        order_id=order_id,
                        submitted_qty=filled_qty,
                    )

            if cfg.fallback == "cancel":
                return OrderSubmitResult(
                    accepted=False,
                    order=final_order,
                    order_type="limit",
                    limit_price=limit_price,
                    spread_pct=spread_pct,
                    broker_detail="maker_first timeout (cancel, sin fallback)",
                    filled_qty=filled_qty,
                    liquidity_role=role_hint,
                    fee_pct_estimate=fee_est,
                )
            if cfg.fallback == "retry" and maker_attempt < retries:
                logger.info("%s | maker-first reintento %s/%s con tick_inside+%s", symbol, maker_attempt + 1, retries, 1)
                continue
            break

        if cfg.fallback == "taker":
            logger.info("%s | maker-first → fallback smart/taker | %s", symbol, reason)
            return self.submit_smart_order(
                symbol,
                qty,
                side,
                price=price,
                entry_price=entry_price,
                reason=f"{reason}_maker_fallback",
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                stop_pct=stop_pct,
                take_profit_pct=take_profit_pct,
                bid=bid,
                ask=ask,
                spread_pct=spread_pct,
                limit_spread_pct=limit_spread_pct,
                attempt=attempt,
                force_market=urgent_fallback,
            )

        return OrderSubmitResult(
            accepted=False,
            order_type="limit",
            spread_pct=spread_pct,
            broker_detail="maker_first sin fill y sin fallback",
        )

    def _broker_submit(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        tif: TimeInForce,
        use_limit: bool,
        limit_price: float | None,
    ) -> Order:
        if use_limit and limit_price:
            try:
                request: MarketOrderRequest | LimitOrderRequest = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    time_in_force=tif,
                    limit_price=limit_price,
                )
                return self.client.trading.submit_order(order_data=request)
            except Exception as exc:
                detail = alpaca_reject_detail(exc).lower()
                if tif is TimeInForce.IOC and ("ioc" in detail or "time_in_force" in detail):
                    fallback_tif = TimeInForce.GTC if is_crypto_symbol(symbol) else TimeInForce.DAY
                    logger.warning(
                        "%s | IOC no soportada (%s) — reintento limit TIF=%s",
                        symbol,
                        alpaca_reject_detail(exc),
                        fallback_tif.value,
                    )
                    request = LimitOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=side,
                        time_in_force=fallback_tif,
                        limit_price=limit_price,
                    )
                    return self.client.trading.submit_order(order_data=request)
                raise
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=tif,
        )
        return self.client.trading.submit_order(order_data=request)

    def _record_fill(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        fill_price: float,
        *,
        entry_price: float | None,
        reason: str,
        dry_run: bool,
        order_id: str,
        stop_price: float | None,
        take_profit_price: float | None,
        stop_pct: float | None,
        take_profit_pct: float | None,
    ) -> float:
        dust_absorbed = 0.0
        if fill_price > 0:
            self.journal.record(
                symbol,
                side.value,
                qty,
                fill_price,
                entry_price=entry_price,
                reason=reason,
                dry_run=dry_run,
                order_id=order_id or ("dry_run" if dry_run else ""),
            )
        if side is OrderSide.BUY and fill_price > 0:
            existing = self.position_book.get(symbol)
            if existing is None:
                self.position_book.open(
                    symbol,
                    qty,
                    fill_price,
                    stop_price=float(stop_price or fill_price * 0.99),
                    take_profit_price=float(take_profit_price or fill_price * 1.015),
                    stop_pct=float(stop_pct or 0.01),
                    take_profit_pct=float(take_profit_pct or 0.015),
                    dry_run=dry_run,
                )
            else:
                self.position_book.add_fill(symbol, qty, fill_price)
        elif side is OrderSide.SELL:
            pos = self.position_book.get(symbol)
            ref_qty = float(pos.opened_qty or pos.qty) if pos is not None else abs(float(qty))
            threshold = dust_threshold_for(self.client.settings, symbol, ref_qty)
            _updated, _sold, dust_absorbed = self.position_book.apply_sell_fill(
                symbol, qty, dust_threshold=threshold
            )
            if pos is None:
                logger.warning(
                    "%s | fill SELL %s sin fila en libro local — qty broker puede diferir",
                    symbol,
                    f"{qty:g}",
                )
        return dust_absorbed

    def _sync_book_qty_to_broker(self, symbol: str, broker_qty: float) -> None:
        tracked = self.position_book.get(symbol)
        if tracked is None or broker_qty <= 0:
            return
        local = abs(float(tracked.qty))
        if abs(local - broker_qty) <= 1e-12:
            return
        logger.warning(
            "%s | sync libro→broker antes de cerrar | local=%s broker=%s",
            symbol,
            f"{local:g}",
            f"{broker_qty:g}",
        )
        tracked.qty = broker_qty if tracked.qty >= 0 else -broker_qty
        self.position_book.save()

    def close_position(
        self,
        symbol: str,
        price: float | None = None,
        reason: str = "close",
        *,
        bid: float | None = None,
        ask: float | None = None,
        spread_pct: float | None = None,
        limit_spread_pct: float = 0.0015,
        attempt: int = 1,
        force_market: bool | None = None,
    ) -> OrderSubmitResult | None:
        symbol = normalize_symbol(sanitize_symbol(symbol))
        position = self.get_position(symbol)
        if position is None:
            logger.info("No hay posicion abierta en %s", symbol)
            audit("order_close", "deny", symbol=symbol, reason="no_position")
            return None

        # Fuente de verdad: qty del broker (qty_available si existe), no el libro local.
        raw_qty = sellable_qty_raw_from_position(position)
        try:
            broker_qty = abs(float(raw_qty)) if raw_qty else 0.0
        except (TypeError, ValueError):
            broker_qty = 0.0
        if broker_qty <= 0:
            logger.info("No hay qty vendible en %s", symbol)
            audit("order_close", "deny", symbol=symbol, reason="no_sellable_qty")
            return None

        fractional = is_crypto_symbol(symbol)
        side = OrderSide.SELL if float(getattr(position, "qty", broker_qty) or broker_qty) > 0 else OrderSide.BUY
        rules = self.asset_rules(symbol)
        qty = normalize_order_qty_for_asset(
            raw_qty or broker_qty,
            symbol=symbol,
            side=side,
            rules=rules,
            fractional=fractional,
            sellable_qty=raw_qty or broker_qty,
        )
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        tracked = self.position_book.get(symbol)
        book_qty = abs(float(tracked.qty)) if tracked is not None else 0.0
        opened_qty = abs(float(tracked.opened_qty or tracked.qty)) if tracked is not None else 0.0
        if not self.dry_run:
            self._sync_book_qty_to_broker(symbol, abs(float(getattr(position, "qty", broker_qty) or broker_qty)))

        logger.info(
            "%s | cierre qty | libro=%s opened=%s broker_raw=%s broker_qty=%s final=%s step=%s side=%s motivo=%s",
            symbol,
            f"{book_qty:.10f}".rstrip("0").rstrip("."),
            f"{opened_qty:.10f}".rstrip("0").rstrip("."),
            raw_qty or "—",
            f"{broker_qty:.10f}".rstrip("0").rstrip("."),
            f"{qty:.10f}".rstrip("0").rstrip("."),
            f"{float(rules.min_trade_increment):g}" if rules.min_trade_increment else "default_1e-6",
            side.value,
            reason,
        )
        audit("order_close", "allow", symbol=symbol, qty=qty, side=side.value, reason=reason)

        urgent = force_market if force_market is not None else str(reason).lower() in {
            "stop_loss",
            "take_profit",
        }
        maker_cfg = getattr(self, "_crypto_maker_config", None)
        if maker_cfg is not None and getattr(maker_cfg, "enabled", False) and is_crypto_symbol(symbol):
            result = self.submit_crypto_maker_first_order(
                symbol,
                qty,
                side,
                price=price,
                entry_price=entry,
                reason=reason,
                bid=bid,
                ask=ask,
                spread_pct=spread_pct,
                limit_spread_pct=limit_spread_pct,
                attempt=attempt,
                maker_config=maker_cfg,
                urgent_fallback=urgent,
            )
        else:
            result = self.submit_smart_order(
                symbol,
                qty,
                side,
                price=price,
                entry_price=entry,
                reason=reason,
                bid=bid,
                ask=ask,
                spread_pct=spread_pct,
                limit_spread_pct=limit_spread_pct,
                attempt=attempt,
                force_market=urgent,
            )
        if (
            result is not None
            and not result.accepted
            and side is OrderSide.SELL
            and is_insufficient_balance_reject(result.broker_detail)
            and not self.dry_run
        ):
            return self._retry_close_after_insufficient(
                symbol,
                price=price,
                reason=reason,
                bid=bid,
                ask=ask,
                spread_pct=spread_pct,
                limit_spread_pct=limit_spread_pct,
                attempt=attempt,
                prev_qty=qty,
                prev_detail=result.broker_detail,
            )
        return result

    def _retry_close_after_insufficient(
        self,
        symbol: str,
        *,
        price: float | None,
        reason: str,
        bid: float | None,
        ask: float | None,
        spread_pct: float | None,
        limit_spread_pct: float,
        attempt: int,
        prev_qty: float,
        prev_detail: str,
    ) -> OrderSubmitResult | None:
        """Relee get_position y vende el remanente exacto (floor) para romper el 40301000."""
        position = self.get_position(symbol)
        if position is None:
            logger.warning(
                "%s | insufficient balance pero ya no hay posición en broker | %s",
                symbol,
                prev_detail,
            )
            tracked = self.position_book.get(symbol)
            if tracked is not None:
                self.position_book.close(symbol)
            return None

        broker_raw = sellable_qty_raw_from_position(position)
        try:
            broker_qty = abs(float(broker_raw)) if broker_raw else 0.0
        except (TypeError, ValueError):
            broker_qty = 0.0
        if broker_qty <= 0:
            logger.warning("%s | insufficient balance y qty_available=0 — limpiando libro", symbol)
            self.position_book.close(symbol)
            return None

        fractional = is_crypto_symbol(symbol)
        rules = self.asset_rules(symbol, force_refresh=True)
        try:
            qty = normalize_order_qty_for_asset(
                broker_raw or broker_qty,
                symbol=symbol,
                side=OrderSide.SELL,
                rules=rules,
                fractional=fractional,
                sellable_qty=broker_raw or broker_qty,
            )
            if fractional and abs(qty - prev_qty) <= 1e-12:
                broker_qty_dec = _asset_decimal(broker_raw or broker_qty) or Decimal("0")
                retry_source = max(Decimal("0"), broker_qty_dec - _retry_qty_step(rules))
                qty = normalize_order_qty_for_asset(
                    str(retry_source),
                    symbol=symbol,
                    side=OrderSide.SELL,
                    rules=rules,
                    fractional=True,
                    sellable_qty=broker_raw or broker_qty,
                )
        except ValidationError:
            logger.error("%s | remanente tras normalización inválido | raw=%s", symbol, broker_raw)
            return OrderSubmitResult(accepted=False, broker_detail=prev_detail)

        self._sync_book_qty_to_broker(symbol, abs(float(getattr(position, "qty", broker_qty) or broker_qty)))
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        logger.warning(
            "%s | reintento cierre por insufficient balance | prev_qty=%s → broker_qty=%s final=%s step=%s | %s",
            symbol,
            f"{prev_qty:g}",
            f"{broker_qty:.10f}".rstrip("0").rstrip("."),
            f"{qty:g}",
            f"{float(rules.min_trade_increment):g}" if rules.min_trade_increment else "default_1e-6",
            prev_detail,
        )
        audit(
            "order_close_retry",
            "allow",
            symbol=symbol,
            qty=qty,
            reason=f"insufficient_balance:{reason}",
        )
        return self.submit_smart_order(
            symbol,
            qty,
            OrderSide.SELL,
            price=price,
            entry_price=entry,
            reason=reason,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            limit_spread_pct=limit_spread_pct,
            attempt=max(1, int(attempt)) + 1,
            force_market=str(reason).lower() in {"stop_loss", "take_profit"},
        )

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
