"""SL/TP/BE crypto night — mismo perfil que acciones (protective_profile)."""

from __future__ import annotations

from bot.risk.protective_profile import (
    BREAKEVEN_ACTIVATE_PCT,
    BREAKEVEN_BUFFER_PCT,
    MAX_STOP_PCT,
    TP_MAX_PCT,
    TP_MIN_PCT,
    TP_TARGET_PCT,
    clamp_tp_pct,
)
from strategies.crypto_night.asymmetric_tp import round_crypto_price
from strategies.crypto_night.types import TradeSide

DEFAULT_MAX_STOP_PCT = MAX_STOP_PCT
DEFAULT_MIN_TP_PCT = TP_MIN_PCT
DEFAULT_MAX_TP_PCT = TP_MAX_PCT
DEFAULT_TP_TARGET_PCT = TP_TARGET_PCT
DEFAULT_BREAKEVEN_ACTIVATE_PCT = BREAKEVEN_ACTIVATE_PCT
DEFAULT_BREAKEVEN_BUFFER_PCT = BREAKEVEN_BUFFER_PCT


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
        if risk_pct > cap or risk_pct <= 0:
            sp = round_crypto_price(ep * (1.0 - cap), ref=ep)
            risk_pct = cap
    else:
        risk_pct = (sp - ep) / ep
        if risk_pct > cap or risk_pct <= 0:
            sp = round_crypto_price(ep * (1.0 + cap), ref=ep)
            risk_pct = cap
    return sp, risk_pct


DEFAULT_COMPRESSION_MAX_STOP_PCT = 0.0018


def apply_compression_protective(
    *,
    entry_price: float,
    stop_price: float,
    side: str,
    max_stop_pct: float = DEFAULT_COMPRESSION_MAX_STOP_PCT,
    partial_tp_pct: float = DEFAULT_TP_TARGET_PCT,
) -> tuple[float, float, float]:
    """SL ultratrecho; sin TP fijo en broker (salida por parcial + trail en software)."""
    ep = float(entry_price)
    side_s = side.value if isinstance(side, TradeSide) else str(side)
    cap = min(max(0.0, float(max_stop_pct)), DEFAULT_MAX_STOP_PCT)
    sp, stop_pct = cap_stop_to_max_pct(
        entry_price=ep,
        stop_price=float(stop_price),
        side=side_s,
        max_stop_pct=cap,
    )
    tp_pct = clamp_tp_pct(partial_tp_pct if partial_tp_pct > 0 else DEFAULT_TP_TARGET_PCT)
    return sp, stop_pct, tp_pct


def apply_crypto_night_protective(
    *,
    entry_price: float,
    stop_price: float,
    side: str,
    reward_risk: float,
    max_stop_pct: float = DEFAULT_MAX_STOP_PCT,
    min_tp_pct: float = DEFAULT_MIN_TP_PCT,
    max_tp_pct: float = DEFAULT_MAX_TP_PCT,
    target_tp_pct: float = DEFAULT_TP_TARGET_PCT,
) -> tuple[float, float, float, float]:
    _ = reward_risk
    ep = float(entry_price)
    side_s = side.value if isinstance(side, TradeSide) else str(side)
    sp, stop_pct = cap_stop_to_max_pct(
        entry_price=ep,
        stop_price=stop_price,
        side=side_s,
        max_stop_pct=max_stop_pct,
    )
    tp_pct = clamp_tp_pct(target_tp_pct if target_tp_pct > 0 else (min_tp_pct + max_tp_pct) / 2)
    if max_tp_pct > 0:
        tp_pct = min(tp_pct, max_tp_pct)
    if min_tp_pct > 0:
        tp_pct = max(tp_pct, min_tp_pct)
    if _is_long(side_s):
        tp = round_crypto_price(ep * (1.0 + tp_pct), ref=ep)
    else:
        tp = round_crypto_price(ep * (1.0 - tp_pct), ref=ep)
    return sp, tp, stop_pct, tp_pct


def unrealized_pct(*, entry_price: float, last_price: float, side: str) -> float:
    ep = float(entry_price)
    lp = float(last_price)
    if ep <= 0:
        return 0.0
    if _is_long(side):
        return (lp - ep) / ep
    return (ep - lp) / ep
