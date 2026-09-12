"""Walk-forward: 3 años de entrenamiento + 3 años de validación."""

from __future__ import annotations

import itertools
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.backtest.engine import BacktestEngine, BacktestResult
from bot.backtest.metrics import PerformanceMetrics, compute_metrics, format_metrics
from bot.config import Settings
from bot.reporting.pnl import PerformanceReporter, PnLSnapshot
from bot.risk.stops import StopTakeProfitPolicy
from bot.security.errors import log_caught
from bot.market.assets import all_symbols, asset_class_for
from bot.strategy.price_flow import PriceFlowFilter
from bot.strategy.sma_crossover import SmaCrossoverStrategy

logger = logging.getLogger(__name__)

SMA_FAST_GRID = (10, 15, 20)
SMA_SLOW_GRID = (30, 50, 80)
STOP_GRID = (0.015, 0.02, 0.03)
TAKE_PROFIT_GRID = (0.04, 0.06, 0.08)
MIN_TRAIN_TRADES = 6
_QUIET_LOGGERS = (
    "bot.strategy.sma_crossover",
    "bot.strategy.price_flow",
    "bot.reporting.pnl",
    "bot.backtest.engine",
)


@contextmanager
def _quiet_strategy_logs():
    previous = {name: logging.getLogger(name).level for name in _QUIET_LOGGERS}
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        yield
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


class SilentReporter(PerformanceReporter):
    def emit(self, snap: PnLSnapshot) -> PnLSnapshot:
        return snap


@dataclass
class WalkForwardResult:
    symbol: str
    params: SymbolParams
    train: BacktestResult
    validate: BacktestResult
    train_metrics: PerformanceMetrics
    validate_metrics: PerformanceMetrics
    bars: pd.DataFrame


def _score(metrics: PerformanceMetrics) -> float:
    if metrics.trades < MIN_TRAIN_TRADES:
        return -1_000.0
    profit_factor = metrics.profit_factor
    if profit_factor == float("inf"):
        profit_factor = 5.0
    profit_factor = min(max(profit_factor, 0.0), 5.0)
    return (
        metrics.sharpe
        + metrics.total_return_pct / 50.0
        + metrics.win_rate_pct / 200.0
        + profit_factor * 0.15
        + metrics.max_drawdown_pct / 80.0
    )


def _split_bars(bars: pd.DataFrame, train_years: int, validate_years: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    end = bars.index.max()
    validate_start = end - pd.DateOffset(years=validate_years)
    train_start = validate_start - pd.DateOffset(years=train_years)
    train = bars[(bars.index >= train_start) & (bars.index < validate_start)]
    validate = bars[bars.index >= validate_start]
    return train, validate


class WalkForwardOptimizer:
    def __init__(self, settings: Settings, market_data: MarketDataService) -> None:
        self.settings = settings
        self.market_data = market_data
        self.flow = PriceFlowFilter(
            max_spread_pct=settings.max_spread_pct,
            adverse_momentum_pct=settings.adverse_momentum_pct,
        )

    def run(
        self,
        symbols: list[str] | None = None,
        years: int | None = None,
        train_years: int | None = None,
        validate_years: int | None = None,
    ) -> list[WalkForwardResult]:
        train_years = train_years or self.settings.backtest_train_years
        validate_years = validate_years or self.settings.backtest_validate_years
        total_years = years or (train_years + validate_years)
        results: list[WalkForwardResult] = []
        chosen: dict[str, SymbolParams] = {}

        for symbol in symbols or all_symbols(self.settings):
            try:
                result = self._run_symbol(symbol, total_years, train_years, validate_years)
            except Exception as exc:
                log_caught(logger, "walk_forward_failed", exc, symbol=symbol)
                continue
            results.append(result)
            chosen[symbol] = result.params

        if chosen:
            path = save_symbol_params(chosen)
            logger.info("Parametros optimizados guardados en %s", path)
        return results

    def _run_symbol(
        self,
        symbol: str,
        years: int,
        train_years: int,
        validate_years: int,
    ) -> WalkForwardResult:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(years * 365.25))
        bars = self.market_data.get_bars_range(
            symbol,
            self.settings.bar_timeframe,
            start=start,
            end=end,
        )
        if bars.empty:
            raise ValueError(f"Sin velas historicas para {symbol}")

        train_bars, validate_bars = _split_bars(bars, train_years, validate_years)
        min_bars = self.settings.sma_slow + 20
        if len(train_bars) < min_bars or len(validate_bars) < min_bars:
            raise ValueError(
                f"Barras insuficientes en {symbol}: train={len(train_bars)} validate={len(validate_bars)}"
            )

        logger.info(
            "Walk-forward %s | %s velas OHLC | train %s -> %s | validate %s -> %s",
            symbol,
            len(bars),
            train_bars.index.min(),
            train_bars.index.max(),
            validate_bars.index.min(),
            validate_bars.index.max(),
        )

        best_score = -1e9
        best: tuple[int, int, float, float] | None = None
        combos = list(itertools.product(SMA_FAST_GRID, SMA_SLOW_GRID, STOP_GRID, TAKE_PROFIT_GRID))
        logger.info("%s | evaluando %s patrones de compra/venta en train", symbol, len(combos))

        with _quiet_strategy_logs():
            for fast, slow, stop_pct, take_pct in combos:
                if fast >= slow:
                    continue
                metrics = self._metrics_for(
                    symbol,
                    train_bars,
                    fast,
                    slow,
                    stop_pct,
                    take_pct,
                )
                score = _score(metrics)
                if score > best_score:
                    best_score = score
                    best = (fast, slow, stop_pct, take_pct)

        if best is None:
            best = (
                self.settings.sma_fast,
                self.settings.sma_slow,
                self.settings.stop_loss_pct,
                self.settings.take_profit_pct,
            )
            best_score = -1e9
            logger.warning("%s | grid sin ganador, se usan parametros de .env", symbol)

        fast, slow, stop_pct, take_pct = best
        train = self._simulate(symbol, train_bars, fast, slow, stop_pct, take_pct, quiet=False)
        validate = self._simulate(symbol, validate_bars, fast, slow, stop_pct, take_pct, quiet=False)
        train_metrics = compute_metrics(train.equity, train.trades, self.settings.backtest_cash)
        validate_metrics = compute_metrics(validate.equity, validate.trades, self.settings.backtest_cash)

        params = SymbolParams(
            sma_fast=fast,
            sma_slow=slow,
            stop_loss_pct=stop_pct,
            take_profit_pct=take_pct,
            atr_stop_mult=self.settings.atr_stop_mult,
            asset_class=asset_class_for(symbol),
            source="optimized" if best_score > -999 else "default",
            train_score=round(best_score, 4),
            train_return_pct=round(train_metrics.total_return_pct, 4),
            train_sharpe=round(train_metrics.sharpe, 4),
            train_trades=train_metrics.trades,
            validate_return_pct=round(validate_metrics.total_return_pct, 4),
            validate_sharpe=round(validate_metrics.sharpe, 4),
            validate_trades=validate_metrics.trades,
            train_start=str(train_bars.index.min()),
            train_end=str(train_bars.index.max()),
            validate_start=str(validate_bars.index.min()),
            validate_end=str(validate_bars.index.max()),
        )
        logger.info(
            "%s | mejor patron SMA %s/%s | SL=%.2f%% TP=%.2f%% | train Sharpe=%.2f | validate Sharpe=%.2f",
            symbol,
            fast,
            slow,
            stop_pct * 100,
            take_pct * 100,
            train_metrics.sharpe,
            validate_metrics.sharpe,
        )
        for line in format_metrics(f"{symbol} TRAIN", train_metrics).splitlines():
            logger.info(line)
        for line in format_metrics(f"{symbol} VALIDATE", validate_metrics).splitlines():
            logger.info(line)
        if validate_metrics.sharpe < 0 and train_metrics.sharpe > 0:
            logger.warning(
                "%s | el patron gana en train pero pierde en validacion — vigilar en paper",
                symbol,
            )
        return WalkForwardResult(
            symbol=symbol,
            params=params,
            train=train,
            validate=validate,
            train_metrics=train_metrics,
            validate_metrics=validate_metrics,
            bars=bars,
        )

    def _metrics_for(
        self,
        symbol: str,
        bars: pd.DataFrame,
        fast: int,
        slow: int,
        stop_pct: float,
        take_pct: float,
    ) -> PerformanceMetrics:
        result = self._simulate(symbol, bars, fast, slow, stop_pct, take_pct, quiet=True)
        return compute_metrics(result.equity, result.trades, self.settings.backtest_cash)

    def _simulate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        fast: int,
        slow: int,
        stop_pct: float,
        take_pct: float,
        quiet: bool,
    ) -> BacktestResult:
        engine = BacktestEngine(
            settings=self.settings,
            market_data=self.market_data,
            strategy=SmaCrossoverStrategy(fast, slow),
            stops=StopTakeProfitPolicy(
                stop_loss_pct=stop_pct,
                take_profit_pct=take_pct,
                atr_stop_mult=self.settings.atr_stop_mult,
                atr_sl_mult=self.settings.atr_sl_mult,
                atr_tp_mult=self.settings.atr_tp_mult,
                atr_trailing_mult=self.settings.atr_trailing_mult,
            ),
            flow=self.flow,
            reporter=SilentReporter() if quiet else PerformanceReporter(),
            quiet=quiet,
        )
        return engine.run_on_bars(symbol, bars)
