"""Walk-forward 3+3 años: cada estrategia sola y las 4 combinadas, sizing viejo vs riesgo fijo."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.backtest.engine import BacktestEngine, BacktestResult
from bot.backtest.metrics import PerformanceMetrics, compute_metrics
from bot.config import Settings
from bot.market.assets import all_symbols
from bot.strategy.multi_strategy import MultiStrategyOrchestrator
from bot.strategy.regime_selector import StrategyId

logger = logging.getLogger(__name__)

RUNS: tuple[tuple[str, StrategyId | None], ...] = (
    ("breakout", StrategyId.BREAKOUT),
    ("mean_reversion", StrategyId.MEAN_REV),
    ("pullback", StrategyId.PULLBACK),
    ("squeeze", StrategyId.SQUEEZE),
    ("combined", None),
)


@dataclass
class SliceReport:
    label: str
    sizing: str
    symbol: str
    window: str
    start: str
    end: str
    signals: int
    trades: int
    win_rate_pct: float
    max_drawdown_pct: float
    total_return_pct: float
    sharpe: float
    profit_factor: float


def _split_bars(bars: pd.DataFrame, train_years: int, validate_years: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    end = bars.index.max()
    if getattr(end, "tzinfo", None) is None:
        end = pd.Timestamp(end, tz="UTC")
    validate_start = end - pd.DateOffset(years=int(validate_years))
    train_start = validate_start - pd.DateOffset(years=int(train_years))
    train = bars[(bars.index >= train_start) & (bars.index < validate_start)]
    validate = bars[bars.index >= validate_start]
    return train, validate


def _report(
    label: str,
    sizing: str,
    symbol: str,
    window: str,
    result: BacktestResult,
    metrics: PerformanceMetrics,
) -> SliceReport:
    pf = metrics.profit_factor
    if pf == float("inf"):
        pf = 99.0
    return SliceReport(
        label=label,
        sizing=sizing,
        symbol=symbol,
        window=window,
        start=str(result.bars.index.min()) if not result.bars.empty else "",
        end=str(result.bars.index.max()) if not result.bars.empty else "",
        signals=int(result.signals),
        trades=int(metrics.trades),
        win_rate_pct=float(metrics.win_rate_pct),
        max_drawdown_pct=float(metrics.max_drawdown_pct),
        total_return_pct=float(metrics.total_return_pct),
        sharpe=float(metrics.sharpe),
        profit_factor=float(pf),
    )


def _run_slice(
    settings: Settings,
    strategy: MultiStrategyOrchestrator,
    symbol: str,
    bars: pd.DataFrame,
    *,
    use_risk: bool,
    label: str,
    window: str,
) -> SliceReport:
    engine = BacktestEngine(
        settings,
        market_data=None,  # type: ignore[arg-type]
        strategy=strategy,
        quiet=True,
        use_risk_sizing=use_risk,
        apply_daily_loss=True,
    )
    result = engine.run_on_bars(symbol, bars)
    metrics = compute_metrics(result.equity, result.trades, settings.backtest_cash)
    sizing = "riesgo_fijo" if use_risk else "notional_fijo"
    return _report(label, sizing, symbol, window, result, metrics)


def run_compare(settings: Settings, market_data: MarketDataService) -> list[SliceReport]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(settings.backtest_years * 365.25))
    symbols = all_symbols(settings)
    logger.info(
        "Compare regimenes | %s | %s -> %s | train=%sa validate=%sa | timeframe=%s",
        ",".join(symbols),
        start.date(),
        end.date(),
        settings.backtest_train_years,
        settings.backtest_validate_years,
        settings.bar_timeframe,
    )
    rows: list[SliceReport] = []
    for symbol in symbols:
        bars = market_data.get_bars_range(symbol, settings.bar_timeframe, start=start, end=end)
        if bars.empty or len(bars) < 80:
            logger.error("%s | barras insuficientes (%s)", symbol, 0 if bars.empty else len(bars))
            continue
        train, validate = _split_bars(
            bars, settings.backtest_train_years, settings.backtest_validate_years
        )
        logger.info(
            "%s | total=%s | train=%s %s->%s | validate=%s %s->%s",
            symbol,
            len(bars),
            len(train),
            train.index.min() if not train.empty else "—",
            train.index.max() if not train.empty else "—",
            len(validate),
            validate.index.min() if not validate.empty else "—",
            validate.index.max() if not validate.empty else "—",
        )
        for label, force in RUNS:
            for use_risk in (False, True):
                for window, slice_bars in (("train", train), ("validate", validate)):
                    if slice_bars.empty or len(slice_bars) < 80:
                        logger.warning("%s | %s %s sin barras", symbol, label, window)
                        continue
                    orch = MultiStrategyOrchestrator(settings)
                    orch.force_strategy = force
                    row = _run_slice(
                        settings,
                        orch,
                        symbol,
                        slice_bars,
                        use_risk=use_risk,
                        label=label,
                        window=window,
                    )
                    rows.append(row)
                    logger.info(
                        "%s | %s | %s | %s | señales=%s trades=%s win=%.1f%% dd=%.2f%% ret=%+.2f%% sharpe=%.2f",
                        symbol,
                        label,
                        window,
                        row.sizing,
                        row.signals,
                        row.trades,
                        row.win_rate_pct,
                        row.max_drawdown_pct,
                        row.total_return_pct,
                        row.sharpe,
                    )
    return rows


def format_reports(rows: list[SliceReport]) -> str:
    lines = [
        "estrategia,sizing,symbol,ventana,inicio,fin,señales,trades,win%,max_dd%,retorno%,sharpe,pf",
    ]
    for r in rows:
        lines.append(
            f"{r.label},{r.sizing},{r.symbol},{r.window},{r.start},{r.end},"
            f"{r.signals},{r.trades},{r.win_rate_pct:.1f},{r.max_drawdown_pct:.2f},"
            f"{r.total_return_pct:.2f},{r.sharpe:.2f},{r.profit_factor:.2f}"
        )
    return "\n".join(lines)
