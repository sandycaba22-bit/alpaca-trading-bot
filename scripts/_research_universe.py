"""Universo research: líquidos US + BTC (IS/OOS backtests)."""

from __future__ import annotations

# Acciones 5Min/15Min (vol_2x, mean-rev research)
LIQUID_STOCK_SYMBOLS: tuple[str, ...] = (
    "NVDA",
    "AAPL",
    "MSFT",
    "QQQ",
    "SPY",
    "GOOGL",
    "META",
    "TSLA",
    "SLV",
)

# Cripto feed Alpaca
LIQUID_CRYPTO_SYMBOLS: tuple[str, ...] = ("BTC/USD",)
