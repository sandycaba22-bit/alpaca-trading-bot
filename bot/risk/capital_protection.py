"""Protección de capital: evita cierres en pérdidas pequeñas (stop ruido)."""

from __future__ import annotations

from bot.config import Settings, capital_protection_max_loss_pct_for
from bot.market.assets import is_crypto_symbol


def blocks_loss_exit(
    *,
    entry_price: float,
    qty: float,
    last_price: float,
    settings: Settings,
    symbol: str | None,
    breakeven_locked: bool,
) -> bool:
    """
    True = no cerrar aún (mantener posición).

    Tras candado BE, el SL del libro debería estar >= entrada; si aun así
    el precio está bajo entrada, no bloqueamos (gap / desincronización).
    """
    if not settings.capital_protection_enabled:
        return False
    if qty <= 0 or entry_price <= 0 or last_price <= 0:
        return False
    if breakeven_locked:
        return False

    long = qty >= 0
    if long:
        if last_price >= entry_price:
            return False
        loss_pct = (entry_price - last_price) / entry_price
    else:
        if last_price <= entry_price:
            return False
        loss_pct = (last_price - entry_price) / entry_price

    cap = capital_protection_max_loss_pct_for(settings, symbol)
    if loss_pct < cap:
        return True
    return False


def loss_exit_block_reason(
    *,
    entry_price: float,
    last_price: float,
    settings: Settings,
    symbol: str | None,
) -> str:
    cap = capital_protection_max_loss_pct_for(settings, symbol) * 100.0
    loss_pct = abs(last_price / entry_price - 1.0) * 100.0 if entry_price else 0.0
    asset = "cripto" if symbol and is_crypto_symbol(symbol) else "acciones"
    return (
        f"protección capital ({asset}) | pérdida {loss_pct:.2f}% < límite duro {cap:.2f}% "
        f"— se mantiene la posición"
    )
