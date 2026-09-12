"""Modo de operación: acciones en horario NYSE/NASDAQ, cripto fuera de horario y fines de semana."""

from __future__ import annotations

from enum import Enum

from bot.alpaca.market_clock import MarketClockView
from bot.config import Settings


class TradingMode(str, Enum):
    STOCKS = "stocks"
    CRYPTO = "crypto"


def resolve_trading_mode(clock: MarketClockView, settings: Settings) -> tuple[TradingMode, list[str]]:
    """Acciones cuando el mercado US está abierto; cripto en cierre, noches y fines de semana."""
    if clock.is_open:
        return TradingMode.STOCKS, list(settings.stock_symbols)
    return TradingMode.CRYPTO, list(settings.crypto_symbols)


def trading_mode_label(mode: TradingMode) -> str:
    if mode is TradingMode.STOCKS:
        return "Modo Acciones"
    return "Modo Cripto Nocturno/Fin de Semana"


def is_symbol_tradable(symbol: str, clock: MarketClockView) -> bool:
    from bot.market.assets import is_crypto_symbol

    if is_crypto_symbol(symbol):
        return True
    return bool(clock.is_open)
