"""Motor de trading: planificador multi-TF → señal → riesgo/SL-TP → ejecución + P&L."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from alpaca.trading.enums import OrderSide

from bot.alpaca.client import AccountSnapshot, AlpacaClient
from bot.alpaca.market_clock import MarketClockView
from bot.alpaca.execution import (
    OrderExecutor,
    OrderSubmitResult,
    classify_order,
    is_insufficient_balance_reject,
)
from bot.alpaca.market_data import LiveTape, MarketDataService
from bot.alpaca.stream import LiveMarketStream
from bot.config import Settings
from bot.market.assets import asset_class_for, is_crypto_symbol, normalize_symbol, positions_by_symbol
from bot.market.dust import dust_threshold_for, effective_qty, is_dust_qty, refresh_crypto_mins
from bot.market.mode import is_symbol_tradable, resolve_trading_mode, trading_mode_label
from bot.notify.telegram import TelegramNotifier, make_event_id
from bot.reporting.pnl import PerformanceReporter, PnLEvent, compute_pnl
from bot.risk.manager import RiskManager
from bot.risk.stops import ExitReason
from bot.scheduler.multi_tf import MultiTimeframeEngine
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, ValidationError
from bot.storage.control import BotControl
from bot.storage.pending_orders import RestingOrder
from bot.strategy.base import Signal, Strategy, StrategyContext
from bot.strategy.indicators import last_atr, momentum_pct
from bot.strategy.price_flow import PriceFlowFilter
from bot.exit.dynamic_tp import DynamicTakeProfitLayer
from bot.storage.breakout_state import BreakoutStateStore
from bot.strategy.signal_filters import SignalFilterLayer

import pandas as pd

logger = logging.getLogger(__name__)

# Trailing stop: se activa con +2.50% flotante y sube el SL (nunca lo baja).
# El SL inicial de la posición (-1.50% en acciones afinadas) no se toca hasta entonces.
TRAIL_ACTIVATE_PCT = 0.025
TRAIL_OFFSET_PCT = 0.0125
FILL_POLL_SECONDS = 5.0


@dataclass
class PendingExecution:
    """Orden rechazada que se reintenta en el siguiente tick."""

    symbol: str
    side: str
    qty: float
    reason: str
    last_price: float
    entry_price: float | None = None
    is_close: bool = False
    attempts: int = 0
    first_at: float = 0.0
    next_at: float = 0.0
    last_error: str = ""
    stop_price: float | None = None
    take_profit_price: float | None = None
    stop_pct: float | None = None
    take_profit_pct: float | None = None
    alerted: bool = False
    signal_price: float = 0.0
    event_id: str = ""


def _trailing_bar_timeframe(settings: Settings, symbol: str) -> str:
    """Velas Alpaca para el high del trailing: 15m/30m cripto, 1m acciones."""
    return settings.crypto_bar_timeframe if is_crypto_symbol(symbol) else "1Min"


def _current_bar_high(bars: pd.DataFrame, last_price: float) -> float:
    """High acumulado de la vela en curso y la recién cerrada (Alpaca bars)."""
    peak = float(last_price) if last_price and last_price > 0 else 0.0
    if bars is None or bars.empty or "high" not in bars.columns:
        return peak
    highs = bars["high"].astype(float).dropna().tail(2)
    if highs.empty:
        return peak
    bar_high = float(highs.max())
    return max(peak, bar_high) if peak > 0 else bar_high


def _peak_since_entry(tracked, last_price: float) -> float:
    """High de la posición desde el fill. No usa máximos de sesión anteriores a la entrada."""
    entry = float(getattr(tracked, "avg_entry_price", 0) or 0)
    last = float(last_price or 0)
    stored = float(getattr(tracked, "peak_price", 0) or 0)
    peak = max(v for v in (entry, last, stored) if v > 0) if any(v > 0 for v in (entry, last, stored)) else 0.0
    if tracked is not None and peak > 0:
        tracked.peak_price = peak
    return peak


def _ratchet_trailing_stop(tracked, entry: float, qty: float, peak_price: float) -> float | None:
    """Fallback % fijo si no hay ATR. El motor prefiere RiskManager.trailing_stop."""
    if tracked is None or qty <= 0 or entry <= 0 or peak_price <= 0:
        return None
    pnl_pct = peak_price / entry - 1.0
    if pnl_pct < TRAIL_ACTIVATE_PCT:
        return None
    candidate = max(entry, peak_price * (1.0 - TRAIL_OFFSET_PCT))
    current_sl = float(getattr(tracked, "stop_price", 0.0) or 0.0)
    if candidate <= current_sl:
        return None
    return candidate


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        client: AlpacaClient,
        market_data: MarketDataService,
        executor: OrderExecutor,
        strategy: Strategy,
        risk: RiskManager,
        reporter: PerformanceReporter | None = None,
        flow: PriceFlowFilter | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.market_data = market_data
        self.executor = executor
        self.strategy = strategy
        self.risk = risk
        self.reporter = reporter or PerformanceReporter()
        self.flow = flow or PriceFlowFilter(
            max_spread_pct=settings.max_spread_pct,
            adverse_momentum_pct=settings.adverse_momentum_pct,
        )
        self.notifier = notifier or TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )
        self.control = BotControl()
        self._running = False
        self._pause_applied = False
        self._mtf: MultiTimeframeEngine | None = None
        self._stream: LiveMarketStream | None = None
        self._stream_shutdown_done = False
        self._graceful_shutdown_done = False
        self._shutdown_event = threading.Event()
        self._clock_cache: MarketClockView | None = None
        self._clock_cache_at = 0.0
        self._tick_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, PendingExecution] = {}
        self._close_cooldown_until: dict[str, float] = {}
        self._last_fill_poll = 0.0
        self.breakout_state = BreakoutStateStore()
        self.signal_filters = SignalFilterLayer(settings, self.breakout_state)
        self._atr_cache: dict[str, float] = {}
        self._daily_halt_day = ""
        self.dynamic_tp = DynamicTakeProfitLayer(settings)

    def run_once(self) -> None:
        """Ejecuta todas las capas una vez (modo --once)."""
        self._mtf_engine().run_all_layers_once()

    def run_loop(self, should_stop: Callable[[], bool] | None = None) -> None:
        self._running = True
        mtf = self._mtf_engine()
        mtf.bootstrap()
        tick = self.settings.scheduler_tick_seconds
        clock = self.client.get_market_clock()
        mode, active = resolve_trading_mode(clock, self.settings)
        open_n = len(self.executor.list_positions())
        logger.info(
            "Motor Tesla 3-6-9 | %s | activos=%s | acciones=%s | cripto=%s | tick=%ss | "
            "capas 3m=%ss 6m=%ss 9m=%ss | dry_run=%s | SL=%.2f%% TP=%.2f%% | posiciones=%s",
            trading_mode_label(mode),
            ",".join(active) or "—",
            ",".join(self.settings.stock_symbols),
            ",".join(self.settings.crypto_symbols),
            tick,
            self.settings.tf_3m_seconds,
            self.settings.tf_6m_seconds,
            self.settings.tf_9m_seconds,
            self.executor.dry_run,
            self.settings.stop_loss_pct * 100,
            self.settings.take_profit_pct * 100,
            open_n,
        )
        logger.info(
            "Ejecucion | ORDER_LIMIT_SPREAD_PCT=%.4f | ORDER_RETRY_MAX_SL=%s | "
            "ORDER_RETRY_TIMEOUT_SL_SECONDS=%s | ORDER_RETRY_MAX=%s | "
            "ORDER_RETRY_TIMEOUT_SECONDS=%s",
            self.settings.order_limit_spread_pct,
            self.settings.order_retry_max_sl,
            self.settings.order_retry_timeout_sl_seconds,
            self.settings.order_retry_max,
            self.settings.order_retry_timeout_seconds,
        )
        logger.info(
            "Senal | multi-regimen | ruptura+pullback+meanrev+squeeze | "
            "ADX umbral=%.1f | colision=%s | riesgo fijo=%s (%.2f%%) | freno diario=%.2f%%",
            self.settings.adx_threshold,
            self.settings.strategy_collision_priority,
            "on" if self.settings.use_fixed_risk_sizing else "off",
            self.settings.risk_percent_per_trade * 100,
            self.settings.daily_loss_limit_pct * 100,
        )
        logger.info(
            "Senal | disparador=ruptura+volumen lookback=%s vol>=%.2fx rango>=%.2fxATR | "
            "sesgo=SMA lenta | ADX%s periodo=%s umbral=%.1f | cooldown=%s velas | "
            "confirmacion entrada=%s tf_stock=%s tf_crypto=%s/%s",
            self.settings.breakout_lookback_periods,
            self.settings.breakout_volume_mult,
            self.settings.breakout_min_range_atr_mult,
            "" if self.settings.adx_filter_enabled else " OFF",
            self.settings.adx_period,
            self.settings.adx_threshold,
            self.settings.breakout_cooldown_bars,
            "on" if self.settings.entry_confirmation_enabled else "off",
            self.settings.confirm_higher_tf,
            self.settings.crypto_bar_timeframe,
            self.settings.crypto_regime_timeframe,
        )
        logger.info(
            "Filtros extra | ADX overrides=%s | ATR periodo=%s SL=%.2fx TP=%.2fx trail=%.2fx | "
            "telegram_filtradas=%s",
            self.settings.adx_threshold_overrides or "{}",
            self.settings.atr_period,
            self.settings.atr_sl_mult,
            self.settings.atr_tp_mult,
            self.settings.atr_trailing_mult,
            self.settings.telegram_notify_filtered,
        )
        logger.info(
            "Stops acciones | SL=%.2fx ATR piso=%.2f%% | Cripto | SL=%.2fx TP=%.2fx trail=%.2fx "
            "piso TP=%.2f%% RSI pullback max=%.0f",
            self.settings.stock_atr_sl_mult,
            self.settings.stock_min_stop_pct * 100,
            self.settings.crypto_atr_sl_mult,
            self.settings.crypto_atr_tp_mult,
            self.settings.crypto_atr_trailing_mult,
            self.settings.crypto_min_tp_pct * 100,
            self.settings.crypto_trend_pullback_rsi_max,
        )
        logger.info(
            "Score entrada | %s min=%.0f | colision=%s | macro -%.0f | spike -%.0f/+%.0f",
            "on" if self.settings.entry_score_enabled else "off",
            self.settings.entry_score_min,
            self.settings.strategy_collision_mode,
            self.settings.entry_score_macro_penalty,
            self.settings.entry_score_spike_adverse_penalty,
            self.settings.entry_score_spike_favor_bonus,
        )
        if not self.executor.dry_run:
            self.executor.position_book.drop_dry_run_rows()
        self._sweep_dust_positions()
        if self.settings.dynamic_tp_enabled:
            logger.info(
                "TP dinámico | ON | ATR mult=%.2f base=%.2f%% max=%.2f%% gap=%.0f%%",
                self.settings.dynamic_tp_atr_mult,
                self.settings.dynamic_tp_base_pct * 100,
                self.settings.dynamic_tp_max_pct * 100,
                self.settings.dynamic_tp_gap_sell_pct * 100,
            )
            self.dynamic_tp.bootstrap_open_positions(self)
        self._start_market_stream()
        if self.notifier.enabled:
            logger.info("Telegram: arranque async (no bloquea el motor)")
            self.notifier.start_background_startup(
                symbols=f"{trading_mode_label(mode)}: {','.join(active)}",
                tick_seconds=tick,
            )
        # SMA 3-6-9 sigue en run_tick (~60s). El loop es 1s: ticks WS + REST si hay fallback.
        last_mtf = 0.0
        last_fallback_mark = 0.0
        last_dust_refresh = time.monotonic()
        dust_refresh_sec = float(self.settings.dust_threshold_refresh_seconds)
        while self._running and not (should_stop and should_stop()):
            started = time.monotonic()
            try:
                now = started
                if self.control.is_paused() or last_mtf == 0.0 or now - last_mtf >= tick:
                    mtf.run_tick()
                    last_mtf = now
                if self._stream is not None:
                    try:
                        self._stream.set_stock_expected(bool(self._cached_clock().is_open))
                    except Exception:
                        pass
                    if self._stream.health.in_fallback() and now - last_fallback_mark >= 5.0:
                        self._mark_all_positions()
                        last_fallback_mark = now
                self._flush_pending_orders()
                if self._last_fill_poll == 0.0 or now - self._last_fill_poll >= FILL_POLL_SECONDS:
                    self.settle_resting_orders()
                    self._last_fill_poll = now
                if (
                    dust_refresh_sec > 0
                    and not self.executor.dry_run
                    and now - last_dust_refresh >= dust_refresh_sec
                ):
                    refresh_crypto_mins(self.client, self.settings)
                    last_dust_refresh = now
            except Exception as exc:
                log_caught(logger, "trading_cycle_failed", exc)
            elapsed = time.monotonic() - started
            self._interruptible_sleep(max(0.0, 1.0 - elapsed))
        self._stop_market_stream()
        logger.info("Motor detenido")

    def stop(self) -> None:
        self.request_shutdown()

    def request_shutdown(self) -> None:
        """Señal ligera (desde handler SIG*): despierta el loop principal."""
        self._running = False
        self._shutdown_event.set()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Apagado ordenado: detiene el loop y cierra WS antes de que termine el proceso."""
        if self._graceful_shutdown_done:
            return
        self._graceful_shutdown_done = True
        self._running = False
        self._shutdown_event.set()
        self._shutdown_market_stream(timeout=timeout)

    def _mtf_engine(self) -> MultiTimeframeEngine:
        if self._mtf is None:
            self._mtf = MultiTimeframeEngine(self)
        return self._mtf

    def latest_price(self, symbol: str, fallback: float) -> float:
        """Tick WebSocket si hay; si no, el fallback REST."""
        if self._stream is not None:
            px = self._stream.last_price(symbol)
            if px and px > 0:
                return float(px)
        return float(fallback)

    def live_tape(self, symbol: str, fallback_price: float | None = None) -> LiveTape | None:
        """Tape desde WS si hay tick/quote; si no, REST (2 tokens data)."""
        md_symbol = normalize_symbol(symbol)
        stream = self._stream
        if stream is not None:
            last = stream.last_price(md_symbol)
            bid = ask = spread = None
            try:
                bid, ask = stream.last_quote(md_symbol)
            except Exception:
                bid = ask = None
            if bid and ask and ask > 0:
                spread = (float(ask) - float(bid)) / float(ask)
                if not last or last <= 0:
                    last = (float(bid) + float(ask)) / 2.0
            if last and last > 0:
                return LiveTape(
                    symbol=md_symbol,
                    last_price=float(last),
                    bid=float(bid) if bid else None,
                    ask=float(ask) if ask else None,
                    spread_pct=spread,
                    source="ws",
                )
        return self.market_data.get_live_tape(md_symbol, fallback_price=fallback_price)

    def _start_market_stream(self) -> None:
        if self._stream is not None:
            return
        settings = self.settings
        self._stream = LiveMarketStream(
            settings.api_key_id,
            settings.api_secret_key,
            stock_symbols=list(settings.stock_symbols),
            crypto_symbols=list(settings.crypto_symbols),
            on_tick=self.apply_tick_trailing,
            on_fallback=self._notify_stream_fallback,
            on_restored=self._notify_stream_restored,
            on_unstable=self._notify_stream_unstable,
            stale_after=settings.stream_stale_seconds,
            data_timeout=settings.stream_data_timeout_seconds or None,
            ping_interval=settings.stream_ws_ping_interval,
            ping_timeout=settings.stream_ws_ping_timeout,
            notify_debounce=settings.stream_notify_debounce_seconds,
            reconnect_min=settings.stream_reconnect_min_seconds,
            reconnect_max=settings.stream_reconnect_max_seconds,
        )
        self._stream.start()
        logger.info("stream | motor en modo streaming (REST solo si el socket está abajo)")

    def _stop_market_stream(self) -> None:
        self._shutdown_market_stream(timeout=8.0)

    def _shutdown_market_stream(self, *, timeout: float = 5.0) -> None:
        if self._stream_shutdown_done:
            return
        stream = self._stream
        if stream is None:
            self._stream_shutdown_done = True
            return
        self._stream = None
        try:
            stream.stop(timeout=timeout)
        except Exception as exc:
            logger.warning("stream | shutdown: %s", type(exc).__name__)
        self._stream_shutdown_done = True

    def _notify_stream_fallback(self, reason: str) -> None:
        logger.error("stream | fallback REST | %s", reason)
        if self.notifier.enabled:
            self.notifier.notify_stream_fallback(reason)

    def _notify_stream_restored(self) -> None:
        logger.info("stream | restaurado — otra vez tick a tick")
        if self.notifier.enabled:
            self.notifier.notify_stream_restored()
        self.settle_resting_orders()

    def _notify_stream_unstable(self, drops: int, window_seconds: float, recovered: bool) -> None:
        logger.warning(
            "stream | inestable | %s caidas en %.0fs | recuperado=%s",
            drops,
            window_seconds,
            recovered,
        )
        if self.notifier.enabled:
            self.notifier.notify_stream_unstable(drops, window_seconds, recovered)
        if recovered:
            self.settle_resting_orders()

    def _cached_clock(self) -> MarketClockView:
        now = time.monotonic()
        if self._clock_cache is not None and now - self._clock_cache_at < 15.0:
            return self._clock_cache
        try:
            self._clock_cache = self.client.get_market_clock()
            self._clock_cache_at = now
        except Exception:
            if self._clock_cache is None:
                raise
        return self._clock_cache

    def _live_quote(
        self, symbol: str, tape: LiveTape | None
    ) -> tuple[float | None, float | None, float | None]:
        bid = ask = None
        if self._stream is not None:
            bid, ask = self._stream.last_quote(symbol)
        if (bid is None or ask is None) and tape is not None:
            bid = tape.bid if bid is None else bid
            ask = tape.ask if ask is None else ask
        spread = None
        if bid and ask and ask > 0:
            spread = (float(ask) - float(bid)) / float(ask)
        return bid, ask, spread

    def settle_resting_orders(self) -> None:
        """Consulta Alpaca por órdenes en vuelo y confirma fill o vencimiento."""
        if self.executor.dry_run:
            return
        for row in self.executor.pending_fills.list():
            try:
                order = self.executor.get_order(row.order_id)
            except Exception as exc:
                log_caught(logger, "resting_order_lookup_failed", exc, symbol=row.symbol)
                continue
            if order is None:
                continue
            self._apply_resting_order(row, order)
        if self.settings.dynamic_tp_enabled:
            self.dynamic_tp.poll_limit_orders(self)

    def _watch_resting(
        self,
        result: OrderSubmitResult,
        *,
        symbol: str,
        side: str,
        qty: float,
        reason: str,
        is_close: bool,
        event_id: str,
        entry_price: float | None,
        signal_price: float,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
        stop_pct: float | None = None,
        take_profit_pct: float | None = None,
    ) -> None:
        order_id = result.order_id or ""
        if not order_id:
            logger.warning("%s | accepted sin fill y sin order_id — no se cierra el libro", symbol)
            return
        self.executor.pending_fills.upsert(
            RestingOrder(
                order_id=order_id,
                symbol=symbol,
                side=side.lower(),
                qty=qty,
                reason=reason,
                is_close=is_close,
                event_id=event_id,
                submitted_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                filled_qty=float(result.filled_qty or 0.0),
                entry_price=entry_price,
                signal_price=signal_price,
                order_type=result.order_type,
                limit_price=result.limit_price,
                last_status=result.broker_detail or "accepted",
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                stop_pct=stop_pct,
                take_profit_pct=take_profit_pct,
            )
        )
        logger.info(
            "%s | pendiente de fill | id=%s | %s qty=%s | libro intacto hasta filled",
            symbol,
            order_id,
            side,
            qty,
        )

    def _apply_resting_order(self, row: RestingOrder, order: object) -> None:
        kind = classify_order(order)
        status = str(getattr(order, "status", "") or row.last_status)
        if "." in status:
            status = status.split(".")[-1]
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        fill_price = float(getattr(order, "filled_avg_price", None) or 0.0)
        delta = filled_qty - float(row.filled_qty or 0.0)
        if delta > 1e-8 and fill_price > 0:
            side = OrderSide.BUY if row.side.lower() == "buy" else OrderSide.SELL
            dust_delta = self.executor._record_fill(
                row.symbol,
                delta,
                side,
                fill_price,
                entry_price=row.entry_price,
                reason=row.reason,
                dry_run=False,
                order_id=row.order_id,
                stop_price=row.stop_price,
                take_profit_price=row.take_profit_price,
                stop_pct=row.stop_pct,
                take_profit_pct=row.take_profit_pct,
            )
            if dust_delta > 0:
                row.dust_qty = float(row.dust_qty or 0.0) + float(dust_delta)
            row.filled_qty = filled_qty
            row.last_status = status
            self.executor.pending_fills.upsert(row)
            logger.info(
                "%s | fill confirmado | +%s @ %.4f | acumulado=%s/%s | status=%s",
                row.symbol,
                f"{delta:g}",
                fill_price,
                f"{filled_qty:g}",
                f"{row.qty:g}",
                status,
            )
            if row.is_close and row.side.lower() == "sell":
                self._reconcile_book_qty_from_broker(row.symbol)
            elif row.side.lower() == "buy" and not row.is_close:
                self._on_buy_filled(row.symbol)
        terminal_dead = any(token in status.lower() for token in ("cancel", "expir", "done_for_day"))
        if kind == "filled":
            self._finalize_resting_fill(row, fill_price, filled_qty)
            self.executor.pending_fills.remove(row.order_id)
            return
        if kind == "failed" or (kind == "partial" and terminal_dead):
            self._finalize_resting_failed(row, status, filled_qty=filled_qty, fill_price=fill_price)
            self.executor.pending_fills.remove(row.order_id)

    def _sweep_dust_positions(self) -> None:
        """Cierra en el libro qty residual bajo umbral (no monitorear SL/TP/trailing)."""
        for pos in list(self.executor.position_book.list()):
            ref = float(pos.opened_qty or pos.qty)
            qty = abs(float(pos.qty))
            if not is_dust_qty(self.settings, pos.symbol, qty, ref):
                continue
            logger.info(
                "%s | polvo en libro al arranque | qty=%s umbral=%s — se cierra localmente",
                pos.symbol,
                f"{qty:g}",
                f"{dust_threshold_for(self.settings, pos.symbol, ref):g}",
            )
            self.executor.position_book.close(pos.symbol)

    def _reconcile_book_qty_from_broker(self, symbol: str) -> float:
        """Alinea qty del libro local con GET /v2/positions (fuente de verdad en live)."""
        if self.executor.dry_run:
            tracked = self.executor.position_book.get(symbol)
            return abs(float(tracked.qty)) if tracked is not None else 0.0
        try:
            broker_pos = self.executor.get_position(symbol)
        except Exception as exc:
            log_caught(logger, "reconcile_book_qty_failed", exc, symbol=symbol)
            tracked = self.executor.position_book.get(symbol)
            return abs(float(tracked.qty)) if tracked is not None else 0.0
        tracked = self.executor.position_book.get(symbol)
        if broker_pos is None:
            if tracked is not None:
                self.executor.position_book.close(symbol)
            return 0.0
        broker_qty = abs(float(broker_pos.qty))
        ref_qty = float(tracked.opened_qty or tracked.qty) if tracked else broker_qty
        eff_broker = effective_qty(self.settings, symbol, broker_qty, ref_qty)
        if eff_broker <= 0:
            if tracked is not None:
                self.executor.position_book.close(symbol)
            return 0.0
        if tracked is None:
            return eff_broker
        local_qty = abs(float(tracked.qty))
        if is_dust_qty(self.settings, symbol, local_qty, ref_qty):
            self.executor.position_book.close(symbol)
            return eff_broker
        if abs(local_qty - eff_broker) > 1e-6:
            logger.warning(
                "%s | libro qty=%s broker=%s — alineando libro al broker",
                symbol,
                f"{local_qty:g}",
                f"{eff_broker:g}",
            )
            tracked.qty = eff_broker if tracked.qty >= 0 else -eff_broker
            self.executor.position_book.save()
        return eff_broker

    def _notify_resting_close_outcome(
        self,
        row: RestingOrder,
        fill_price: float,
        filled_qty: float,
        *,
        order_status: str = "filled",
    ) -> None:
        sold_qty = min(float(filled_qty), float(row.qty)) if filled_qty > 0 else float(row.qty)
        price = fill_price if fill_price > 0 else row.signal_price
        entry = float(row.entry_price or price)
        ref_qty = float(row.qty)
        remaining_broker = self._reconcile_book_qty_from_broker(row.symbol)
        dust_total = float(row.dust_qty or 0.0)
        remaining_effective = effective_qty(self.settings, row.symbol, remaining_broker, ref_qty)
        is_partial = remaining_effective > 1e-8 and sold_qty + 1e-8 < float(row.qty)
        event_id = row.event_id or self._event_id(
            row.symbol, "close", row.reason, signal_ts=f"{entry:.4f}"
        )
        if sold_qty <= 1e-8:
            return
        snap = compute_pnl(row.symbol, sold_qty, entry, price, PnLEvent.CLOSED)
        self.reporter.emit(snap)
        if is_partial:
            self.notifier.notify_partial_close(
                row.symbol,
                sold_qty,
                remaining_effective,
                entry,
                price,
                snap.pnl_abs,
                snap.pnl_pct,
                row.reason,
                order_status,
                float(row.qty),
                self.executor.dry_run,
                event_id=event_id,
            )
            return
        self._note_position_closed(row.symbol, row.reason)
        self.notifier.notify_closed(
            row.symbol,
            sold_qty,
            entry,
            price,
            snap.pnl_abs,
            snap.pnl_pct,
            row.reason,
            self.executor.dry_run,
            event_id=event_id,
            dust_qty=dust_total,
        )

    def _finalize_resting_fill(self, row: RestingOrder, fill_price: float, filled_qty: float) -> None:
        if row.is_close:
            self._notify_resting_close_outcome(row, fill_price, filled_qty, order_status="filled")
            return
        qty = filled_qty if filled_qty > 0 else row.qty
        price = fill_price if fill_price > 0 else row.signal_price
        self._note_breakout_entry(row.symbol)
        self.reporter.emit(compute_pnl(row.symbol, qty, price, price, PnLEvent.OPENED))
        self.notifier.notify_opened(
            row.symbol,
            row.side,
            qty,
            price,
            row.reason,
            self.executor.dry_run,
            stop_price=row.stop_price,
            take_profit_price=row.take_profit_price,
            stop_pct=row.stop_pct,
            take_profit_pct=row.take_profit_pct,
            event_id=row.event_id or self._event_id(row.symbol, "open", row.reason),
        )
        self._on_buy_filled(row.symbol)

    def _finalize_resting_failed(
        self,
        row: RestingOrder,
        status: str,
        *,
        filled_qty: float = 0.0,
        fill_price: float = 0.0,
    ) -> None:
        if filled_qty > 0 and row.is_close:
            self._notify_resting_close_outcome(
                row, fill_price, filled_qty, order_status=status or "canceled"
            )
        elif filled_qty > 0:
            self._finalize_resting_fill(row, fill_price, filled_qty)
        remaining = max(0.0, float(row.qty) - float(filled_qty))
        still_open = row.is_close and (
            effective_qty(
                self.settings,
                row.symbol,
                self._reconcile_book_qty_from_broker(row.symbol),
                float(row.qty),
            )
            > 1e-8
        )
        if (not row.is_close) and filled_qty <= 0:
            self.breakout_state.clear_open(row.symbol)
        logger.warning(
            "%s | orden %s sin completar | status=%s | filled=%s restante=%s | posicion_abierta=%s",
            row.symbol,
            row.order_id,
            status,
            f"{filled_qty:g}",
            f"{remaining:g}",
            still_open,
        )
        if filled_qty > 1e-8 and row.is_close:
            return
        self.notifier.notify_order_unfilled(
            row.symbol,
            row.side,
            row.qty,
            status,
            remaining_open=bool(still_open or remaining > 1e-8),
            filled_qty=filled_qty,
            event_id=self._event_id(
                row.symbol,
                "unfilled",
                row.reason,
                signal_ts=row.event_id or row.order_id,
            ),
        )

    def _retry_limits(self, reason: str) -> tuple[int, float]:
        if reason == ExitReason.STOP_LOSS.value:
            return (
                int(self.settings.order_retry_max_sl),
                float(self.settings.order_retry_timeout_sl_seconds),
            )
        return (
            int(self.settings.order_retry_max),
            float(self.settings.order_retry_timeout_seconds),
        )

    def _event_id(
        self,
        symbol: str,
        kind: str,
        operation: str,
        *,
        tracked=None,
        signal_ts: str = "",
    ) -> str:
        ts = str(signal_ts or "").strip()
        if not ts and tracked is not None:
            ts = str(getattr(tracked, "opened_at", "") or "").strip()
        if not ts:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return make_event_id(symbol, kind, ts, operation)

    def _maybe_notify_fractional_wide(
        self,
        result: OrderSubmitResult | None,
        symbol: str,
        side: str,
        qty: float,
        event_id: str,
    ) -> None:
        if result is None or not result.accepted or not result.wide_spread_market:
            return
        spread = float(result.spread_pct or 0.0)
        self.notifier.notify_fractional_wide_spread(
            symbol,
            side,
            qty,
            spread,
            event_id=self._event_id(symbol, "frac_spread", side, signal_ts=event_id),
        )

    def _log_retry_slippage(self, pending: PendingExecution, fill_price: float) -> None:
        if pending.attempts <= 1:
            return
        signal_price = float(pending.signal_price or 0.0)
        fill = float(fill_price or 0.0)
        if signal_price <= 0 or fill <= 0:
            return
        qty = float(pending.qty)
        if pending.side.lower() == "buy":
            slip_abs = (fill - signal_price) * qty
            slip_pct = (fill / signal_price - 1.0) * 100.0
        else:
            slip_abs = (signal_price - fill) * qty
            slip_pct = (1.0 - fill / signal_price) * 100.0
        logger.info(
            "%s | slippage post-reintento | señal=%.4f fill=%.4f | %+.4f%% | %+.2f USD | "
            "intentos=%s motivo=%s",
            pending.symbol,
            signal_price,
            fill,
            slip_pct,
            slip_abs,
            pending.attempts,
            pending.reason,
        )

    def _has_pending_close(self, symbol: str) -> bool:
        key = str(symbol).upper()
        with self._pending_lock:
            pending = self._pending.get(key)
            return pending is not None and pending.is_close

    def _queue_execution(self, pending: PendingExecution) -> None:
        now = time.monotonic()
        pending.first_at = pending.first_at or now
        pending.next_at = now + float(self.settings.order_retry_backoff_seconds)
        if pending.signal_price <= 0:
            pending.signal_price = float(pending.last_price or 0.0)
        if not pending.event_id:
            kind = "close" if pending.is_close else "open"
            pending.event_id = self._event_id(
                pending.symbol,
                kind,
                pending.reason,
                signal_ts="",
            )
        key = pending.symbol.upper()
        with self._pending_lock:
            current = self._pending.get(key)
            if (
                current is not None
                and current.side == pending.side
                and current.is_close == pending.is_close
            ):
                current.last_error = pending.last_error
                current.last_price = pending.last_price
                return
            self._pending[key] = pending
        logger.warning(
            "%s | orden en cola de reintento | %s qty=%s | %s",
            pending.symbol,
            pending.side,
            pending.qty,
            pending.last_error or "rechazada",
        )

    def _flush_pending_orders(self, only_symbol: str | None = None) -> None:
        now = time.monotonic()
        with self._pending_lock:
            items = list(self._pending.items())
        for key, pending in items:
            if only_symbol and key != only_symbol.upper():
                continue
            if now < pending.next_at:
                continue
            self._retry_pending(pending)

    def _retry_pending(self, pending: PendingExecution) -> None:
        settings = self.settings
        now = time.monotonic()
        elapsed = now - pending.first_at
        retry_max, retry_timeout = self._retry_limits(pending.reason)
        if pending.attempts >= retry_max or elapsed >= retry_timeout:
            self._expire_pending(pending)
            return
        try:
            try:
                self.executor.cancel_open_orders(pending.symbol)
            except Exception:
                pass
            bid, ask, spread = self._live_quote(pending.symbol, None)
            last = self.latest_price(pending.symbol, pending.last_price)
            pending.attempts += 1
            pending.last_price = last
            side = OrderSide.BUY if pending.side.lower() == "buy" else OrderSide.SELL
            if pending.is_close:
                result = self.executor.close_position(
                    pending.symbol,
                    price=last,
                    reason=pending.reason,
                    bid=bid,
                    ask=ask,
                    spread_pct=spread,
                    limit_spread_pct=settings.order_limit_spread_pct,
                    attempt=pending.attempts,
                )
                if result is None:
                    with self._pending_lock:
                        self._pending.pop(pending.symbol.upper(), None)
                    return
            else:
                result = self.executor.submit_smart_order(
                    pending.symbol,
                    pending.qty,
                    side,
                    price=last,
                    entry_price=pending.entry_price,
                    reason=pending.reason,
                    stop_price=pending.stop_price,
                    take_profit_price=pending.take_profit_price,
                    stop_pct=pending.stop_pct,
                    take_profit_pct=pending.take_profit_pct,
                    bid=bid,
                    ask=ask,
                    spread_pct=spread,
                    limit_spread_pct=settings.order_limit_spread_pct,
                    attempt=pending.attempts,
                )
            if result.accepted:
                with self._pending_lock:
                    self._pending.pop(pending.symbol.upper(), None)
                if result.filled:
                    self._notify_pending_success(pending, result, last)
                else:
                    self._watch_resting(
                        result,
                        symbol=pending.symbol,
                        side=pending.side,
                        qty=pending.qty,
                        reason=pending.reason,
                        is_close=pending.is_close,
                        event_id=pending.event_id,
                        entry_price=pending.entry_price,
                        signal_price=pending.signal_price or pending.last_price,
                        stop_price=pending.stop_price,
                        take_profit_price=pending.take_profit_price,
                        stop_pct=pending.stop_pct,
                        take_profit_pct=pending.take_profit_pct,
                    )
                return
            pending.last_error = result.broker_detail or "rechazada"
            backoff = min(
                8.0,
                float(settings.order_retry_backoff_seconds) * (2 ** max(0, pending.attempts - 1)),
            )
            pending.next_at = time.monotonic() + backoff
        except ValidationError as exc:
            pending.last_error = str(exc)
            self._expire_pending(pending)
        except Exception as exc:
            pending.last_error = str(exc)
            backoff = min(
                8.0,
                float(settings.order_retry_backoff_seconds) * (2 ** max(0, pending.attempts - 1)),
            )
            pending.next_at = time.monotonic() + backoff

    def _expire_pending(self, pending: PendingExecution) -> None:
        with self._pending_lock:
            self._pending.pop(pending.symbol.upper(), None)
        if pending.is_close and is_insufficient_balance_reject(pending.last_error):
            # Evita re-disparar SL cada tick tras agotar reintentos por polvo de qty.
            self._close_cooldown_until[pending.symbol.upper()] = time.monotonic() + 60.0
        if pending.alerted:
            return
        pending.alerted = True
        _, timeout_seconds = self._retry_limits(pending.reason)
        logger.error(
            "%s | orden agotó reintentos | %s qty=%s | %s | expuesta=%s | timeout=%ss motivo=%s",
            pending.symbol,
            pending.side,
            pending.qty,
            pending.last_error,
            pending.is_close,
            timeout_seconds,
            pending.reason,
        )
        if self.notifier.enabled:
            self.notifier.notify_order_timeout(
                pending.symbol,
                pending.side,
                pending.qty,
                pending.last_error,
                exposed=pending.is_close,
                attempts=pending.attempts,
                timeout_seconds=timeout_seconds,
                event_id=self._event_id(
                    pending.symbol,
                    "timeout",
                    pending.reason,
                    signal_ts=pending.event_id,
                ),
            )

    def _notify_pending_success(
        self, pending: PendingExecution, result: OrderSubmitResult, last_price: float
    ) -> None:
        fill_price = float(result.fill_price or last_price)
        self._log_retry_slippage(pending, fill_price)
        self._maybe_notify_fractional_wide(
            result, pending.symbol, pending.side, pending.qty, pending.event_id
        )
        if not pending.is_close:
            self._note_breakout_entry(pending.symbol)
        if pending.is_close:
            filled_qty = float(result.filled_qty or pending.qty)
            entry = float(pending.entry_price or fill_price)
            snap = compute_pnl(
                pending.symbol, filled_qty, entry, fill_price, PnLEvent.CLOSED
            )
            self.reporter.emit(snap)
            self._reconcile_book_qty_from_broker(pending.symbol)
            self._note_position_closed(pending.symbol, pending.reason)
            self.notifier.notify_closed(
                pending.symbol,
                filled_qty,
                entry,
                fill_price,
                snap.pnl_abs,
                snap.pnl_pct,
                pending.reason,
                self.executor.dry_run,
                event_id=pending.event_id
                or self._event_id(pending.symbol, "close", pending.reason),
            )
            return
        self.reporter.emit(
            compute_pnl(pending.symbol, pending.qty, fill_price, fill_price, PnLEvent.OPENED)
        )
        self.notifier.notify_opened(
            pending.symbol,
            pending.side,
            pending.qty,
            fill_price,
            pending.reason,
            self.executor.dry_run,
            stop_price=pending.stop_price,
            take_profit_price=pending.take_profit_price,
            stop_pct=pending.stop_pct,
            take_profit_pct=pending.take_profit_pct,
            event_id=pending.event_id
            or self._event_id(pending.symbol, "open", pending.reason),
        )
        self._on_buy_filled(pending.symbol)

    def _on_buy_filled(self, symbol: str, atr_value: float | None = None) -> None:
        """Hook post-compra: peak de trailing desde el fill + TP dinámico."""
        tracked = self.executor.position_book.get(symbol)
        if tracked is not None:
            entry = float(tracked.avg_entry_price or 0.0)
            if entry > 0:
                tracked.peak_price = max(float(tracked.peak_price or 0.0), entry)
                self.executor.position_book.save()
            if self._stream is not None and entry > 0:
                self._stream.reset_peak(symbol, entry)
        if not self.settings.dynamic_tp_enabled:
            return
        if tracked is None:
            return
        atr = atr_value if atr_value is not None else self._atr_cache.get(str(symbol).upper())
        self.dynamic_tp.register_entry(self, symbol, tracked, atr)

    def apply_tick_trailing(self, symbol: str, tick_price: float) -> None:
        """Trailing + SL/TP en cada tick del WebSocket (sin pedir velas REST)."""
        if not self._running or self.control.is_paused():
            return
        if tick_price <= 0:
            return
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            tracked = self.executor.position_book.get(symbol)
            if tracked is None:
                return
            if tracked.dry_run and not self.executor.dry_run:
                return
            clock = self._cached_clock()
            if not is_symbol_tradable(symbol, clock):
                return
            qty = float(tracked.qty)
            entry = float(tracked.avg_entry_price)
            peak = _peak_since_entry(tracked, tick_price)
            atr_value = self._atr_cache.get(symbol.upper())
            if self.settings.dynamic_tp_enabled:
                bid, ask, spread = self._live_quote(symbol, None)
                if self.dynamic_tp.on_tick(
                    self, symbol, tick_price, bid, ask, spread, tracked
                ):
                    return
            self._ratchet_and_maybe_exit(symbol, qty, entry, tick_price, peak, clock, atr_value)
        except Exception as exc:
            log_caught(logger, "tick_trailing_failed", exc, symbol=symbol)
        finally:
            self._tick_lock.release()
        self._flush_pending_orders(symbol)

    def _remember_atr(self, symbol: str, atr_value: float | None) -> float | None:
        if atr_value and atr_value > 0:
            self._atr_cache[str(symbol).upper()] = float(atr_value)
            return float(atr_value)
        return self._atr_cache.get(str(symbol).upper())

    def _daily_loss_halted(self, account: AccountSnapshot) -> bool:
        limit_pct = float(self.settings.daily_loss_limit_pct or 0.0)
        if limit_pct <= 0:
            return False
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lost = self.executor.journal.realized_loss_sum_on_utc_date(day)
        cap = float(account.equity) * limit_pct
        if lost < cap:
            return False
        if self._daily_halt_day != day:
            self._daily_halt_day = day
            logger.error(
                "Freno diario activo | perdidas UTC %s = $%.2f >= %.2f%% equity ($%.2f)",
                day,
                lost,
                limit_pct * 100,
                cap,
            )
            if self.notifier.enabled:
                self.notifier._send(
                    "🛡️ Freno diario de protección — no es un error\n"
                    f"Pérdidas realizadas hoy (UTC): ${lost:,.2f}\n"
                    f"Límite: {limit_pct * 100:.2f}% del equity (${cap:,.2f})\n"
                    "No se abren posiciones nuevas hasta mañana. "
                    "Las abiertas siguen con su SL/TP."
                )
        return True

    def _note_breakout_entry(self, symbol: str) -> None:
        self.breakout_state.mark_open(symbol)

    def _note_position_closed(self, symbol: str, reason: str) -> None:
        if reason == ExitReason.STOP_LOSS.value:
            bars_required = self.signal_filters.cooldown_bars_for(symbol)
            if self.breakout_state.arm_cooldown_if_breakout(symbol, bars_required):
                return
        self.breakout_state.clear_open(symbol)

    def _notify_signal_filtered(self, symbol: str, signal: Signal, reason: str) -> None:
        if not self.settings.telegram_notify_filtered or not self.notifier.enabled:
            return
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
        self.notifier.notify_signal_filtered(
            symbol,
            signal.value,
            reason,
            event_id=self._event_id(symbol, "filtered", signal.value, signal_ts=hour),
        )

    def _ratchet_and_maybe_exit(
        self,
        symbol: str,
        qty: float,
        entry: float,
        last_price: float,
        peak_price: float,
        clock: MarketClockView,
        atr_value: float | None = None,
    ) -> None:
        tracked = self.executor.position_book.get(symbol)
        if tracked is None or qty <= 0 or entry <= 0:
            return
        self.executor.position_book.update_mark(symbol, last_price)
        current_sl = float(getattr(tracked, "stop_price", 0.0) or 0.0)
        new_sl, trail_src = self.risk.trailing_stop(
            entry,
            qty,
            peak_price,
            current_sl,
            atr_value,
            symbol=symbol,
            activate_pct=TRAIL_ACTIVATE_PCT,
            offset_pct=TRAIL_OFFSET_PCT,
        )
        if new_sl is None and atr_value is None:
            new_sl = _ratchet_trailing_stop(tracked, entry, qty, peak_price)
            trail_src = "pct"
        if new_sl is not None:
            if last_price > 0 and new_sl >= last_price:
                logger.warning(
                    "%s | trailing ignorado — SL %.4f >= last %.4f (peak inflado o no realizado)",
                    symbol,
                    new_sl,
                    last_price,
                )
                new_sl = None
                trail_src = "ignored"
        if new_sl is not None:
            first_be = trail_src == "breakeven" and not bool(
                getattr(tracked, "breakeven_notified", False)
            )
            first_trail = trail_src in {"atr", "pct"} and not bool(
                getattr(tracked, "trailing_notified", False)
            )
            old_sl = float(tracked.stop_price)
            tracked.stop_price = new_sl
            if first_be:
                tracked.breakeven_notified = True
            if first_trail:
                tracked.trailing_notified = True
            self.executor.position_book.save()
            gain_pct = (peak_price / entry - 1.0) * 100.0
            trail_off = (atr_value * self.settings.atr_trailing_mult) if atr_value else peak_price * TRAIL_OFFSET_PCT
            logger.info(
                "%s | trailing stop | ATR=%s fuente=%s offset=%.4f | peak=%.4f last=%.4f "
                "pnl=+%.2f%% SL %.4f -> %.4f",
                symbol,
                f"{atr_value:.4f}" if atr_value else "n/a",
                trail_src,
                trail_off,
                peak_price,
                last_price,
                gain_pct,
                old_sl,
                new_sl,
            )
            if first_be:
                self.notifier.notify_breakeven_locked(
                    symbol,
                    entry,
                    peak_price,
                    new_sl,
                    gain_pct,
                    event_id=self._event_id(symbol, "breakeven", "lock", tracked=tracked),
                )
            elif first_trail:
                self.notifier.notify_trailing_activated(
                    symbol,
                    entry,
                    peak_price,
                    gain_pct,
                    event_id=self._event_id(symbol, "trailing", "activate", tracked=tracked),
                )

        stored_sl = float(getattr(tracked, "stop_price", 0.0) or 0.0)
        stored_tp = float(getattr(tracked, "take_profit_price", 0.0) or 0.0)
        use_book_tp = not self.settings.dynamic_tp_enabled
        reason = ExitReason.NONE
        if stored_sl > 0 or (use_book_tp and stored_tp > 0):
            if stored_sl > 0 and last_price <= stored_sl:
                reason = ExitReason.STOP_LOSS
                logger.info(
                    "Salida stop_loss | %s last=%.4f SL=%.4f (libro)",
                    symbol,
                    last_price,
                    stored_sl,
                )
            elif use_book_tp and stored_tp > 0 and last_price >= stored_tp:
                reason = ExitReason.TAKE_PROFIT
                logger.info(
                    "Salida take_profit | %s last=%.4f TP=%.4f (libro)",
                    symbol,
                    last_price,
                    stored_tp,
                )
        else:
            reason = self.risk.evaluate_exit(entry, qty, last_price, atr_value, symbol=symbol)
            if not use_book_tp and reason is ExitReason.TAKE_PROFIT:
                reason = ExitReason.NONE
        if reason is ExitReason.NONE:
            return
        if not is_symbol_tradable(symbol, clock):
            logger.info("%s | %s detectado pero mercado cerrado para acciones", symbol, reason.value)
            return
        if self.executor.pending_fills.has_close(symbol):
            logger.info(
                "%s | %s detectado pero hay orden de cierre en vuelo — no se reenvía",
                symbol,
                reason.value,
            )
            return
        if self._has_pending_close(symbol):
            logger.info(
                "%s | %s detectado pero cierre ya en cola de reintento — no se reenvía",
                symbol,
                reason.value,
            )
            return
        self._close_and_report(symbol, qty, entry, last_price, reason.value)

    def _mark_all_positions(self) -> None:
        """Monitorea SL/TP de todas las posiciones abiertas (libro local o broker)."""
        clock = self.client.get_market_clock()
        positions = positions_by_symbol(self.executor.list_positions())
        for symbol in list(positions):
            if self.control.is_paused():
                return
            try:
                self._mark_to_market(symbol, positions, clock)
            except (ValidationError, RateLimitError) as exc:
                log_caught(logger, "mark_to_market_blocked", exc, symbol=symbol)
            except Exception as exc:
                log_caught(logger, "mark_to_market_failed", exc, symbol=symbol)

    def _mark_to_market(self, symbol: str, positions: dict, clock: MarketClockView) -> None:
        position = positions.get(normalize_symbol(symbol)) or positions.get(symbol)
        if position is None:
            return

        qty = float(position.qty)
        entry = float(position.avg_entry_price)
        md_symbol = normalize_symbol(symbol)
        stream_px = self._stream.last_price(md_symbol) if self._stream is not None else None
        cached_atr = self._atr_cache.get(str(symbol).upper()) or self._atr_cache.get(md_symbol.upper())
        # Con WS vivo + ATR en cache: mark sin REST (evita quemar el bucket data).
        if stream_px and stream_px > 0 and cached_atr and cached_atr > 0:
            last_price = float(stream_px)
            atr_value = float(cached_atr)
        else:
            bars = self.market_data.get_bars(
                md_symbol,
                self.settings.crypto_bar_timeframe if is_crypto_symbol(symbol) else self.settings.bar_timeframe,
                self.settings.lookback_bars,
            )
            fallback = float(bars["close"].iloc[-1]) if not bars.empty else entry
            tape = self.live_tape(md_symbol, fallback_price=fallback)
            rest_last = tape.last_price if tape else fallback
            last_price = self.latest_price(symbol, rest_last)
            atr_value = last_atr(bars, self.settings.atr_period) if not bars.empty else None
            atr_value = self._remember_atr(symbol, atr_value)

        trail_tf = _trailing_bar_timeframe(self.settings, symbol)
        tracked = self.executor.position_book.get(symbol)
        peak_price = _peak_since_entry(tracked, last_price) if tracked is not None else last_price

        ref_qty = float(tracked.opened_qty or tracked.qty) if tracked is not None else abs(qty)
        if effective_qty(self.settings, symbol, abs(qty), ref_qty) > 1e-8:
            self.reporter.emit(compute_pnl(symbol, qty, entry, last_price, PnLEvent.UPDATED))
        levels = self.risk.protective_levels(entry, qty, last_price, atr_value, symbol=symbol)
        logger.debug(
            "%s | mark=%.4f SL=%.4f TP=%.4f | ATR=%s fuente=%s | trail_tf=%s peak=%.4f",
            symbol,
            last_price,
            levels.stop_price,
            levels.take_profit_price,
            f"{atr_value:.4f}" if atr_value else "n/a",
            "atr" if levels.used_atr else "pct",
            trail_tf,
            peak_price,
        )
        before = self.executor.position_book.get(symbol)
        self._ratchet_and_maybe_exit(symbol, qty, entry, last_price, peak_price, clock, atr_value)
        after = self.executor.position_book.get(symbol)
        if before is not None and after is None:
            positions.pop(symbol, None)

    def _execute_signal(
        self,
        symbol: str,
        account: AccountSnapshot | None,
        positions: dict,
        bars: pd.DataFrame,
        tape: LiveTape | None,
        last_price: float,
        position,
        signal: Signal,
        atr_sl_mult: float | None = None,
        entry_strategy: str | None = None,
    ) -> None:
        qty = float(position.qty) if position is not None else 0.0
        atr_value = self._remember_atr(symbol, last_atr(bars, self.settings.atr_period))
        bid, ask, spread = self._live_quote(symbol, tape)
        tape_spread = spread if spread is not None else (tape.spread_pct if tape else None)
        strategy_key = entry_strategy
        if strategy_key is None:
            last_strategy = getattr(self.strategy, "last_strategy", None)
            strategy_key = last_strategy.value if last_strategy is not None else None
        ctx = StrategyContext(
            symbol=symbol,
            bars=bars,
            has_long_position=qty > 0,
            has_short_position=qty < 0,
            last_price=last_price,
            spread_pct=tape_spread,
            momentum_pct=momentum_pct(bars["close"], self.settings.momentum_bars),
            atr=atr_value,
            entry_strategy=strategy_key,
        )
        logger.info(
            "%s | decisión señal=%s | precio=%.4f | spread=%s | pos_qty=%s | atr=%s",
            symbol,
            signal.value,
            last_price,
            f"{tape_spread:.4%}" if tape_spread is not None else "n/a",
            f"{qty:g}",
            f"{atr_value:.4f}" if atr_value else "n/a",
        )

        ok, flow_reason = self.flow.confirm(signal, ctx)
        if not ok:
            logger.info("%s | señal=%s rechazada por flujo: %s", symbol, signal.value, flow_reason)
            if "spread" in flow_reason.lower():
                self._queue_signal_after_reject(
                    symbol, signal, position, qty, last_price, account, positions, atr_value, flow_reason
                )
            return

        if signal is Signal.BUY:
            if account is None:
                logger.warning(
                    "%s | BUY bloqueado — no hay snapshot de cuenta ni cache reciente para dimensionar la orden",
                    symbol,
                )
                return
            if self._daily_loss_halted(account):
                logger.info("%s | BUY bloqueado — freno de pérdida diaria", symbol)
                return

        decision = self.risk.evaluate(
            signal=signal,
            symbol=symbol,
            last_price=last_price,
            account=account,
            open_positions=len(positions),
            has_position=position is not None,
            atr_value=atr_value,
            atr_sl_mult=atr_sl_mult,
        )
        if not decision.approved:
            logger.info("%s | señal=%s bloqueada: %s", symbol, signal.value, decision.reason)
            return

        # --- Entrada: compra + registro SL/TP en libro ---
        if signal is Signal.BUY:
            if self.executor.pending_fills.has_open(symbol):
                logger.info("%s | BUY omitido — ya hay orden de entrada en vuelo", symbol)
                return
            levels = self.risk.protective_levels(
                last_price,
                decision.qty,
                last_price,
                atr_value,
                symbol=symbol,
                atr_sl_mult=atr_sl_mult,
            )
            sl_mult_log = (
                self.settings.crypto_atr_sl_mult
                if is_crypto_symbol(symbol)
                else self.settings.atr_sl_mult
            )
            tp_mult_log = (
                self.settings.crypto_atr_tp_mult
                if is_crypto_symbol(symbol)
                else self.settings.atr_tp_mult
            )
            trail_mult_log = (
                self.settings.crypto_atr_trailing_mult
                if is_crypto_symbol(symbol)
                else self.settings.atr_trailing_mult
            )
            logger.info(
                "%s | ATR=%s (period=%s) fuente=%s | SL=%.4f (%.2f%%, %.2fx) | "
                "TP=%.4f (%.2f%%, %.2fx) | trailing=%s (%.2fx)",
                symbol,
                f"{atr_value:.4f}" if atr_value else "n/a",
                self.settings.atr_period,
                "atr" if levels.used_atr else "pct_fallback",
                levels.stop_price,
                levels.stop_pct * 100,
                atr_sl_mult if atr_sl_mult is not None else sl_mult_log,
                levels.take_profit_price,
                levels.take_profit_pct * 100,
                tp_mult_log,
                f"{levels.trailing_offset:.4f}" if levels.trailing_offset else "pct_fallback",
                trail_mult_log,
            )
            audit("signal_buy", "allow", symbol=symbol, qty=decision.qty)
            buy_event_id = self._event_id(symbol, "open", "signal_6m")
            result = self.executor.submit_smart_order(
                symbol,
                decision.qty,
                OrderSide.BUY,
                price=last_price,
                reason="signal_6m",
                stop_price=levels.stop_price,
                take_profit_price=levels.take_profit_price,
                stop_pct=levels.stop_pct,
                take_profit_pct=levels.take_profit_pct,
                bid=bid,
                ask=ask,
                spread_pct=spread,
                limit_spread_pct=self.settings.order_limit_spread_pct,
                attempt=1,
            )
            if result.accepted:
                self._note_breakout_entry(symbol)
            if not result.accepted:
                self._queue_execution(
                    PendingExecution(
                        symbol=symbol,
                        side="buy",
                        qty=decision.qty,
                        reason="signal_6m",
                        last_price=last_price,
                        is_close=False,
                        attempts=1,
                        first_at=time.monotonic(),
                        last_error=result.broker_detail,
                        stop_price=levels.stop_price,
                        take_profit_price=levels.take_profit_price,
                        stop_pct=levels.stop_pct,
                        take_profit_pct=levels.take_profit_pct,
                        signal_price=last_price,
                        event_id=buy_event_id,
                    )
                )
                return
            if not result.filled:
                self._watch_resting(
                    result,
                    symbol=symbol,
                    side="buy",
                    qty=decision.qty,
                    reason="signal_6m",
                    is_close=False,
                    event_id=buy_event_id,
                    entry_price=last_price,
                    signal_price=last_price,
                    stop_price=levels.stop_price,
                    take_profit_price=levels.take_profit_price,
                    stop_pct=levels.stop_pct,
                    take_profit_pct=levels.take_profit_pct,
                )
                return
            positions[symbol] = self.executor.get_position(symbol)
            self.reporter.emit(
                compute_pnl(symbol, decision.qty, last_price, last_price, PnLEvent.OPENED)
            )
            self._maybe_notify_fractional_wide(
                result, symbol, "buy", decision.qty, buy_event_id
            )
            self.notifier.notify_opened(
                symbol,
                "buy",
                decision.qty,
                last_price,
                "signal_6m",
                self.executor.dry_run,
                stop_price=levels.stop_price,
                take_profit_price=levels.take_profit_price,
                stop_pct=levels.stop_pct,
                take_profit_pct=levels.take_profit_pct,
                event_id=buy_event_id,
            )
            self._on_buy_filled(symbol, atr_value)
        # --- Salida por señal contraria ---
        elif signal is Signal.SELL and position is not None:
            if self.executor.pending_fills.has_close(symbol):
                logger.info("%s | SELL omitido — ya hay orden de cierre en vuelo", symbol)
                return
            if self._has_pending_close(symbol):
                logger.info("%s | SELL omitido — cierre ya en cola de reintento", symbol)
                return
            closed = self._close_and_report(
                symbol, qty, float(position.avg_entry_price), last_price, "signal_6m"
            )
            if closed:
                positions.pop(symbol, None)

    def close_positions_on_mode_switch(self, new_mode_value: str) -> int:
        """
        Cierra posiciones del activo contrario al cambiar Cripto <-> Acciones.
        Evita riesgo cruzado entre clases de mercado.
        """
        if not self.settings.close_on_mode_switch:
            logger.info("CLOSE_ON_MODE_SWITCH=false — se mantienen posiciones abiertas")
            return 0

        keep_class = "crypto" if new_mode_value == "crypto" else "stock"
        closed = 0
        for pos in list(self.executor.list_positions()):
            symbol = normalize_symbol(str(pos.symbol))
            if asset_class_for(symbol) == keep_class:
                continue
            qty = float(pos.qty)
            tracked = self.executor.position_book.get(symbol)
            ref = float(tracked.opened_qty or tracked.qty) if tracked else abs(qty)
            if effective_qty(self.settings, symbol, abs(qty), ref) <= 1e-8:
                logger.info("%s | mode_switch omitido — residuo polvo broker=%s", symbol, qty)
                self.executor.position_book.close(symbol)
                continue
            entry = float(pos.avg_entry_price)
            md_symbol = normalize_symbol(symbol)
            bars = self.market_data.get_bars(
                md_symbol,
                self.settings.crypto_bar_timeframe if is_crypto_symbol(symbol) else "15Min",
                30,
            )
            fallback = float(bars["close"].iloc[-1]) if not bars.empty else entry
            tape = self.live_tape(md_symbol, fallback_price=fallback)
            last_price = tape.last_price if tape else fallback
            try:
                if self._close_and_report(symbol, qty, entry, last_price, "mode_switch"):
                    closed += 1
            except Exception as exc:
                log_caught(logger, "mode_switch_close_failed", exc, symbol=symbol)
        if closed:
            logger.info("Cambio de mercado: cerradas %s posiciones de clase cruzada", closed)
        return closed

    def _queue_signal_after_reject(
        self,
        symbol: str,
        signal: Signal,
        position,
        qty: float,
        last_price: float,
        account: AccountSnapshot | None,
        positions: dict,
        atr_value: float | None,
        flow_reason: str,
    ) -> None:
        if signal is Signal.SELL and position is not None:
            self._queue_execution(
                PendingExecution(
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    reason="signal_6m",
                    last_price=last_price,
                    entry_price=float(position.avg_entry_price),
                    is_close=True,
                    last_error=flow_reason,
                    signal_price=last_price,
                    event_id=self._event_id(
                        symbol,
                        "close",
                        "signal_6m",
                        tracked=self.executor.position_book.get(symbol),
                    ),
                )
            )
            return
        if signal is not Signal.BUY:
            return
        if account is None:
            logger.warning(
                "%s | BUY no encola reintento tras rechazo de flujo porque no hay snapshot de cuenta",
                symbol,
            )
            return
        decision = self.risk.evaluate(
            signal=signal,
            symbol=symbol,
            last_price=last_price,
            account=account,
            open_positions=len(positions),
            has_position=position is not None,
            atr_value=atr_value,
        )
        if not decision.approved:
            return
        levels = self.risk.protective_levels(
            last_price, decision.qty, last_price, atr_value, symbol=symbol
        )
        logger.info(
            "%s | cola BUY | ATR=%s fuente=%s | SL=%.4f (%.2f%%) TP=%.4f (%.2f%%) trailing=%s",
            symbol,
            f"{atr_value:.4f}" if atr_value else "n/a",
            "atr" if levels.used_atr else "pct_fallback",
            levels.stop_price,
            levels.stop_pct * 100,
            levels.take_profit_price,
            levels.take_profit_pct * 100,
            f"{levels.trailing_offset:.4f}" if levels.trailing_offset else "pct_fallback",
        )
        self._queue_execution(
            PendingExecution(
                symbol=symbol,
                side="buy",
                qty=decision.qty,
                reason="signal_6m",
                last_price=last_price,
                is_close=False,
                last_error=flow_reason,
                stop_price=levels.stop_price,
                take_profit_price=levels.take_profit_price,
                stop_pct=levels.stop_pct,
                take_profit_pct=levels.take_profit_pct,
                signal_price=last_price,
                event_id=self._event_id(symbol, "open", "signal_6m"),
            )
        )

    def _close_and_report(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        last_price: float,
        reason: str,
    ) -> bool:
        logger.info("%s | cerrando por %s @ %.4f (entry %.4f)", symbol, reason, last_price, entry_price)
        if self.executor.pending_fills.has_close(symbol):
            existing = self.executor.pending_fills.close_for(symbol)
            logger.info(
                "%s | cierre ya en vuelo | id=%s — no se reenvía",
                symbol,
                existing.order_id if existing else "—",
            )
            return False
        if self._has_pending_close(symbol):
            logger.info("%s | cierre ya en cola de reintento — no se reenvía", symbol)
            return False
        blocked_until = self._close_cooldown_until.get(symbol.upper(), 0.0)
        if blocked_until and time.monotonic() < blocked_until:
            logger.warning(
                "%s | cierre en cooldown tras rechazo de qty (%.0fs) — no se martillea Alpaca",
                symbol,
                max(0.0, blocked_until - time.monotonic()),
            )
            return False
        tracked = self.executor.position_book.get(symbol)
        signal_ts = str(getattr(tracked, "opened_at", "") or "") if tracked is not None else ""
        if not signal_ts:
            signal_ts = f"{float(entry_price):.4f}"
        event_id = self._event_id(symbol, "close", reason, signal_ts=signal_ts)
        tp_atr = None
        restore_dynamic_tp = False
        if self.settings.dynamic_tp_enabled:
            tp_row = self.dynamic_tp.store.get(symbol)
            if tp_row is not None:
                tp_atr = tp_row.atr_value
                restore_dynamic_tp = bool(tp_row.order_id)
                if restore_dynamic_tp and not self.dynamic_tp.cancel_for_symbol(self, symbol):
                    logger.warning("%s | cierre omitido — no se pudo cancelar el TP dinámico vigente", symbol)
                    return False
        bid, ask, spread = self._live_quote(symbol, None)
        result = self.executor.close_position(
            symbol,
            price=last_price,
            reason=reason,
            bid=bid,
            ask=ask,
            spread_pct=spread,
            limit_spread_pct=self.settings.order_limit_spread_pct,
            attempt=1,
        )
        if result is None:
            tracked = self.executor.position_book.get(symbol)
            if self.executor.dry_run:
                self.executor.journal.record(
                    symbol,
                    "sell",
                    qty,
                    last_price,
                    entry_price=entry_price,
                    reason=reason,
                    dry_run=True,
                    order_id="dry_run",
                )
                self.executor.position_book.close(symbol)
                self._note_position_closed(symbol, reason)
            elif tracked is not None and tracked.dry_run:
                logger.warning("%s | cierre omitido — fila dry-run huérfana, se quita del libro", symbol)
                self.executor.position_book.close(symbol)
                return False
            else:
                logger.warning("%s | close_position sin orden — no se notifica cierre", symbol)
                tracked = self.executor.position_book.get(symbol)
                if restore_dynamic_tp and tracked is not None:
                    self.dynamic_tp.register_entry(self, symbol, tracked, tp_atr)
                return False
        elif not result.accepted:
            tracked = self.executor.position_book.get(symbol)
            if restore_dynamic_tp and tracked is not None:
                self.dynamic_tp.register_entry(self, symbol, tracked, tp_atr)
            self._queue_execution(
                PendingExecution(
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    reason=reason,
                    last_price=last_price,
                    entry_price=entry_price,
                    is_close=True,
                    attempts=1,
                    first_at=time.monotonic(),
                    last_error=result.broker_detail,
                    signal_price=last_price,
                    event_id=event_id,
                )
            )
            return False
        self._close_cooldown_until.pop(symbol.upper(), None)
        if not result.filled:
            self._watch_resting(
                result,
                symbol=symbol,
                side="sell",
                qty=qty,
                reason=reason,
                is_close=True,
                event_id=event_id,
                entry_price=entry_price,
                signal_price=last_price,
            )
            return False
        fill_price = float(getattr(result, "fill_price", 0.0) or last_price) if result else last_price
        filled_qty = float(getattr(result, "filled_qty", 0.0) or qty)
        self._maybe_notify_fractional_wide(result, symbol, "sell", qty, event_id)
        remaining = self._reconcile_book_qty_from_broker(symbol)
        ref_qty = qty
        tracked = self.executor.position_book.get(symbol)
        if tracked is not None:
            ref_qty = float(tracked.opened_qty or qty)
        remaining_eff = effective_qty(self.settings, symbol, remaining, ref_qty)
        if remaining_eff > 1e-8 and filled_qty + 1e-8 < qty:
            snap = compute_pnl(symbol, filled_qty, entry_price, fill_price, PnLEvent.CLOSED)
            self.reporter.emit(snap)
            self.notifier.notify_partial_close(
                symbol,
                filled_qty,
                remaining_eff,
                entry_price,
                fill_price,
                snap.pnl_abs,
                snap.pnl_pct,
                reason,
                str(getattr(result, "broker_detail", "") or "filled"),
                qty,
                self.executor.dry_run,
                event_id=event_id,
            )
            return True
        snap = compute_pnl(symbol, filled_qty, entry_price, fill_price, PnLEvent.CLOSED)
        self.reporter.emit(snap)
        self._note_position_closed(symbol, reason)
        dust_note = max(0.0, remaining - remaining_eff) if remaining > remaining_eff else 0.0
        self.notifier.notify_closed(
            symbol,
            filled_qty,
            entry_price,
            fill_price,
            snap.pnl_abs,
            snap.pnl_pct,
            reason,
            self.executor.dry_run,
            event_id=event_id,
            dust_qty=dust_note,
        )
        return True

    def _apply_pause(self) -> None:
        if self._pause_applied:
            return
        cancelled = 0
        try:
            cancelled = self.executor.cancel_open_orders()
        except Exception as exc:
            log_caught(logger, "pause_cancel_failed", exc)
        logger.info("Bot detenido — canceladas %s ordenes pendientes", cancelled)
        if self.notifier.enabled:
            if not self.notifier.notify_bot_state(False, cancelled):
                logger.warning("Telegram: no se pudo enviar aviso de detencion")
        self._pause_applied = True

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while self._running and time.monotonic() < deadline:
            if self.control.is_paused():
                self._apply_pause()
                self.request_shutdown()
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._shutdown_event.wait(timeout=min(0.25, remaining)):
                break
