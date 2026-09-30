"""TP asimétrico crypto night: recompensa = reward_risk × distancia al stop."""

from __future__ import annotations

from strategies.crypto_night.types import TradeSide


def take_profit_from_stop(
    *,
    entry_price: float,
    stop_price: float,
    side: str,
    reward_risk: float = 2.5,
) -> float:
    ep = float(entry_price)
    sp = float(stop_price)
    rr = max(1.0, float(reward_risk))
    if ep <= 0 or sp <= 0:
        return 0.0
    if side == TradeSide.LONG.value or str(side).lower() == "long":
        risk = ep - sp
        if risk <= 0:
            return 0.0
        return ep + risk * rr
    risk = sp - ep
    if risk <= 0:
        return 0.0
    return ep - risk * rr


def round_crypto_price(price: float, *, ref: float) -> float:
    if price <= 0:
        return 0.0
    if ref >= 1000:
        return round(price, 2)
    if ref >= 1:
        return round(price, 4)
    return round(price, 6)
