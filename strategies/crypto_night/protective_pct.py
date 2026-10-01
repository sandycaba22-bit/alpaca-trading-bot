"""SL/TP por %% en crypto night — misma lógica que acciones vol_2x (tope SL, piso TP)."""

from __future__ import annotations

from strategies.crypto_night.asymmetric_tp import round_crypto_price, take_profit_from_stop
from strategies.crypto_night.types import TradeSide

DEFAULT_MAX_STOP_PCT = 0.0025
DEFAULT_MIN_TP_PCT = 0.014
DEFAULT_BREAKEVEN_ACTIVATE_PCT = 0.007
DEFAULT_BREAKEVEN_BUFFER_PCT = 0.0005


def _is_long(side: str) -> bool:
    s = (side.value if isinstance(side, TradeSide) else str(side)).lower()
    return s == TradeSide.LONG.value


def cap_stop_to_max_pct(
    *,
    entry_price: float,
    stop_price: float,
    side: str,
    max_stop_pct: float,
) -> tuple[float, float]:
    ep = float(entry_price)
    sp = float(stop_price)
    cap = max(0.0, float(max_stop_pct))
    if cap <= 0 or ep <= 0:
        return sp, abs(ep - sp) / ep if ep > 0 else 0.0
    long = _is_long(side)
    if long:
        risk_pct = (ep - sp) / ep
        if risk_pct > cap:
            sp = round_crypto_price(ep * (1.0 - cap), ref=ep)
            risk_pct = cap
        elif risk_pct <= 0:
            sp = round_crypto_price(ep * (1.0 - cap), ref=ep)
            risk_pct = cap
    else:
        risk_pct = (sp - ep) / ep
        if risk_pct > cap:
            sp = round_crypto_price(ep * (1.0 + cap), ref=ep)
            risk_pct = cap
        elif risk_pct <= 0:
            sp = round_crypto_price(ep * (1.0 + cap), ref=ep)
            risk_pct = cap
    return sp, risk_pct


def apply_crypto_night_protective(
    *,
    entry_price: float,
    stop_price: float,
    side: str,
    reward_risk: float,
    max_stop_pct: float = DEFAULT_MAX_STOP_PCT,
    min_tp_pct: float = DEFAULT_MIN_TP_PCT,
) -> tuple[float, float, float, float]:
    """Devuelve stop, take_profit, stop_pct, tp_pct desde entry."""
    ep = float(entry_price)
    side_s = side.value if isinstance(side, TradeSide) else str(side)
    sp, stop_pct = cap_stop_to_max_pct(
        entry_price=ep,
        stop_price=stop_price,
        side=side_s,
        max_stop_pct=max_stop_pct,
    )
    tp_raw = take_profit_from_stop(
        entry_price=ep,
        stop_price=sp,
        side=side_s,
        reward_risk=reward_risk,
    )
    tp = round_crypto_price(tp_raw, ref=ep)
    tp_pct = abs(tp / ep - 1.0) if ep > 0 and tp > 0 else 0.0
    floor = max(0.0, float(min_tp_pct))
    if floor > 0 and ep > 0:
        if _is_long(side_s):
            tp_floor = round_crypto_price(ep * (1.0 + floor), ref=ep)
            if tp < tp_floor:
                tp = tp_floor
                tp_pct = floor
        else:
            tp_floor = round_crypto_price(ep * (1.0 - floor), ref=ep)
            if tp > tp_floor:
                tp = tp_floor
                tp_pct = floor
    return sp, tp, stop_pct, tp_pct


def unrealized_pct(*, entry_price: float, last_price: float, side: str) -> float:
    ep = float(entry_price)
    lp = float(last_price)
    if ep <= 0:
        return 0.0
    if _is_long(side):
        return (lp - ep) / ep
    return (ep - lp) / ep
