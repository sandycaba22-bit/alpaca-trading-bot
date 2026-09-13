"""Pullback de compra en tendencia 9m alcista, reutilizando el criterio RSI/Bollinger de mean-reversion."""

from __future__ import annotations

import logging

import pandas as pd

from bot.config import Settings
from bot.strategy.base import Signal
from bot.strategy.mean_reversion import mean_reversion_criteria
from bot.strategy.multi_tf_analysis import analyze_trend

logger = logging.getLogger(__name__)


def resolve_htf_trend(bars: pd.DataFrame, htf_trend: str | None) -> str:
    """Usa la tendencia 9m ya calculada; si no llega, el mismo analyze_trend del filtro."""
    raw = str(htf_trend or "").strip().lower()
    if raw in {"bull", "bear", "sideways"}:
        return raw
    trend, _ret = analyze_trend(bars)
    return trend


def detect_trend_pullback(
    bars: pd.DataFrame,
    *,
    settings: Settings,
    has_long: bool,
    symbol: str = "",
    htf_trend: str | None = None,
) -> tuple[Signal, str]:
    trend = resolve_htf_trend(bars, htf_trend)
    if trend != "bull":
        return Signal.HOLD, f"trend-pullback inactivo | tendencia 9m={trend}"
    if has_long:
        return Signal.HOLD, "trend-pullback omitido — ya hay long"

    raw, detail = mean_reversion_criteria(bars, settings=settings, has_long=has_long)
    if raw is not Signal.BUY:
        return Signal.HOLD, f"trend-pullback sin corrección | {detail}"

    why = detail.replace("mean-rev BUY", "trend-pullback BUY", 1)
    logger.info("%s | %s | tendencia 9m alcista", symbol or "?", why)
    return Signal.BUY, why
