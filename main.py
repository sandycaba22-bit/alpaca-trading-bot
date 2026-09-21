"""Punto de entrada del bot de trading Alpaca (paper)."""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import sys

from bot.alpaca.client import AlpacaClient
from bot.alpaca.execution import OrderExecutor
from bot.alpaca.market_data import MarketDataService
from bot.alpaca.reconcile import adopt_open_orders, assert_positions_match
from bot.backtest.compare_mtf import format_mtf_report, run_mtf_compare
from bot.backtest.compare_regimes import format_reports, run_compare
from bot.backtest.optimize import WalkForwardOptimizer
from bot.market.assets import all_symbols
from bot.market.dust import refresh_crypto_mins
from bot.config import load_settings
from bot.runtime_paths import configure_runtime_paths
from bot.engine import TradingEngine
from bot.logging_setup import setup_logging
from bot.notify.telegram import TelegramNotifier
from bot.reporting.pnl import PerformanceReporter
from bot.risk.manager import RiskManager
from bot.security.audit import audit
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, SecurityError, ValidationError
from bot.security.sanitize import bounded_int, safe_path_under
from bot.security.secrets import mask_secret
from bot.strategy.price_flow import PriceFlowFilter
from bot.storage.control import BotControl
from bot.storage.journal import TradeJournal
from bot.storage.params import load_symbol_params
from bot.storage.positions import OpenPositionBook
from bot.storage.pending_orders import PendingOrderBook
from bot.strategy.multi_strategy import MultiStrategyOrchestrator

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
        help="Walk-forward 6 anos (3 train + 3 validate) sin enviar ordenes",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=None,
        help="Anos de historia para --backtest (por defecto BACKTEST_YEARS)",
    )
    parser.add_argument(
        "--compare-strategies",
        action="store_true",
        help="Walk-forward 3+3 por estrategia y combinado (no guarda params, no opera)",
    )
    parser.add_argument(
        "--compare-mtf",
        action="store_true",
        help="Compara estructura 6m+9m vs 5m+15m (6 anos, SMA crossover, no opera)",
    )
    return parser.parse_args(argv)


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except (AttributeError, ValueError):
        return str(signum)


def _request_shutdown(engine: TradingEngine, *, reason: str, signum: int | None = None) -> None:
    if signum is not None:
        name = _signal_name(signum)
        logger.info(
            "Senal %s (%s) recibida — apagado ordenado con cierre WS",
            signum,
            name,
        )
        audit("shutdown", "allow", reason=reason, signum=signum, signal=name)
    else:
        logger.info("Apagado ordenado con cierre WS (%s)", reason)
        audit("shutdown", "allow", reason=reason)
    engine.request_shutdown()


def _finalize_shutdown(engine: TradingEngine) -> None:
    engine.shutdown(timeout=8.0)
    logging.shutdown()


def _install_signal_handlers(engine: TradingEngine) -> None:
    def _handle(signum: int, _frame) -> None:
        _request_shutdown(engine, reason="signal", signum=signum)

    atexit.register(_finalize_shutdown, engine)

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle)


def _run_backtest(settings, client: AlpacaClient, years: int | None) -> int:
    if years is not None:
        years = bounded_int(str(years), settings.backtest_years, min_value=2, max_value=10, name="years")
    train_years = settings.backtest_train_years
    validate_years = settings.backtest_validate_years
    if years is not None and years != train_years + validate_years:
        train_years = max(1, years // 2)
        validate_years = max(1, years - train_years)

    market_data = MarketDataService(client)
    optimizer = WalkForwardOptimizer(settings, market_data)
    audit(
        "backtest_start",
        "allow",
        years=years or (train_years + validate_years),
        train_years=train_years,
        validate_years=validate_years,
    )
    results = optimizer.run(
        all_symbols(settings),
        years=years,
        train_years=train_years,
        validate_years=validate_years,
    )
    if not results:
        logger.error("No se obtuvo ningun resultado de walk-forward")
        audit("backtest_end", "deny", reason="no_results")
        return 1

    for result in results:
        train_out = safe_path_under(settings.log_dir, f"backtest_{result.symbol}_train.csv")
        val_out = safe_path_under(settings.log_dir, f"backtest_{result.symbol}_validate.csv")
        result.train.equity.to_csv(train_out, header=["equity"])
        result.validate.equity.to_csv(val_out, header=["equity"])
        logger.info("Curvas de equity %s | train=%s | validate=%s", result.symbol, train_out, val_out)
    audit("backtest_end", "allow", symbols=",".join(r.symbol for r in results))
    return 0


def _run_compare_mtf(settings, client: AlpacaClient) -> int:
    logging.getLogger("bot.strategy.sma_crossover").setLevel(logging.WARNING)
    market_data = MarketDataService(client)
    audit("compare_mtf_start", "allow", years=settings.backtest_years)
    rows = run_mtf_compare(settings, market_data)
    report = format_mtf_report(rows)
    print(report)
    out = safe_path_under(settings.log_dir, "mtf_compare.txt")
    out.write_text(report + "\n", encoding="utf-8")
    logger.info("Informe MTF guardado en %s", out)
    audit("compare_mtf_end", "allow", rows=len(rows))
    return 0


def _run_compare_strategies(settings, client: AlpacaClient) -> int:
    market_data = MarketDataService(client)
    audit("compare_start", "allow", years=settings.backtest_years)
    quiet = (
        "bot.strategy.regime_selector",
        "bot.strategy.breakout",
        "bot.strategy.mean_reversion",
        "bot.strategy.pullback",
        "bot.strategy.squeeze",
        "bot.strategy.multi_strategy",
        "bot.backtest.engine",
        "bot.reporting.pnl",
    )
    previous = {name: logging.getLogger(name).level for name in quiet}
    for name in quiet:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        rows = run_compare(settings, market_data)
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)
    if not rows:
        logger.error("Compare: sin resultados")
        return 1
    text = format_reports(rows)
    out = safe_path_under(settings.log_dir, "strategy_compare.csv")
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    logger.info("Compare escrito en %s (%s filas)", out, len(rows))
    audit("compare_end", "allow", rows=len(rows))
    return 0


def _live_strategy(settings):
    stored = load_symbol_params()
    by_symbol = {
        symbol: (row.sma_fast, row.sma_slow)
        for symbol, row in stored.items()
        if row.sma_fast < row.sma_slow
    }
    for symbol in settings.crypto_symbols:
        key = symbol.upper()
        if key not in by_symbol:
            by_symbol[key] = (settings.crypto_sma_fast, settings.crypto_sma_slow)
    sma_slow_by_symbol = {sym: slow for sym, (_fast, slow) in by_symbol.items()}
    if by_symbol:
        logger.info(
            "Sesgo SMA lenta | %s",
            ", ".join(f"{sym} SMA{slow}" for sym, slow in sma_slow_by_symbol.items()),
        )
    else:
        logger.info(
            "Sesgo SMA lenta | default %s / cripto %s",
            settings.sma_slow,
            settings.crypto_sma_slow,
        )
    logger.info(
        "Disparador | multi-regimen | ruptura+pullback+mean-rev+squeeze | "
        "lookback=%s vol>=%.2fx cooldown=%s | riesgo fijo=%.2f%% | freno diario=%.2f%%",
        settings.breakout_lookback_periods,
        settings.breakout_volume_mult,
        settings.breakout_cooldown_bars,
        settings.risk_percent_per_trade * 100,
        settings.daily_loss_limit_pct * 100,
    )
    return MultiStrategyOrchestrator(settings, sma_slow_by_symbol), stored


def main(argv: list[str] | None = None) -> int:
    _local_lock = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "LOCAL_DISABLED")
    if os.path.isfile(_local_lock):
        print("Bot local deshabilitado de forma permanente (data/LOCAL_DISABLED).", file=sys.stderr)
        return 1
    args = _parse_args(argv)
    try:
        settings = load_settings()
        configure_runtime_paths(settings)
    except (ValidationError, SecurityError, ValueError) as exc:
        print(f"Configuracion invalida: {exc}", file=sys.stderr)
        return 1

    setup_logging(settings.log_level, settings.log_dir)
    audit(
        "startup",
        "allow",
        paper=settings.paper,
        dry_run=settings.dry_run,
        symbols=",".join(all_symbols(settings)),
        key=mask_secret(settings.api_key_id),
        mode="validate"
        if args.validate
        else "compare_mtf"
        if args.compare_mtf
        else "compare"
        if args.compare_strategies
        else "backtest"
        if args.backtest
        else "once"
        if args.once
        else "loop",
    )
    logger.info(
        "=== Bot trading Alpaca | profile=%s | paper=%s | dry_run=%s | data=%s ===",
        settings.bot_profile,
        settings.paper,
        settings.dry_run,
        settings.data_dir,
    )

    client = AlpacaClient(settings)
    snapshot, _clock = client.validate_startup()
    if not settings.dry_run:
        refresh_crypto_mins(client, settings)

    if args.validate:
        if snapshot is None:
            logger.error("Validacion estricta: no se pudo leer la cuenta")
            return 1
        logger.info("Validacion completada")
        audit("validate", "allow")
        return 0

    if args.compare_mtf:
        try:
            return _run_compare_mtf(settings, client)
        except (ValidationError, RateLimitError, SecurityError) as exc:
            log_caught(logger, "compare_mtf_blocked", exc)
            return 1

    if args.compare_strategies:
        try:
            return _run_compare_strategies(settings, client)
        except (ValidationError, RateLimitError, SecurityError) as exc:
            log_caught(logger, "compare_blocked", exc)
            return 1

    if args.backtest:
        try:
            return _run_backtest(settings, client, args.years)
        except (ValidationError, RateLimitError, SecurityError) as exc:
            log_caught(logger, "backtest_blocked", exc)
            return 1

    control = BotControl()
    strategy, stored_params = _live_strategy(settings)
    notifier = TelegramNotifier(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        prefix=settings.telegram_prefix,
    )
    if notifier.enabled:
        logger.info("Telegram configurado — verificacion en segundo plano al entrar al loop")
    else:
        logger.info("Telegram desactivado (falta token o chat_id)")

    position_book = OpenPositionBook()
    pending_fills = PendingOrderBook()
    executor = OrderExecutor(
        client,
        dry_run=settings.dry_run,
        journal=TradeJournal(),
        position_book=position_book,
        pending_fills=pending_fills,
        notifier=notifier,
        live_account_id=snapshot.id if snapshot is not None else "",
    )
    engine = TradingEngine(
        settings=settings,
        client=client,
        market_data=MarketDataService(client),
        executor=executor,
        strategy=strategy,
        risk=RiskManager(settings, overrides=stored_params),
        reporter=PerformanceReporter(),
        flow=PriceFlowFilter(
            max_spread_pct=settings.max_spread_pct,
            adverse_momentum_pct=settings.adverse_momentum_pct,
        ),
        notifier=notifier,
    )

    try:
        if not settings.paper:
            if snapshot is None:
                raise ValidationError(
                    "Modo live: no se pudo leer la cuenta Alpaca. El bot no arranca en seco."
                )
            if snapshot.account_blocked or snapshot.trading_blocked:
                raise ValidationError(
                    "Modo live: la cuenta esta bloqueada "
                    f"(account_blocked={snapshot.account_blocked} "
                    f"trading_blocked={snapshot.trading_blocked})."
                )
        if not settings.paper and not settings.dry_run:
            logger.warning(
                "LIVE | dinero real | URL=%s | DRY_RUN=false | "
                "la primera orden exigira CONFIRMO (TTY o data/live_confirm.txt)",
                settings.api_base_url,
            )
        if not settings.dry_run:
            n_open = adopt_open_orders(client, position_book, pending_fills)
            if n_open:
                logger.info("Reconcile: %s órdenes abiertas adoptadas como pendiente de fill", n_open)
            engine.settle_resting_orders()
        assert_positions_match(client, book=position_book, notifier=notifier, settings=settings)
        if args.once:
            engine.run_once()
        else:
            control.write_pid(os.getpid())
            control.set_paused(False)
            _install_signal_handlers(engine)
            engine.run_loop()
            _finalize_shutdown(engine)
    except KeyboardInterrupt:
        _request_shutdown(engine, reason="keyboard")
    except (ValidationError, RateLimitError, SecurityError) as exc:
        log_caught(logger, "runtime_blocked", exc)
        return 1
    except Exception as exc:
        log_caught(logger, "fatal", exc)
        return 1
    finally:
        control.clear_pid()

    logger.info("Proceso finalizado")
    audit("shutdown", "allow", reason="normal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
