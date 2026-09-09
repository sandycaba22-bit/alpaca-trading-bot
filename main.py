"""Punto de entrada del bot de trading Alpaca (paper)."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from bot.alpaca.client import AlpacaClient
from bot.alpaca.execution import OrderExecutor
from bot.alpaca.market_data import MarketDataService
from bot.alpaca.reconcile import assert_positions_match
from bot.backtest.optimize import WalkForwardOptimizer
from bot.market.assets import all_symbols
from bot.config import load_settings
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
from bot.strategy.sma_crossover import TunedSmaStrategy

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
    if by_symbol:
        logger.info(
            "Estrategia afinada | %s",
            ", ".join(f"{sym} SMA{fast}/{slow}" for sym, (fast, slow) in by_symbol.items()),
        )
    else:
        logger.info(
            "Sin walk-forward previo — SMA %s/%s de .env. Ejecuta python main.py --backtest",
            settings.sma_fast,
            settings.sma_slow,
        )
    return TunedSmaStrategy(settings.sma_fast, settings.sma_slow, by_symbol), stored


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
        symbols=",".join(all_symbols(settings)),
        key=mask_secret(settings.api_key_id),
        mode="validate" if args.validate else "backtest" if args.backtest else "once" if args.once else "loop",
    )
    logger.info(
        "=== Bot trading Alpaca | hibrido acciones+cripto | paper=%s | dry_run=%s ===",
        settings.paper,
        settings.dry_run,
    )

    client = AlpacaClient(settings)
    snapshot, _clock = client.validate_startup()

    if args.validate:
        if snapshot is None:
            logger.error("Validacion estricta: no se pudo leer la cuenta")
            return 1
        logger.info("Validacion completada")
        audit("validate", "allow")
        return 0

    if args.backtest:
        try:
            return _run_backtest(settings, client, args.years)
        except (ValidationError, RateLimitError, SecurityError) as exc:
            log_caught(logger, "backtest_blocked", exc)
            return 1

    control = BotControl()
    strategy, stored_params = _live_strategy(settings)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    if notifier.enabled:
        logger.info("Telegram configurado — verificacion en segundo plano al entrar al loop")
    else:
        logger.info("Telegram desactivado (falta token o chat_id)")

    position_book = OpenPositionBook()
    executor = OrderExecutor(
        client,
        dry_run=settings.dry_run,
        journal=TradeJournal(),
        position_book=position_book,
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
        assert_positions_match(client, book=position_book, notifier=notifier)
        if args.once:
            engine.run_once()
        else:
            control.write_pid(os.getpid())
            control.set_paused(False)
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
    finally:
        control.clear_pid()

    logger.info("Proceso finalizado")
    audit("shutdown", "allow", reason="normal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
