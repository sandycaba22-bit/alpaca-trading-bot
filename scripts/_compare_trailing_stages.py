"""6 años: trailing 1 etapa vs 2 etapas (BTC/ETH/AAPL/MSFT)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.backtest.engine import BacktestEngine
from bot.backtest.metrics import compute_metrics
from bot.config import load_settings
from bot.risk.stops import StopTakeProfitPolicy
from bot.strategy.multi_strategy import MultiStrategyOrchestrator

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

SYMBOLS = ("AAPL", "MSFT", "BTC/USD", "ETH/USD")


def _stops(settings, *, two_stage: bool) -> StopTakeProfitPolicy:
    return StopTakeProfitPolicy(
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        atr_stop_mult=settings.atr_stop_mult,
        atr_sl_mult=settings.atr_sl_mult,
        atr_tp_mult=settings.atr_tp_mult,
        atr_trailing_mult=settings.atr_trailing_mult,
        breakeven_activate_pct=settings.breakeven_activate_pct,
        breakeven_activate_atr_mult=settings.breakeven_activate_atr_mult,
        breakeven_buffer=settings.breakeven_buffer,
        use_breakeven_lock=two_stage,
    )


def _run(settings, symbol, bars, *, two_stage: bool):
    orch = MultiStrategyOrchestrator(settings)
    engine = BacktestEngine(
        settings,
        market_data=None,  # type: ignore[arg-type]
        strategy=orch,
        stops=_stops(settings, two_stage=two_stage),
        quiet=True,
        use_risk_sizing=True,
        apply_daily_loss=True,
        apply_trailing=True,
    )
    result = engine.run_on_bars(symbol, bars)
    metrics = compute_metrics(result.equity, result.trades, settings.backtest_cash)
    pnls = [t.pnl_abs for t in result.trades]
    avg_pnl = (sum(pnls) / len(pnls)) if pnls else 0.0
    return result, metrics, avg_pnl


def main() -> int:
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    end = datetime.now(timezone.utc)
    years = max(int(settings.backtest_years or 6), 6)
    start = end - timedelta(days=int(years * 365.25))
    print(f"tf={settings.bar_timeframe} {start.date()}->{end.date()} years={years}")
    print(
        "symbol,scheme,signals,trades,win_rate_pct,avg_pnl,total_return_pct,max_dd_pct"
    )
    for symbol in SYMBOLS:
        bars = market.get_bars_range(symbol, settings.bar_timeframe, start=start, end=end)
        if bars.empty or len(bars) < 80:
            print(f"{symbol},SKIP,{0 if bars.empty else len(bars)}")
            continue
        for label, two_stage in (("one_stage", False), ("two_stage", True)):
            result, metrics, avg_pnl = _run(settings, symbol, bars, two_stage=two_stage)
            print(
                f"{symbol},{label},{result.signals},{metrics.trades},"
                f"{metrics.win_rate_pct:.1f},{avg_pnl:.4f},"
                f"{metrics.total_return_pct:.2f},{metrics.max_drawdown_pct:.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
