"""Motor de trading: tape en vivo → señal → riesgo/SL-TP → ejecución + P&L."""

from __future__ import annotations

import logging
import time
from typing import Callable

from alpaca.trading.enums import OrderSide
from alpaca.trading.models import Position

from bot.alpaca.client import AccountSnapshot, AlpacaClient
from bot.alpaca.execution import OrderExecutor
from bot.alpaca.market_data import MarketDataService
from bot.config import Settings
from bot.reporting.pnl import PerformanceReporter, PnLEvent, compute_pnl
from bot.risk.manager import RiskManager
from bot.risk.stops import ExitReason
from bot.strategy.base import Signal, Strategy, StrategyContext
from bot.strategy.indicators import last_atr, momentum_pct
from bot.strategy.price_flow import PriceFlowFilter
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, ValidationError

logger = logging.getLogger(__name__)


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
        self._running = False

    def run_once(self) -> None:
        clock = self.client.get_clock()
        account = self.client.snapshot_account()
        positions = {pos.symbol: pos for pos in self.executor.list_positions()}

        for symbol in self.settings.symbols:
            try:
                self._mark_to_market(symbol, positions)
            except (ValidationError, RateLimitError) as exc:
                log_caught(logger, "mark_to_market_blocked", exc, symbol=symbol)
            except Exception as exc:
                log_caught(logger, "mark_to_market_failed", exc, symbol=symbol)

        if not clock.is_open:
            logger.info("Mercado cerrado — se reporta P&L; no hay nuevas entradas")
            return

        positions = {pos.symbol: pos for pos in self.executor.list_positions()}
        for symbol in self.settings.symbols:
            try:
                self._process_symbol(symbol, account, positions)
            except (ValidationError, RateLimitError) as exc:
                log_caught(logger, "symbol_blocked", exc, symbol=symbol)
            except Exception as exc:
                log_caught(logger, "symbol_failed", exc, symbol=symbol)

    def run_loop(self, should_stop: Callable[[], bool] | None = None) -> None:
        self._running = True
        interval = self.settings.poll_interval_seconds
        logger.info(
            "Motor iniciado | estrategia=%s | símbolos=%s | intervalo=%ss | dry_run=%s | "
            "SL=%.2f%% TP=%.2f%%",
            self.strategy.name,
            ",".join(self.settings.symbols),
            interval,
            self.executor.dry_run,
            self.settings.stop_loss_pct * 100,
            self.settings.take_profit_pct * 100,
        )
        while self._running and not (should_stop and should_stop()):
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                log_caught(logger, "trading_cycle_failed", exc)
            elapsed = time.monotonic() - started
            remaining = max(0.0, interval - elapsed)
            if remaining:
                time.sleep(remaining)
        logger.info("Motor detenido")

    def stop(self) -> None:
        self._running = False

    def _mark_to_market(self, symbol: str, positions: dict[str, Position]) -> None:
        position = positions.get(symbol)
        if position is None:
            return

        qty = float(position.qty)
        entry = float(position.avg_entry_price)
        bars = self.market_data.get_bars(
            symbol, self.settings.bar_timeframe, self.settings.lookback_bars
        )
        fallback = float(bars["close"].iloc[-1]) if not bars.empty else entry
        tape = self.market_data.get_live_tape(symbol, fallback_price=fallback)
        last_price = tape.last_price if tape else fallback
        atr_value = last_atr(bars, self.settings.atr_period) if not bars.empty else None

        self.reporter.emit(compute_pnl(symbol, qty, entry, last_price, PnLEvent.UPDATED))
        levels = self.risk.protective_levels(entry, qty, last_price, atr_value)
        logger.info(
            "%s | niveles dinámicos SL=%.4f (%.2f%%) TP=%.4f (%.2f%%) tape=%s",
            symbol,
            levels.stop_price,
            levels.stop_pct * 100,
            levels.take_profit_price,
            levels.take_profit_pct * 100,
            tape.source if tape else "n/a",
        )

        reason = self.risk.evaluate_exit(entry, qty, last_price, atr_value)
        if reason is ExitReason.NONE:
            return

        clock = self.client.get_clock()
        if not clock.is_open:
            logger.info("%s | %s detectado pero mercado cerrado — no se envía orden", symbol, reason.value)
            return

        self._close_and_report(symbol, qty, entry, last_price, reason.value)
        positions.pop(symbol, None)

    def _process_symbol(
        self,
        symbol: str,
        account: AccountSnapshot,
        positions: dict[str, Position],
    ) -> None:
        bars = self.market_data.get_bars(
            symbol,
            self.settings.bar_timeframe,
            self.settings.lookback_bars,
        )
        if bars.empty:
            return

        fallback = float(bars["close"].iloc[-1])
        tape = self.market_data.get_live_tape(symbol, fallback_price=fallback)
        last_price = tape.last_price if tape else fallback
        position = positions.get(symbol)
        qty = float(position.qty) if position is not None else 0.0

        ctx = StrategyContext(
            symbol=symbol,
            bars=bars,
            has_long_position=qty > 0,
            has_short_position=qty < 0,
            last_price=last_price,
            spread_pct=tape.spread_pct if tape else None,
            momentum_pct=momentum_pct(bars["close"], self.settings.momentum_bars),
            atr=last_atr(bars, self.settings.atr_period),
        )
        signal = self.strategy.generate_signal(ctx)
        if signal is Signal.HOLD:
            logger.debug("%s | HOLD", symbol)
            return

        ok, flow_reason = self.flow.confirm(signal, ctx)
        if not ok:
            logger.info("%s | señal=%s rechazada por flujo: %s", symbol, signal.value, flow_reason)
            return

        decision = self.risk.evaluate(
            signal=signal,
            symbol=symbol,
            last_price=last_price,
            account=account,
            open_positions=len(positions),
            has_position=position is not None,
        )
        if not decision.approved:
            logger.info("%s | señal=%s bloqueada: %s", symbol, signal.value, decision.reason)
            return

        if signal is Signal.BUY:
            audit("signal_buy", "allow", symbol=symbol, qty=decision.qty)
            self.executor.submit_market_order(symbol, decision.qty, OrderSide.BUY)
            self.reporter.emit(
                compute_pnl(symbol, decision.qty, last_price, last_price, PnLEvent.OPENED)
            )
            levels = self.risk.protective_levels(last_price, decision.qty, last_price, ctx.atr)
            logger.info(
                "%s | SL inicial=%.4f TP inicial=%.4f",
                symbol,
                levels.stop_price,
                levels.take_profit_price,
            )
        elif signal is Signal.SELL and position is not None:
            self._close_and_report(symbol, qty, float(position.avg_entry_price), last_price, "signal")

    def _close_and_report(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        last_price: float,
        reason: str,
    ) -> None:
        logger.info("%s | cerrando por %s", symbol, reason)
        self.executor.close_position(symbol)
        self.reporter.emit(compute_pnl(symbol, qty, entry_price, last_price, PnLEvent.CLOSED))
