"""Candado 5 — score 0–5 y R neto mínimo."""

from __future__ import annotations

from strategies.crypto_night.types import QualityScore, SweepSetup, TradeSide


def score_setup(
    setup: SweepSetup,
    *,
    spread_pct: float | None,
    atr_pct: float | None,
    mode_min: int = 5,
    min_theoretical_r: float = 1.8,
    max_spread_pct: float = 0.0005,
    fee_slip_r: float = 0.15,
    min_volume_ratio: float = 1.5,
) -> QualityScore:
    spread_ok = spread_pct is not None and spread_pct <= max_spread_pct
    sweep_ok = True
    # ATR% ya pasó candado vol (percentil 30–70 en 1H); aquí solo exigimos dato válido.
    atr_ok = atr_pct is not None and atr_pct > 0
    vol_ok = setup.volume_ratio >= float(min_volume_ratio)
    risk = abs(setup.limit_price - setup.stop_price)
    reward = abs(setup.limit_price - setup.sweep_extreme) * 2.0
    sl_atr_ok = setup.atr_1h > 0 and risk <= 1.5 * setup.atr_1h
    theoretical_r = (reward / risk) if risk > 0 else 0.0
    theoretical_r_net = theoretical_r - fee_slip_r
    points = {
        "spread": spread_ok,
        "sweep_bias": sweep_ok,
        "atr_band": atr_ok,
        "volume": vol_ok,
        "sl_atr": sl_atr_ok,
    }
    total = sum(1 for v in points.values() if v)
    ok = total >= mode_min and theoretical_r_net >= min_theoretical_r
    return QualityScore(
        total=total,
        points=points,
        min_required=mode_min,
        ok=ok,
        theoretical_r=theoretical_r_net,
        skip_low_r=theoretical_r_net < min_theoretical_r,
    )
