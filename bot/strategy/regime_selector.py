"""Selector de régimen: decide qué estrategias pueden disparar en este símbolo."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from bot.config import Settings
from bot.strategy.indicators import bollinger_width, last_adx

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    TREND = "tendencia_fuerte"
    RANGE = "lateral"
    SQUEEZE = "comprimido"
    UNKNOWN = "desconocido"


class StrategyId(str, Enum):
    BREAKOUT = "breakout"
    MEAN_REV = "mean_reversion"
    PULLBACK = "pullback"
    SQUEEZE = "squeeze"


@dataclass(frozen=True)
class RegimeSnapshot:
    regime: MarketRegime
    adx: float | None
    bb_width: float | None
    bb_width_avg: float | None
    compressed: bool
    enabled: tuple[StrategyId, ...]
    reason: str


def _is_compressed(closes: pd.Series, settings: Settings) -> tuple[bool, float | None, float | None]:
    width = bollinger_width(closes, settings.bb_period, settings.bb_std)
    lookback = int(settings.squeeze_width_lookback)
    clean = width.dropna()
    if len(clean) < lookback + 1:
        return False, None, None
    current = float(clean.iloc[-1])
    window = clean.iloc[-(lookback + 1) : -1]
    avg = float(window.mean())
    if not (current > 0 and avg > 0):
        return False, current, avg
    at_low = current <= float(window.min()) * 1.02
    below_avg = current < avg
    return bool(below_avg and at_low), current, avg


def select_regime(symbol: str, bars: pd.DataFrame, settings: Settings) -> RegimeSnapshot:
    adx_value = last_adx(bars, settings.adx_period) if bars is not None and not bars.empty else None
    threshold = float(settings.adx_threshold_overrides.get(str(symbol).upper(), settings.adx_threshold))
    compressed, width, width_avg = (False, None, None)
    if bars is not None and not bars.empty and "close" in bars.columns:
        compressed, width, width_avg = _is_compressed(bars["close"], settings)

    if compressed:
        regime = MarketRegime.SQUEEZE
        enabled = (StrategyId.SQUEEZE,)
        reason = (
            f"BB width comprimido ({width:.4f} < avg {width_avg:.4f})"
            if width is not None and width_avg is not None
            else "BB width comprimido"
        )
    elif adx_value is not None and adx_value > threshold:
        regime = MarketRegime.TREND
        enabled = (StrategyId.BREAKOUT, StrategyId.PULLBACK)
        reason = f"ADX {adx_value:.1f} > {threshold:.1f}"
    elif adx_value is not None:
        regime = MarketRegime.RANGE
        enabled = (StrategyId.MEAN_REV,)
        reason = f"ADX {adx_value:.1f} <= {threshold:.1f} sin squeeze"
    else:
        regime = MarketRegime.UNKNOWN
        enabled = (StrategyId.BREAKOUT,)
        reason = "ADX no disponible — solo ruptura (fallback)"

    logger.info(
        "%s | regimen=%s | %s | habilitadas=%s",
        symbol,
        regime.value,
        reason,
        ",".join(s.value for s in enabled),
    )
    return RegimeSnapshot(
        regime=regime,
        adx=adx_value,
        bb_width=width,
        bb_width_avg=width_avg,
        compressed=compressed,
        enabled=enabled,
        reason=reason,
    )
