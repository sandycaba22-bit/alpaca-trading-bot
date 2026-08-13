"""Backtesting histórico."""

from .engine import BacktestEngine, BacktestResult, SimulatedTrade
from .metrics import PerformanceMetrics, compute_metrics, format_metrics

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "SimulatedTrade",
    "PerformanceMetrics",
    "compute_metrics",
    "format_metrics",
]
