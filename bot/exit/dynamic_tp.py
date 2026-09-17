"""TP dinámico por ATR + protección de gap (capa paralela, no reemplaza SL/trailing)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from alpaca.trading.enums import OrderSide

from bot.alpaca.execution import normalize_order_qty_for_asset
from bot.exit.limit_orders import (
    cancel_order,
    fetch_order,
    order_fill_snapshot,
    submit_resting_limit_sell,
)
from bot.market.assets import is_crypto_symbol
from bot.market.dust import effective_qty
from bot.reporting.pnl import PnLEvent, compute_pnl
from bot.storage.dynamic_tp_state import DynamicTpRow, DynamicTpStateStore

if TYPE_CHECKING:
    from bot.engine import TradingEngine
    from bot.storage.positions import TrackedPosition

logger = logging.getLogger(__name__)

REASON_LIMIT = "dynamic_tp_limit"
REASON_GAP_MARKET = "dynamic_tp_gap_market"
REASON_GAP_LIMIT_IOC = "dynamic_tp_gap_limit_ioc"


def compute_dynamic_tp(
    entry_price: float,
    atr_value: float | None,
    *,
    base_pct: float,
    atr_mult: float,
    max_pct: float,
) -> tuple[float, float]:
    """Target = entry * (1 + pct) con pct derivado de ATR (misma idea que SL/TP por volatilidad)."""
    entry = float(entry_price)
    if entry <= 0:
        return 0.0, 0.0
    pct = float(base_pct)
    if atr_value and atr_value > 0:
        from_atr = (float(atr_value) * float(atr_mult)) / entry
        if from_atr > 0:
            pct = from_atr
    pct = min(float(max_pct), max(pct, 0.0001))
    return entry * (1.0 + pct), pct


class DynamicTakeProfitLayer:
    """Registra TP límite al comprar; gap parcial en tick; limpia al cerrar posición."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.store = DynamicTpStateStore()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "dynamic_tp_enabled", False))

    def register_entry(
        self,
        engine: TradingEngine,
        symbol: str,
        tracked: TrackedPosition,
        atr_value: float | None,
    ) -> None:
        if not self.enabled:
            return
        entry = float(tracked.avg_entry_price)
        qty = float(tracked.qty)
        if entry <= 0 or qty <= 0:
            return

        tp_price, tp_pct = compute_dynamic_tp(
            entry,
            atr_value,
            base_pct=self.settings.dynamic_tp_base_pct,
            atr_mult=self.settings.dynamic_tp_atr_mult,
            max_pct=self.settings.dynamic_tp_max_pct,
        )
        if tp_price <= entry:
            logger.warning("%s | dynamic TP omitido — precio target inválido", symbol)
            return

        existing = self.store.get(symbol)
        if existing and existing.order_id:
            if not cancel_order(engine.client, existing.order_id, symbol=symbol):
                logger.warning("%s | dynamic TP omitido — no se pudo reemplazar la orden previa", symbol)
                return

        order_id, detail = submit_resting_limit_sell(
            engine.client,
            symbol=symbol,
            qty=qty,
            limit_price=tp_price,
            dry_run=engine.executor.dry_run,
        )
        if not order_id:
            logger.error("%s | no se pudo colocar limit TP | %s", symbol, detail)
            return

        self.store.upsert(
            DynamicTpRow(
                symbol=str(symbol).upper(),
                entry_price=entry,
                tp_price=tp_price,
                tp_pct=tp_pct,
                order_id=order_id,
                order_qty=qty,
                filled_qty=0.0,
                gap_partial_done=False,
                atr_value=atr_value,
            )
        )
        logger.info(
            "%s | dynamic TP | entry=%.4f tp=%.4f (+%.2f%%) qty=%s order=%s",
            symbol,
            entry,
            tp_price,
            tp_pct * 100,
            qty,
            order_id,
        )

    def cancel_for_symbol(self, engine: TradingEngine, symbol: str) -> bool:
        row = self.store.get(symbol)
        if row is None:
            return True
        if row.order_id and not cancel_order(engine.client, row.order_id, symbol=symbol):
            logger.warning("%s | dynamic TP no se pudo cancelar; se conserva la protección", symbol)
            return False
        self.store.remove(symbol)
        return True

    def on_tick(
        self,
        engine: TradingEngine,
        symbol: str,
        last_price: float,
        bid: float | None,
        ask: float | None,
        spread: float | None,
        tracked: TrackedPosition,
    ) -> bool:
        """True si esta capa manejó la salida (parcial o total) en este tick."""
        if not self.enabled:
            return False
        row = self.store.get(symbol)
        if row is None:
            return False

        if self._check_limit_filled(engine, symbol, row, tracked, last_price):
            return True

        if row.gap_partial_done or not row.order_id:
            return False

        if last_price < row.tp_price:
            return False

        return self._execute_gap_partial(
            engine, symbol, row, tracked, last_price, bid, ask, spread
        )

    def _check_limit_filled(
        self,
        engine: TradingEngine,
        symbol: str,
        row: DynamicTpRow,
        tracked: TrackedPosition,
        last_price: float,
    ) -> bool:
        if row.order_id and row.order_id.startswith("dry_run"):
            if engine.executor.dry_run and last_price >= row.tp_price:
                return self._finalize_limit_fill(
                    engine,
                    symbol,
                    row,
                    tracked,
                    row.tp_price,
                    total_filled_qty=float(tracked.qty),
                    order_filled=True,
                )
            return False
        if not row.order_id:
            return False

        order = fetch_order(engine.client, row.order_id)
        if order is None:
            return False
        kind, filled_qty, fill_price = order_fill_snapshot(order)
        total_filled = min(float(filled_qty), float(row.order_qty))
        if total_filled <= float(row.filled_qty or 0.0) + 1e-8:
            return False
        px = fill_price if fill_price > 0 else row.tp_price
        return self._finalize_limit_fill(
            engine,
            symbol,
            row,
            tracked,
            px,
            total_filled_qty=total_filled,
            order_filled=kind == "filled",
        )

    def _finalize_limit_fill(
        self,
        engine: TradingEngine,
        symbol: str,
        row: DynamicTpRow,
        tracked: TrackedPosition,
        fill_price: float,
        *,
        total_filled_qty: float,
        order_filled: bool,
    ) -> bool:
        entry = float(tracked.avg_entry_price)
        prev_filled = float(row.filled_qty or 0.0)
        qty = min(float(total_filled_qty), float(tracked.qty)) - prev_filled
        qty = max(0.0, float(qty))
        if qty <= 1e-8:
            return False

        dust_qty = engine.executor._record_fill(
            symbol,
            qty,
            OrderSide.SELL,
            fill_price,
            entry_price=entry,
            reason=REASON_LIMIT,
            dry_run=engine.executor.dry_run,
            order_id=row.order_id,
            stop_price=getattr(tracked, "stop_price", None),
            take_profit_price=getattr(tracked, "take_profit_price", None),
            stop_pct=getattr(tracked, "stop_pct", None),
            take_profit_pct=getattr(tracked, "take_profit_pct", None),
        )
        if not engine.executor.dry_run:
            remaining = engine._reconcile_book_qty_from_broker(symbol)
        else:
            current = engine.executor.position_book.get(symbol)
            remaining = abs(float(current.qty)) if current is not None else 0.0

        snap = compute_pnl(symbol, qty, entry, fill_price, PnLEvent.CLOSED)
        engine.reporter.emit(snap)
        ref_qty = float(tracked.opened_qty or tracked.qty)
        remaining_eff = effective_qty(engine.settings, symbol, remaining, ref_qty)
        event_id = engine._event_id(symbol, "close", REASON_LIMIT, tracked=tracked)
        row.filled_qty = float(total_filled_qty)
        if order_filled:
            self.store.remove(symbol)
            if remaining_eff > 1e-8:
                engine.notifier.notify_partial_close(
                    symbol,
                    qty,
                    remaining_eff,
                    entry,
                    fill_price,
                    snap.pnl_abs,
                    snap.pnl_pct,
                    REASON_LIMIT,
                    "filled",
                    float(row.order_qty),
                    engine.executor.dry_run,
                    event_id=event_id,
                )
                refreshed = engine.executor.position_book.get(symbol)
                if refreshed is not None:
                    self.register_entry(engine, symbol, refreshed, row.atr_value)
            else:
                engine._note_position_closed(symbol, REASON_LIMIT)
                engine.notifier.notify_closed(
                    symbol,
                    qty,
                    entry,
                    fill_price,
                    snap.pnl_abs,
                    snap.pnl_pct,
                    REASON_LIMIT,
                    engine.executor.dry_run,
                    event_id=event_id,
                    dust_qty=dust_qty,
                )
        else:
            self.store.upsert(row)
            if remaining_eff > 1e-8:
                engine.notifier.notify_partial_close(
                    symbol,
                    qty,
                    remaining_eff,
                    entry,
                    fill_price,
                    snap.pnl_abs,
                    snap.pnl_pct,
                    REASON_LIMIT,
                    "partial",
                    float(row.order_qty),
                    engine.executor.dry_run,
                    event_id=event_id,
                )
        logger.info(
            "%s | dynamic TP limit fill | +%s @ %.4f entry=%.4f | acumulado=%s/%s | remaining=%s | status=%s",
            symbol,
            qty,
            fill_price,
            entry,
            f"{float(row.filled_qty):g}",
            f"{float(row.order_qty):g}",
            f"{remaining_eff:g}",
            "filled" if order_filled else "partial",
        )
        return True

    def _execute_gap_partial(
        self,
        engine: TradingEngine,
        symbol: str,
        row: DynamicTpRow,
        tracked: TrackedPosition,
        last_price: float,
        bid: float | None,
        ask: float | None,
        spread: float | None,
    ) -> bool:
        total_qty = float(tracked.qty)
        if total_qty <= 0:
            self.store.remove(symbol)
            return False

        partial_pct = float(self.settings.dynamic_tp_gap_sell_pct)
        try:
            sell_qty = normalize_order_qty_for_asset(
                total_qty * partial_pct,
                symbol=symbol,
                side=OrderSide.SELL,
                rules=engine.executor.asset_rules(symbol),
                fractional=is_crypto_symbol(symbol),
                sellable_qty=total_qty,
            )
        except Exception as exc:
            logger.info("%s | gap en TP %.4f pero qty parcial inválida — %s", symbol, row.tp_price, exc)
            return False
        if sell_qty <= 0 or sell_qty >= total_qty:
            logger.info(
                "%s | gap en TP %.4f pero qty parcial inválida — se deja al trailing",
                symbol,
                row.tp_price,
            )
            return False

        if row.order_id and not cancel_order(engine.client, row.order_id, symbol=symbol):
            logger.warning("%s | gap TP omitido — no se pudo cancelar el limit TP vigente", symbol)
            return False

        limit_spread = float(engine.settings.order_limit_spread_pct)
        wide = spread is not None and spread > limit_spread
        entry = float(tracked.avg_entry_price)
        reason = REASON_GAP_LIMIT_IOC if wide else REASON_GAP_MARKET

        if engine.executor.pending_fills.has_close(symbol):
            logger.info("%s | gap TP pero cierre ya en vuelo", symbol)
            return False

        event_id = engine._event_id(symbol, "close", reason, tracked=tracked)
        result = engine.executor.submit_smart_order(
            symbol,
            sell_qty,
            OrderSide.SELL,
            price=last_price,
            entry_price=entry,
            reason=reason,
            bid=bid,
            ask=ask,
            spread_pct=spread,
            limit_spread_pct=limit_spread,
            attempt=1,
        )
        if not result.accepted:
            logger.warning("%s | gap partial rechazada | %s", symbol, result.broker_detail)
            refreshed = engine.executor.position_book.get(symbol)
            if refreshed is not None:
                self.register_entry(engine, symbol, refreshed, row.atr_value)
            return False

        if not result.filled:
            engine._watch_resting(
                result,
                symbol=symbol,
                side="sell",
                qty=sell_qty,
                reason=reason,
                is_close=True,
                event_id=event_id,
                entry_price=entry,
                signal_price=last_price,
            )
            row.gap_partial_done = True
            row.order_id = ""
            self.store.upsert(row)
            return True

        fill_price = float(result.fill_price or last_price)
        engine._maybe_notify_fractional_wide(result, symbol, "sell", sell_qty, "")
        snap = compute_pnl(symbol, sell_qty, entry, fill_price, PnLEvent.CLOSED)
        engine.reporter.emit(snap)
        engine.notifier.notify_closed(
            symbol,
            sell_qty,
            entry,
            fill_price,
            snap.pnl_abs,
            snap.pnl_pct,
            reason,
            engine.executor.dry_run,
            event_id=event_id,
        )
        row.gap_partial_done = True
        row.order_id = ""
        self.store.upsert(row)
        remaining = engine.executor.position_book.get(symbol)
        rem_qty = float(remaining.qty) if remaining else 0.0
        logger.info(
            "%s | gap partial %.0f%% @ %.4f | restante=%.4g con trailing | motivo=%s",
            symbol,
            partial_pct * 100,
            fill_price,
            rem_qty,
            reason,
        )
        return True

    def poll_limit_orders(self, engine: TradingEngine) -> None:
        """Revisa fills de límites TP en el ciclo REST (complemento al tick WS)."""
        if not self.enabled:
            return
        for row in list(self.store.list_rows()):
            if row.gap_partial_done:
                continue
            tracked = engine.executor.position_book.get(row.symbol)
            if tracked is None:
                self.store.remove(row.symbol)
                continue
            last = float(getattr(tracked, "current_price", 0.0) or tracked.avg_entry_price)
            self._check_limit_filled(engine, row.symbol, row, tracked, last)

    def bootstrap_open_positions(self, engine: TradingEngine) -> None:
        """Posiciones ya abiertas antes del deploy: coloca limit TP si falta estado."""
        if not self.enabled:
            return
        for tracked in engine.executor.position_book.list():
            if self.store.get(tracked.symbol):
                continue
            atr = engine._atr_cache.get(tracked.symbol.upper())
            self.register_entry(engine, tracked.symbol, tracked, atr)
