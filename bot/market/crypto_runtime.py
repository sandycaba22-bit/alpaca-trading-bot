"""Helpers runtime cripto (paper régimen ETH, salidas asimétricas por símbolo)."""

from __future__ import annotations

from bot.config import Settings
from bot.market.assets import is_crypto_symbol, normalize_symbol


def crypto_regime_entry_symbols_norm(settings: Settings) -> frozenset[str]:
    return frozenset(normalize_symbol(s) for s in settings.crypto_regime_entry_symbols)


def symbol_uses_crypto_regime_entry(settings: Settings, symbol: str) -> bool:
    if not settings.crypto_regime_entry_enabled:
        return False
    return normalize_symbol(symbol) in crypto_regime_entry_symbols_norm(settings)


def crypto_uses_asymmetric_exits(settings: Settings, symbol: str) -> bool:
    if not is_crypto_symbol(symbol):
        return False
    if settings.crypto_asymmetric_live_enabled:
        return True
    return symbol_uses_crypto_regime_entry(settings, symbol)
