"""Universo research — reexporta desde bot.universe."""

from __future__ import annotations

from bot.universe import ELITE_STOCK_SYMBOLS, TOP50_US_STOCK_SYMBOLS

# Compat: scripts que importaban LIQUID_STOCK_SYMBOLS
LIQUID_STOCK_SYMBOLS = ELITE_STOCK_SYMBOLS
