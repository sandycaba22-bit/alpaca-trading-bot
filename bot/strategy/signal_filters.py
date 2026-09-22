"""Filtros de contexto sobre la ruptura: sesgo SMA, ADX, cooldown y confirmación HTF."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from bot.config import Settings
from bot.storage.breakout_state import BreakoutStateStore
from bot.strategy.base import Signal
from bot.strategy.indicators import last_adx, momentum_pct, sma, volume_vs_average

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterCheck:
    allowed: bool
    reason: str
    value: float | None = None
    threshold: float | None = None


class SignalFilterLayer:
    """
    Jerarquía post-disparador (la ruptura vive en breakout.py):

    1. Sesgo SMA lenta (dirección)
    2. ADX (régimen de tendencia)
    3. Cooldown anti-whipsaw (solo nuevas entradas)
    4. Confirmación volumen / momentum TF superior (opcional, solo BUY)
    """

    def __init__(
        self,
        settings: Settings,
        state: BreakoutStateStore | None = None,
    ) -> None:
        self.settings = settings
        self.state = state or BreakoutStateStore()

    def adx_threshold_for(self, symbol: str) -> float:
        key = str(symbol or "").upper()
        return float(self.settings.adx_threshold_overrides.get(key, self.settings.adx_threshold))

    def cooldown_bars_for(self, symbol: str) -> int:
        key = str(symbol or "").upper()
        return int(
            self.settings.breakout_cooldown_overrides.get(
                key, self.settings.breakout_cooldown_bars
            )
        )

    def check_sma_bias(
        self,
        symbol: str,
        signal: Signal,
        bars: pd.DataFrame,
        slow_period: int,
    ) -> FilterCheck:
        if signal is Signal.HOLD:
            return FilterCheck(True, "hold", None, float(slow_period))
        if bars is None or bars.empty or "close" not in bars.columns:
            return FilterCheck(True, "SMA no disponible — se deja pasar", None, float(slow_period))
        if len(bars) < slow_period:
            return FilterCheck(
                True,
                f"SMA{slow_period} insuficiente — se deja pasar",
                None,
                float(slow_period),
            )
        closes = bars["close"].astype(float)
        slow = sma(closes, slow_period)
        last_sma = float(slow.iloc[-1])
        close = float(closes.iloc[-1])
        if pd.isna(last_sma) or last_sma <= 0:
            return FilterCheck(True, "SMA lenta vacía — se deja pasar", None, float(slow_period))
        if signal is Signal.BUY and close <= last_sma:
            return FilterCheck(
                False,
                f"ruptura contra sesgo SMA (close={close:.4f} <= SMA{slow_period}={last_sma:.4f})",
                last_sma,
                float(slow_period),
            )
        if signal is Signal.SELL and close >= last_sma:
            return FilterCheck(
                False,
                f"ruptura contra sesgo SMA (close={close:.4f} >= SMA{slow_period}={last_sma:.4f})",
                last_sma,
                float(slow_period),
            )
        side = "sobre" if close > last_sma else "bajo"
        return FilterCheck(
            True,
            f"sesgo SMA{slow_period} OK ({side} {last_sma:.4f})",
            last_sma,
            float(slow_period),
        )

    def check_adx(self, symbol: str, bars: pd.DataFrame) -> FilterCheck:
        threshold = self.adx_threshold_for(symbol)
        value = last_adx(bars, self.settings.adx_period)
        if value is None:
            return FilterCheck(
                True,
                "ADX no disponible — se deja pasar la ruptura",
                None,
                threshold,
            )
        if value <= threshold:
            return FilterCheck(
                False,
                f"ruptura ignorada por ADX bajo ({value:.2f} <= {threshold:.2f})",
                value,
                threshold,
            )
        return FilterCheck(
            True,
            f"ADX {value:.2f} > {threshold:.2f}",
            value,
            threshold,
        )

    def check_cooldown(self, symbol: str, bars: pd.DataFrame | None) -> FilterCheck:
        left = self.state.remaining_bars(symbol, bars)
        required = self.cooldown_bars_for(symbol)
        if left > 0:
            return FilterCheck(
                False,
                f"ruptura en cooldown | faltan {left} velas de {required}",
                float(left),
                float(required),
            )
        return FilterCheck(True, "sin cooldown", 0.0, float(required))

    def check_entry_confirmation(
        self,
        symbol: str,
        bars: pd.DataFrame,
        higher_tf_bars: pd.DataFrame | None,
        higher_tf_label: str | None = None,
    ) -> FilterCheck:
        settings = self.settings
        vol_ok, vol_ratio = volume_vs_average(
            bars,
            settings.volume_confirmation_period,
            settings.volume_confirmation_mult,
        )
        htf_ok: bool | None = None
        htf_mom: float | None = None
        if higher_tf_bars is not None and not higher_tf_bars.empty:
            htf_mom = momentum_pct(higher_tf_bars["close"], settings.confirm_momentum_bars)
            if htf_mom is not None:
                htf_ok = htf_mom > 0.0

        vol_txt = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
        mom_txt = f"{htf_mom:+.2%}" if htf_mom is not None else "n/a"
        tf = higher_tf_label or settings.confirm_higher_tf

        if vol_ok is True:
            return FilterCheck(
                True,
                f"confirmación por volumen ({vol_txt} avg {settings.volume_confirmation_period})",
                vol_ratio,
            )
        if htf_ok is True:
            return FilterCheck(
                True,
                f"confirmación por momentum {tf} ({mom_txt})",
                htf_mom,
            )
        local_mom = momentum_pct(bars["close"], settings.confirm_momentum_bars)
        if local_mom is not None and local_mom > 0.0:
            return FilterCheck(
                True,
                f"confirmación momentum operativo ({local_mom:+.2%})",
                local_mom,
            )
        if vol_ok is None and htf_ok is None:
            logger.info(
                "%s | confirmación no disponible (volumen y %s) — se deja pasar la entrada",
                symbol,
                tf,
            )
            return FilterCheck(True, "confirmación no disponible — se deja pasar", None)

        return FilterCheck(
            False,
            f"señal sin confirmación | vol={vol_txt} | momentum {tf}={mom_txt}",
            vol_ratio if vol_ratio is not None else htf_mom,
        )
