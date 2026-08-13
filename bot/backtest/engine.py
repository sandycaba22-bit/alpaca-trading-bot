"""Motor de backtesting histórico (5–6 años) sobre datos Alpaca."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.config import Settings
from bot.reporting.pnl import PnLEvent, PerformanceReporter, compute_pnl
from bot.risk.stops import ExitReason, StopTakeProfitPolicy
from bot.strategy.base import Signal, Strategy, StrategyContext
from bot.strategy.indicators import classify_regime, last_atr, momentum_pct
from bot.strategy.price_flow import PriceFlowFilter
from bot.security.errors import log_caught
from bot.strategy.sma_crossover import SmaCrossoverStrategy

logger = logging.getLogger(__name__)


@dataclass
class SimulatedTrade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl_abs: float
    pnl_pct: float
    reason: str
    regime: str


@dataclass
class BacktestResult:
    symbol: str
    trades: list[SimulatedTrade]
    equity: pd.Series
    bars: pd.DataFrame


class BacktestEngine:
    """
    Simulación barra a barra sin look-ahead:

    - La señal se calcula con datos hasta el cierre de la barra i-1.
    - La entrada se llena al open de la barra i.
    - SL/TP se evalúan con high/low de la barra i (si ambos, gana el SL).
    """

    def __init__(
        self,
        settings: Settings,
        market_data: MarketDataService,
        strategy: Strategy | None = None,
        stops: StopTakeProfitPolicy | None = None,
        flow: PriceFlowFilter | None = None,
        reporter: PerformanceReporter | None = None,
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.strategy = strategy or SmaCrossoverStrategy(settings.sma_fast, settings.sma_slow)
        self.stops = stops or StopTakeProfitPolicy(
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            atr_stop_mult=settings.atr_stop_mult,
        )
        self.flow = flow or PriceFlowFilter(
            max_spread_pct=settings.max_spread_pct,
            adverse_momentum_pct=settings.adverse_momentum_pct,
        )
        self.reporter = reporter or PerformanceReporter()

    def run_symbol(self, symbol: str, years: int | None = None) -> BacktestResult:
        years = years or self.settings.backtest_years
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(years * 365.25))
        bars = self.market_data.get_bars_range(
            symbol,
            self.settings.bar_timeframe,
            start=start,
            end=end,
        )
        if bars.empty or len(bars) < self.settings.sma_slow + 5:
            raise ValueError(f"Barras insuficientes para backtest de {symbol}")

        logger.info(
            "Backtest %s | %s barras | %s -> %s | estrategia=%s",
            symbol,
            len(bars),
            bars.index.min(),
            bars.index.max(),
            self.strategy.name,
        )
        return self._simulate(symbol, bars)

    def run_all(self, symbols: list[str] | None = None, years: int | None = None) -> list[BacktestResult]:
        results: list[BacktestResult] = []
        for symbol in symbols or self.settings.symbols:
            try:
                results.append(self.run_symbol(symbol, years=years))
            except Exception as exc:
                log_caught(logger, "backtest_symbol_failed", exc, symbol=symbol)
        return results

    def _simulate(self, symbol: str, bars: pd.DataFrame) -> BacktestResult:
        cash = self.settings.backtest_cash
        equity_points: list[tuple[pd.Timestamp, float]] = []
        trades: list[SimulatedTrade] = []
        position_qty = 0.0
        entry_price = 0.0
        entry_time: pd.Timestamp | None = None
        entry_regime = "sideways"
        pending_buy = False

        for i in range(self.settings.sma_slow + 1, len(bars)):
            hist = bars.iloc[:i]
            row = bars.iloc[i]
            ts = bars.index[i]
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            atr_value = last_atr(hist, self.settings.atr_period)

            if pending_buy and position_qty == 0:
                qty = math.floor((cash * self.settings.position_size_pct) / o) if o > 0 else 0
                if qty >= 1:
                    position_qty = float(qty)
                    entry_price = o
                    entry_time = ts
                    cash -= qty * o
                    entry_regime = classify_regime(hist["close"])
                    self.reporter.emit(
                        compute_pnl(symbol, position_qty, entry_price, o, PnLEvent.OPENED)
                    )
                pending_buy = False

            if position_qty > 0:
                reason, exit_px = self.stops.evaluate_bar(
                    entry_price, position_qty, h, l, c, atr_value
                )
                signal = self._signal(symbol, hist, True, c)
                if reason is ExitReason.NONE and signal is Signal.SELL:
                    trades.append(
                        self._close(
                            symbol,
                            entry_time or ts,
                            ts,
                            entry_price,
                            c,
                            position_qty,
                            "signal",
                            entry_regime,
                            cash,
                        )
                    )
                    cash += position_qty * c
                    position_qty = 0.0
                    entry_price = 0.0
                elif reason is not ExitReason.NONE:
                    trades.append(
                        self._close(
                            symbol,
                            entry_time or ts,
                            ts,
                            entry_price,
                            exit_px,
                            position_qty,
                            reason.value,
                            entry_regime,
                            cash,
                        )
                    )
                    cash += position_qty * exit_px
                    position_qty = 0.0
                    entry_price = 0.0

            if position_qty == 0 and not pending_buy:
                signal = self._signal(symbol, hist, False, c)
                if signal is Signal.BUY:
                    pending_buy = True

            mtm = cash + position_qty * c
            equity_points.append((ts, mtm))

        if position_qty > 0:
            last_ts = bars.index[-1]
            last_px = float(bars["close"].iloc[-1])
            trades.append(
                self._close(
                    symbol,
                    entry_time or last_ts,
                    last_ts,
                    entry_price,
                    last_px,
                    position_qty,
                    "eod",
                    entry_regime,
                    cash,
                )
            )
            cash += position_qty * last_px
            equity_points[-1] = (last_ts, cash)

        equity = pd.Series(
            {ts: val for ts, val in equity_points},
            name="equity",
        )
        equity.index = pd.DatetimeIndex(equity.index)
        return BacktestResult(symbol=symbol, trades=trades, equity=equity, bars=bars)

    def _signal(self, symbol: str, hist: pd.DataFrame, has_long: bool, last_price: float) -> Signal:
        ctx = StrategyContext(
            symbol=symbol,
            bars=hist,
            has_long_position=has_long,
            has_short_position=False,
            last_price=last_price,
            spread_pct=0.0,
            momentum_pct=momentum_pct(hist["close"], self.settings.momentum_bars),
            atr=last_atr(hist, self.settings.atr_period),
        )
        signal = self.strategy.generate_signal(ctx)
        if signal is Signal.HOLD:
            return signal
        ok, reason = self.flow.confirm(signal, ctx)
        if not ok:
            logger.debug("%s | señal %s filtrada en backtest: %s", symbol, signal.value, reason)
            return Signal.HOLD
        return signal

    def _close(
        self,
        symbol: str,
        entry_time: pd.Timestamp,
        exit_time: pd.Timestamp,
        entry_price: float,
        exit_price: float,
        qty: float,
        reason: str,
        regime: str,
        cash: float,
    ) -> SimulatedTrade:
        snap = self.reporter.emit(
            compute_pnl(symbol, qty, entry_price, exit_price, PnLEvent.CLOSED)
        )
        logger.info(
            "%s | salida %s | cash~%.2f",
            symbol,
            reason,
            cash + qty * exit_price,
        )
        return SimulatedTrade(
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            pnl_abs=snap.pnl_abs,
            pnl_pct=snap.pnl_pct,
            reason=reason,
            regime=regime,
        )
