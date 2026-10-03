"""Modo compresión: ATR bajo + precio cerca de S/R — caza de ruptura sin apagar el candado normal."""

from __future__ import annotations

import pandas as pd

from strategies.crypto_night.types import GateResult, NightBias, RejectReason, TradeSide
from strategies.crypto_night.volatility import VolatilitySnapshot, volatility_snapshot


# Distancia máxima al nivel de sesión (soporte/resistencia) para considerar compresión.
NEAR_LEVEL_PCT = 0.004
# Ancho Bollinger 1H (20, 2σ) por debajo de esto = mercado comprimido.
MAX_BB_WIDTH_PCT = 2.8
BB_PERIOD = 20
BB_STD = 2.0


def _bollinger_width_pct(closes: pd.Series, period: int = BB_PERIOD, n_std: float = BB_STD) -> float | None:
    if closes is None or len(closes) < period + 2:
        return None
    c = closes.astype(float)
    mid = c.rolling(period, min_periods=period).mean()
    std = c.rolling(period, min_periods=period).std()
    last_mid = float(mid.iloc[-1])
    last_std = float(std.iloc[-1])
    if last_mid <= 0 or not (last_std >= 0):
        return None
    upper = last_mid + n_std * last_std
    lower = last_mid - n_std * last_std
    return (upper - lower) / last_mid * 100.0


def _session_level(bars_15m: pd.DataFrame, session_start: pd.Timestamp, bias: NightBias) -> float | None:
    window = bars_15m.loc[bars_15m.index >= session_start]
    if len(window) < 5:
        return None
    if bias.side == TradeSide.LONG:
        return float(window["low"].astype(float).min())
    return float(window["high"].astype(float).max())


def near_key_level(
    *,
    last_price: float,
    level: float,
    max_dist_pct: float = NEAR_LEVEL_PCT,
) -> bool:
    if last_price <= 0 or level <= 0:
        return False
    return abs(last_price - level) / last_price <= max(0.0, float(max_dist_pct))


def try_compression_vol_gate(
    bars_1h: pd.DataFrame,
    bars_15m: pd.DataFrame,
    at_ts: pd.Timestamp,
    bias: NightBias,
    session_start: pd.Timestamp,
    *,
    pct_low: float,
    pct_high: float,
) -> tuple[GateResult, VolatilitySnapshot | None]:
    """Solo ATR por debajo del piso (compresión), no por encima del techo."""
    gate, snap = volatility_snapshot(
        bars_1h,
        at_ts,
        pct_low=pct_low,
        pct_high=pct_high,
    )
    if snap is None:
        return gate, None
    if gate.ok:
        return gate, snap
    if snap.current > snap.pct_ceiling:
        return (
            GateResult(
                False,
                RejectReason.VOLATILITY,
                f"ATR%={snap.current:.3f} alto (>{snap.pct_ceiling:.3f}) — sin modo compresión",
            ),
            snap,
        )
    if snap.current >= snap.pct_floor:
        return gate, snap

    bb_w = _bollinger_width_pct(bars_1h["close"])
    if bb_w is None or bb_w > MAX_BB_WIDTH_PCT:
        bb_txt = f"{bb_w:.2f}%" if bb_w is not None else "n/a"
        return (
            GateResult(
                False,
                RejectReason.VOLATILITY,
                f"compresión rechazada | BB ancho {bb_txt} > {MAX_BB_WIDTH_PCT:.1f}%",
            ),
            snap,
        )

    level = _session_level(bars_15m, session_start, bias)
    if level is None:
        return GateResult(False, RejectReason.VOLATILITY, "compresión sin nivel de sesión"), snap
    last_px = float(bars_15m["close"].astype(float).iloc[-1])
    if not near_key_level(last_price=last_px, level=level):
        side = "soporte" if bias.side == TradeSide.LONG else "resistencia"
        return (
            GateResult(
                False,
                RejectReason.VOLATILITY,
                f"compresión lejos de {side} {level:.2f} (px={last_px:.2f})",
            ),
            snap,
        )

    side = "soporte" if bias.side == TradeSide.LONG else "resistencia"
    return (
        GateResult(
            ok=True,
            detail=(
                f"compresión asimétrica | ATR%={snap.current:.3f}<{snap.pct_floor:.3f} "
                f"| BB {bb_w:.2f}% | cerca {side} {level:.2f}"
            ),
        ),
        snap,
    )


def resolve_entry_volatility(
    bars_1h: pd.DataFrame,
    bars_15m: pd.DataFrame,
    at_ts: pd.Timestamp,
    bias: NightBias,
    session_start: pd.Timestamp,
    *,
    pct_low: float,
    pct_high: float,
    low_vol_mode: bool,
) -> tuple[GateResult, float | None, bool]:
    """Candado ATR normal primero; si falla por ATR bajo, opcionalmente modo compresión."""
    gate, snap = volatility_snapshot(
        bars_1h,
        at_ts,
        pct_low=pct_low,
        pct_high=pct_high,
    )
    if snap is None:
        return gate, None, False
    if gate.ok:
        return gate, snap.current, False
    if not low_vol_mode:
        return gate, snap.current, False
    cg, cs = try_compression_vol_gate(
        bars_1h,
        bars_15m,
        at_ts,
        bias,
        session_start,
        pct_low=pct_low,
        pct_high=pct_high,
    )
    if cg.ok:
        return cg, cs.current if cs else snap.current, True
    return cg, snap.current, False
