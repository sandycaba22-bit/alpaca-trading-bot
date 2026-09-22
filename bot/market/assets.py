"""Registro de activos: acciones vs cripto."""

from __future__ import annotations

from typing import Any, Iterable

from bot.config import Settings


def normalize_symbol(symbol: str) -> str:
    """Unifica BTCUSD (broker) con BTC/USD (libro y órdenes cripto)."""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return raw
    if "/" in raw:
        return raw
    if raw.endswith("USD") and len(raw) > 3 and raw[:-3].isalpha():
        return f"{raw[:-3]}/USD"
    return raw


def is_crypto_symbol(symbol: str) -> bool:
    return "/" in normalize_symbol(symbol)


def is_stock_symbol(symbol: str) -> bool:
    return not is_crypto_symbol(symbol)


def positions_by_symbol(positions: Iterable[Any]) -> dict[str, Any]:
    """Indexa posiciones del broker por símbolo canónico (BTC/USD, no BTCUSD)."""
    out: dict[str, Any] = {}
    for pos in positions:
        key = normalize_symbol(str(getattr(pos, "symbol", "") or ""))
        if not key:
            continue
        out[key] = pos
    return out


def all_symbols(settings: Settings) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for sym in list(settings.stock_symbols) + list(settings.crypto_symbols):
        key = normalize_symbol(sym)
        if key not in seen:
            seen.add(key)
            ordered.append(key if is_crypto_symbol(sym) else sym.upper())
    return ordered


def asset_class_for(symbol: str) -> str:
    return "crypto" if is_crypto_symbol(symbol) else "stock"


def position_matches_profile(symbol: str, bot_profile: str) -> bool:
    """True si el símbolo pertenece al perfil PM2 (stocks / crypto / hybrid=all)."""
    profile = str(bot_profile or "hybrid").strip().lower()
    if profile == "hybrid":
        return True
    if profile == "crypto":
        return is_crypto_symbol(symbol)
    if profile == "stocks":
        return is_stock_symbol(symbol)
    return True


def count_open_positions_for_profile(positions: dict[str, Any], bot_profile: str) -> int:
    return sum(
        1
        for sym in positions
        if position_matches_profile(sym, bot_profile)
        and float(getattr(positions[sym], "qty", 0) or 0) != 0
    )


def filter_positions_for_profile(positions: dict[str, Any], bot_profile: str) -> dict[str, Any]:
    return {
        sym: pos
        for sym, pos in positions.items()
        if position_matches_profile(sym, bot_profile)
    }
