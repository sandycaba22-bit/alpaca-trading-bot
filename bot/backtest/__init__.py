"""Backtesting histórico."""

from .engine import BacktestEngine, BacktestResult, SimulatedTrade
from .metrics import PerformanceMetrics, compute_metrics, format_metrics
from .optimize import WalkForwardOptimizer, WalkForwardResult

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "SimulatedTrade",
    "PerformanceMetrics",
    "WalkForwardOptimizer",
    "WalkForwardResult",
    "compute_metrics",
    "format_metrics",
]
