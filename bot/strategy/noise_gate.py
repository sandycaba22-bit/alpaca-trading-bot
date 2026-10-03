"""Filtro de ruido para entradas en acciones.

El escaneo no cambia de cadencia. Este paso solo decide si el gatillo
se ejecuta: mercado lateral, ADX flojo o ruptura sin volumen se omiten.
INTU y CMCSA exigen un umbral más alto porque rompen el rango y vuelven.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.config import Settings
from bot.strategy.indicators import efficiency_ratio, last_dmi, last_rsi, volume_vs_average

# Piso de tendencia para cualquier acción, aunque ADX_THRESHOLD del entorno sea más bajo.
STOCK_ADX_FLOOR = 26.0
STOCK_VOLUME_FLOOR = 1.85
STOCK_EFFICIENCY_MIN = 0.40
EFFICIENCY_LOOKBACK = 10
# RSI en vela de entrada: evita comprar rupturas sobrecompradas o sin impulso.
STOCK_RSI_MIN = 42.0
STOCK_RSI_MAX = 66.0

# Falsas rupturas frecuentes: más ADX, más volumen y menos camino de ida y vuelta.
NOISY_BREAKOUT_SYMBOLS: dict[str, dict[str, float]] = {
    "INTU": {"adx_min": 32.0, "volume_mult": 2.3, "min_efficiency": 0.48, "rsi_max": 62.0},
    "CMCSA": {"adx_min": 32.0, "volume_mult": 2.3, "min_efficiency": 0.48, "rsi_max": 62.0},
}


@dataclass(frozen=True)
class NoiseLimits:
    adx_min: float
    volume_mult: float
    min_efficiency: float
    rsi_min: float
    rsi_max: float


def noise_limits(symbol: str, settings: Settings) -> NoiseLimits:
    key = str(symbol or "").upper()
    overrides = getattr(settings, "adx_threshold_overrides", {}) or {}
    base_adx = max(float(settings.adx_threshold), STOCK_ADX_FLOOR)
    base_vol = max(float(settings.sync_entry_volume_mult_stock), STOCK_VOLUME_FLOOR)
    noisy = NOISY_BREAKOUT_SYMBOLS.get(key)
    if key in overrides:
        adx_min = float(overrides[key])
    elif noisy is not None:
        adx_min = max(base_adx, float(noisy["adx_min"]))
    else:
        adx_min = base_adx
    rsi_max = float(noisy["rsi_max"]) if noisy and "rsi_max" in noisy else STOCK_RSI_MAX
    if noisy is not None:
        return NoiseLimits(
            adx_min=adx_min,
            volume_mult=max(base_vol, float(noisy["volume_mult"])),
            min_efficiency=float(noisy["min_efficiency"]),
            rsi_min=STOCK_RSI_MIN,
            rsi_max=rsi_max,
        )
    return NoiseLimits(
        adx_min=adx_min,
        volume_mult=base_vol,
        min_efficiency=STOCK_EFFICIENCY_MIN,
        rsi_min=STOCK_RSI_MIN,
        rsi_max=STOCK_RSI_MAX,
    )


def check_stock_entry_noise(
    symbol: str,
    bars: pd.DataFrame,
    settings: Settings,
) -> tuple[bool, str]:
    """True si la acción tiene tendencia suficiente para abrir. False = quedarse quieto."""
    limits = noise_limits(symbol, settings)
    if bars is None or bars.empty or "close" not in bars.columns:
        return False, "ruido sin velas — entrada omitida"

    period = int(settings.adx_period)
    dmi_now = last_dmi(bars, period)
    if dmi_now is None:
        return False, f"ruido ADX no disponible (periodo {period}) — entrada omitida"
    adx_value, plus_di, minus_di = dmi_now
    if adx_value <= limits.adx_min:
        return False, (
            f"ruido mercado lateral ADX {adx_value:.1f} <= {limits.adx_min:.1f} — entrada omitida"
        )
    if plus_di <= minus_di:
        return False, (
            f"ruido sin dirección alcista +DI {plus_di:.1f} <= -DI {minus_di:.1f} — entrada omitida"
        )

    vol_period = int(settings.volume_confirmation_period)
    vol_ok, vol_ratio = volume_vs_average(bars, vol_period, limits.volume_mult)
    vol_txt = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
    if vol_ok is not True:
        return False, (
            f"ruido volumen {vol_txt} < {limits.volume_mult:.2f}x — entrada omitida"
        )

    eff = efficiency_ratio(bars["close"], EFFICIENCY_LOOKBACK)
    if eff is None or eff < limits.min_efficiency:
        eff_txt = f"{eff:.2f}" if eff is not None else "n/a"
        return False, (
            f"ruido eficiencia {eff_txt} < {limits.min_efficiency:.2f} — entrada omitida"
        )

    rsi_period = int(settings.rsi_period)
    rsi_val = last_rsi(bars["close"], rsi_period)
    if rsi_val is None:
        return False, f"ruido RSI no disponible (periodo {rsi_period}) — entrada omitida"
    if rsi_val < limits.rsi_min or rsi_val > limits.rsi_max:
        return False, (
            f"ruido RSI {rsi_val:.1f} fuera [{limits.rsi_min:.0f},{limits.rsi_max:.0f}] "
            "— entrada omitida"
        )

    return True, (
        f"ruido OK ADX {adx_value:.1f}>{limits.adx_min:.1f} "
        f"vol {vol_txt} eficiencia {eff:.2f} RSI {rsi_val:.1f}"
    )
