"""Modo de operación: solo acciones US (perfiles stocks / stocks_top50)."""

from __future__ import annotations

from enum import Enum

from bot.alpaca.market_clock import MarketClockView
from bot.config import Settings


class TradingMode(str, Enum):
    STOCKS = "stocks"


def resolve_trading_mode(clock: MarketClockView, settings: Settings) -> tuple[TradingMode, list[str]]:
    """Lista de acciones del perfil PM2; fuera de horario no se opera."""
    _ = clock
    return TradingMode.STOCKS, list(settings.stock_symbols)


def trading_mode_label(mode: TradingMode, profile: str = "stocks") -> str:
    _ = mode
    if profile == "stocks_top50":
        return "Modo Top 50 Acciones"
    return "Modo Acciones Elite"


def is_symbol_tradable(symbol: str, clock: MarketClockView) -> bool:
    _ = symbol
    return bool(clock.is_open)
