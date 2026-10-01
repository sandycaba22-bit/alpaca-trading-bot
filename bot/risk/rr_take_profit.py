"""Take profit 1:2 (R:R) desde distancia al stop — vol_2x acciones."""

from __future__ import annotations

from bot.market.assets import is_crypto_symbol, normalize_symbol
from bot.security.exceptions import ValidationError

DEFAULT_REWARD_RISK = 2.0
# Acciones vol_2x: SL ceñido + TP mínimo (sostenible vs micro-BE)
DEFAULT_STOCK_MAX_STOP_PCT = 0.0025
DEFAULT_STOCK_MIN_TP_PCT = 0.014


def _round_protective_price(price: float, symbol: str) -> float:
    sym = normalize_symbol(symbol)
    if price <= 0:
        return 0.0
    if is_crypto_symbol(sym):
        if price >= 1000:
            return round(price, 2)
        if price >= 1:
            return round(price, 4)
        return round(price, 6)
    if price >= 1:
        return round(price, 2)
    return round(price, 4)


def is_vol2x_stock_entry_reason(reason: str | None) -> bool:
    r = (reason or "").strip().lower()
    return "vol_2x" in r


def entry_requires_vol2x_rr_tp(symbol: str, reason: str | None) -> bool:
    if is_crypto_symbol(symbol):
        return False
    return is_vol2x_stock_entry_reason(reason)


def cap_stop_to_max_pct(
    *,
    entry_price: float,
    stop_price: float,
    qty: float,
    symbol: str,
    max_stop_pct: float,
) -> tuple[float, float]:
    """Acota la distancia SL desde entry (p. ej. -0.25% máx en acciones)."""
    ep = float(entry_price)
    sp = float(stop_price)
    cap = max(0.0, float(max_stop_pct))
    if cap <= 0 or ep <= 0:
        return sp, abs(ep - sp) / ep if ep > 0 else 0.0
    long = float(qty) >= 0
    if long:
        risk_pct = (ep - sp) / ep
        if risk_pct > cap:
            sp = _round_protective_price(ep * (1.0 - cap), symbol)
            risk_pct = cap
    else:
        risk_pct = (sp - ep) / ep
        if risk_pct > cap:
            sp = _round_protective_price(ep * (1.0 + cap), symbol)
            risk_pct = cap
    return sp, risk_pct


def compute_rr_take_profit(
    *,
    entry_price: float,
    stop_price: float,
    qty: float,
    symbol: str,
    reward_risk: float = DEFAULT_REWARD_RISK,
) -> tuple[float, float]:
    """LONG/SHORT: TP = entry ± risk_amount * reward_risk."""
    ep = float(entry_price)
    sp = float(stop_price)
    if ep <= 0:
        raise ValidationError("entry_price inválido para TP 1:2")
    rr = max(0.1, float(reward_risk))
    long = float(qty) >= 0
    if long:
        risk = ep - sp
        if risk <= 0:
            raise ValidationError(
                f"stop {sp:.4f} debe estar por debajo de entry {ep:.4f} (LONG vol_2x)"
            )
        tp = ep + risk * rr
    else:
        risk = sp - ep
        if risk <= 0:
            raise ValidationError(
                f"stop {sp:.4f} debe estar por encima de entry {ep:.4f} (SHORT vol_2x)"
            )
        tp = ep - risk * rr
    tp = _round_protective_price(tp, symbol)
    if tp <= 0:
        raise ValidationError("take_profit calculado inválido (<=0)")
    tp_pct = abs(tp / ep - 1.0)
    return tp, tp_pct


def enforce_vol2x_protective(
    *,
    symbol: str,
    side: str,
    reason: str | None,
    entry_price: float,
    qty: float,
    stop_price: float | None,
    take_profit_price: float | None,
    stop_pct: float | None,
    take_profit_pct: float | None,
    reward_risk: float = DEFAULT_REWARD_RISK,
    max_stop_pct: float | None = None,
    min_tp_pct: float | None = None,
) -> tuple[float, float, float, float]:
    """Garantiza TP>0 en entradas vol_2x acciones antes de broker/libro."""
    sp = float(stop_price or 0.0)
    tp = float(take_profit_price or 0.0)
    ep = float(entry_price or 0.0)
    side_l = (side or "").lower()
    if side_l != "buy" or not entry_requires_vol2x_rr_tp(symbol, reason):
        if tp > 0 and sp > 0:
            return sp, tp, float(stop_pct or 0.0), float(take_profit_pct or 0.0)
        return sp, tp, float(stop_pct or 0.0), float(take_profit_pct or 0.0)

    if sp <= 0 or ep <= 0:
        raise ValidationError(
            f"{symbol} vol_2x: prohibido enviar orden sin stop válido (TP 1:2 requiere SL)"
        )
    cap = DEFAULT_STOCK_MAX_STOP_PCT if max_stop_pct is None else max(0.0, float(max_stop_pct))
    floor_tp = DEFAULT_STOCK_MIN_TP_PCT if min_tp_pct is None else max(0.0, float(min_tp_pct))
    sp, st_pct = cap_stop_to_max_pct(
        entry_price=ep,
        stop_price=sp,
        qty=qty,
        symbol=symbol,
        max_stop_pct=cap,
    )
    tp, tp_pct = compute_rr_take_profit(
        entry_price=ep,
        stop_price=sp,
        qty=qty,
        symbol=symbol,
        reward_risk=reward_risk,
    )
    long = float(qty) >= 0
    if floor_tp > 0:
        if long:
            tp_floor = _round_protective_price(ep * (1.0 + floor_tp), symbol)
            if tp < tp_floor:
                tp = tp_floor
                tp_pct = floor_tp
        else:
            tp_floor = _round_protective_price(ep * (1.0 - floor_tp), symbol)
            if tp > tp_floor:
                tp = tp_floor
                tp_pct = floor_tp
    if tp <= 0:
        raise ValidationError(f"{symbol} vol_2x: take_profit no puede ser 0")
    if not (stop_pct and stop_pct > 0):
        st_pct = abs(ep - sp) / ep if ep > 0 else st_pct
    return sp, tp, st_pct, tp_pct
