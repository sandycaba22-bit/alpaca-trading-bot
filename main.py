"""Punto de entrada del bot de trading Alpaca (paper)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from bot.alpaca.client import AlpacaClient
from bot.alpaca.execution import OrderExecutor
from bot.alpaca.market_data import MarketDataService
from bot.backtest.engine import BacktestEngine
from bot.backtest.metrics import compute_metrics, format_metrics
from bot.config import load_settings
from bot.engine import TradingEngine
from bot.logging_setup import setup_logging
from bot.reporting.pnl import PerformanceReporter
from bot.risk.manager import RiskManager
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, SecurityError, ValidationError
from bot.security.sanitize import bounded_int, safe_path_under
from bot.security.secrets import mask_secret
from bot.strategy.price_flow import PriceFlowFilter
from bot.strategy.sma_crossover import SmaCrossoverStrategy

logger = logging.getLogger("bot")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bot de trading Alpaca Paper")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Valida la conexion, reporta P&L y ejecuta un solo ciclo",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Solo valida credenciales y estado de la cuenta",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Backtest historico (5-6 anos) sin enviar ordenes",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=None,
        help="Anos de historia para --backtest (por defecto BACKTEST_YEARS)",
    )
    return parser.parse_args(argv)


def _install_signal_handlers(engine: TradingEngine) -> None:
    def _handle(signum: int, _frame) -> None:
        logger.info("Senal %s recibida - apagado ordenado", signum)
        audit("shutdown", "allow", reason="signal", signum=signum)
        engine.stop()

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle)


def _run_backtest(settings, client: AlpacaClient, years: int | None) -> int:
    if years is not None:
        years = bounded_int(str(years), settings.backtest_years, min_value=1, max_value=10, name="years")
    market_data = MarketDataService(client)
    strategy = SmaCrossoverStrategy(fast=settings.sma_fast, slow=settings.sma_slow)
    engine = BacktestEngine(
        settings=settings,
        market_data=market_data,
        strategy=strategy,
        reporter=PerformanceReporter(),
        flow=PriceFlowFilter(
            max_spread_pct=settings.max_spread_pct,
            adverse_momentum_pct=settings.adverse_momentum_pct,
        ),
    )
    audit("backtest_start", "allow", years=years or settings.backtest_years)
    results = engine.run_all(settings.symbols, years=years)
    if not results:
        logger.error("No se obtuvo ningun resultado de backtest")
        audit("backtest_end", "deny", reason="no_results")
        return 1

    for result in results:
        metrics = compute_metrics(result.equity, result.trades, settings.backtest_cash)
        report = format_metrics(result.symbol, metrics)
        for line in report.splitlines():
            logger.info(line)
        out = safe_path_under(settings.log_dir, f"backtest_{result.symbol}.csv")
        result.equity.to_csv(out, header=["equity"])
        logger.info("Curva de equity guardada en %s", out)
    audit("backtest_end", "allow", symbols=",".join(r.symbol for r in results))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = load_settings()
    except (ValidationError, SecurityError, ValueError) as exc:
        print(f"Configuracion invalida: {exc}", file=sys.stderr)
        return 1

    setup_logging(settings.log_level, settings.log_dir)
    audit(
        "startup",
        "allow",
        paper=settings.paper,
        dry_run=settings.dry_run,
        symbols=",".join(settings.symbols),
        key=mask_secret(settings.api_key_id),
        mode="validate" if args.validate else "backtest" if args.backtest else "once" if args.once else "loop",
    )
    logger.info("=== Bot trading Alpaca | paper=%s | dry_run=%s ===", settings.paper, settings.dry_run)

    if not settings.paper:
        logger.error("Este bot esta limitado a Paper Trading.")
        audit("startup", "deny", reason="live_trading_blocked")
        return 1

    client = AlpacaClient(settings)
    try:
        client.validate_connection()
    except (ConnectionError, RuntimeError, RateLimitError) as exc:
        log_caught(logger, "startup_auth", exc)
        return 1

    if args.validate:
        logger.info("Validacion completada")
        audit("validate", "allow")
        return 0

    if args.backtest:
        try:
            return _run_backtest(settings, client, args.years)
        except (ValidationError, RateLimitError, SecurityError) as exc:
            log_caught(logger, "backtest_blocked", exc)
            return 1

    engine = TradingEngine(
        settings=settings,
        client=client,
        market_data=MarketDataService(client),
        executor=OrderExecutor(client, dry_run=settings.dry_run),
        strategy=SmaCrossoverStrategy(fast=settings.sma_fast, slow=settings.sma_slow),
        risk=RiskManager(settings),
        reporter=PerformanceReporter(),
        flow=PriceFlowFilter(
            max_spread_pct=settings.max_spread_pct,
            adverse_momentum_pct=settings.adverse_momentum_pct,
        ),
    )

    try:
        if args.once:
            engine.run_once()
        else:
            _install_signal_handlers(engine)
            engine.run_loop()
    except KeyboardInterrupt:
        logger.info("Interrupcion de teclado - saliendo")
        audit("shutdown", "allow", reason="keyboard")
        engine.stop()
    except (ValidationError, RateLimitError, SecurityError) as exc:
        log_caught(logger, "runtime_blocked", exc)
        return 1
    except Exception as exc:
        log_caught(logger, "fatal", exc)
        return 1

    logger.info("Proceso finalizado")
    audit("shutdown", "allow", reason="normal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
