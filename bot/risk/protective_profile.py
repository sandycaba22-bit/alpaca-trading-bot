"""Perfil único SL/TP/BE — élite, Top 50 y crypto night (solo gestión de salidas)."""

from __future__ import annotations

# SL simétrico: -0.20% … -0.22% (centro 0.21%)
MAX_STOP_PCT = 0.0021
# TP fijo rápido: +0.65% … +0.80% (centro 0.75%)
TP_MIN_PCT = 0.0065
TP_TARGET_PCT = 0.0075
TP_MAX_PCT = 0.0080
# Breakeven inteligente ~ mitad del TP: +0.35% … +0.40%
BREAKEVEN_ACTIVATE_PCT = 0.00375
BREAKEVEN_BUFFER_PCT = 0.0


def clamp_tp_pct(tp_pct: float) -> float:
    return max(TP_MIN_PCT, min(TP_MAX_PCT, float(tp_pct)))
