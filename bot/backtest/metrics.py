"""Métricas de backtest y desglose por régimen de mercado."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.backtest.engine import SimulatedTrade


@dataclass
class PerformanceMetrics:
    initial_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float
    avg_pnl: float
    avg_win: float
    avg_loss: float
    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def compute_metrics(
    equity: pd.Series,
    trades: list[SimulatedTrade],
    initial_cash: float,
) -> PerformanceMetrics:
    if equity.empty:
        return PerformanceMetrics(
            initial_cash=initial_cash,
            final_equity=initial_cash,
            total_return_pct=0.0,
            cagr_pct=0.0,
            sharpe=0.0,
            max_drawdown_pct=0.0,
            trades=0,
            win_rate_pct=0.0,
            profit_factor=0.0,
            avg_pnl=0.0,
            avg_win=0.0,
            avg_loss=0.0,
        )

    final_equity = float(equity.iloc[-1])
    total_return = _safe_div(final_equity, initial_cash) - 1.0
    days = max((equity.index[-1] - equity.index[0]).days, 1)
    years = days / 365.25
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(returns) > 1 and float(returns.std()) > 0:
        sharpe = float(np.sqrt(252) * returns.mean() / returns.std())
    else:
        sharpe = 0.0

    roll_max = equity.cummax()
    drawdown = equity / roll_max - 1.0
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    pnls = [t.pnl_abs for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = float(sum(wins))
    gross_loss = float(abs(sum(losses)))

    by_regime: dict[str, dict[str, float]] = {}
    for regime in ("bull", "bear", "sideways"):
        subset = [t for t in trades if t.regime == regime]
        if not subset:
            by_regime[regime] = {"trades": 0, "pnl": 0.0, "win_rate_pct": 0.0}
            continue
        r_wins = [t.pnl_abs for t in subset if t.pnl_abs > 0]
        by_regime[regime] = {
            "trades": float(len(subset)),
            "pnl": float(sum(t.pnl_abs for t in subset)),
            "win_rate_pct": 100.0 * len(r_wins) / len(subset),
        }

    return PerformanceMetrics(
        initial_cash=initial_cash,
        final_equity=final_equity,
        total_return_pct=total_return * 100.0,
        cagr_pct=cagr * 100.0,
        sharpe=sharpe,
        max_drawdown_pct=max_dd * 100.0,
        trades=len(trades),
        win_rate_pct=100.0 * _safe_div(len(wins), len(trades)),
        profit_factor=_safe_div(gross_profit, gross_loss) if gross_loss else (float("inf") if gross_profit else 0.0),
        avg_pnl=float(np.mean(pnls)) if pnls else 0.0,
        avg_win=float(np.mean(wins)) if wins else 0.0,
        avg_loss=float(np.mean(losses)) if losses else 0.0,
        by_regime=by_regime,
    )


def format_metrics(symbol: str, metrics: PerformanceMetrics) -> str:
    pf = metrics.profit_factor
    pf_txt = "inf" if pf == float("inf") else f"{pf:.2f}"
    lines = [
        f"=== BACKTEST {symbol} ===",
        f"Capital inicial     ${metrics.initial_cash:,.2f}",
        f"Equity final        ${metrics.final_equity:,.2f}",
        f"Retorno total       {metrics.total_return_pct:+.2f}%",
        f"CAGR                {metrics.cagr_pct:+.2f}%",
        f"Sharpe (252)        {metrics.sharpe:.2f}",
        f"Max drawdown        {metrics.max_drawdown_pct:.2f}%",
        f"Trades              {metrics.trades}",
        f"Win rate            {metrics.win_rate_pct:.1f}%",
        f"Profit factor       {pf_txt}",
        f"P&L medio           ${metrics.avg_pnl:,.2f}",
        f"Ganancia media      ${metrics.avg_win:,.2f}",
        f"Pérdida media       ${metrics.avg_loss:,.2f}",
        "--- Por régimen de mercado ---",
    ]
    labels = {"bull": "Alcista", "bear": "Bajista", "sideways": "Lateral"}
    for key, label in labels.items():
        row = metrics.by_regime.get(key, {})
        lines.append(
            f"  {label:8s}  trades={int(row.get('trades', 0)):3d}  "
            f"P&L=${row.get('pnl', 0.0):,.2f}  win={row.get('win_rate_pct', 0.0):.1f}%"
        )
    return "\n".join(lines)
