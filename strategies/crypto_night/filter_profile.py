"""Perfiles de candados crypto night — mismos 5 filtros, distinta exigencia."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CryptoNightFilters:
    vol_pct_low: float
    vol_pct_high: float
    bias_swing_lookback_4h: int
    bias_swing_lookback_1d: int
    bias_relaxed_structure: bool
    eth_inherit_btc_bias_only: bool
    entry_delay_minutes_after_us_close: int
    setup_min_volume_ratio: float
    setup_v1_max_bars: int
    setup_try_v2_fallback: bool
    quality_min_score: int
    quality_min_theoretical_r: float
    quality_max_spread_pct: float


STRICT = CryptoNightFilters(
    vol_pct_low=30.0,
    vol_pct_high=70.0,
    bias_swing_lookback_4h=6,
    bias_swing_lookback_1d=5,
    bias_relaxed_structure=False,
    eth_inherit_btc_bias_only=False,
    entry_delay_minutes_after_us_close=0,
    setup_min_volume_ratio=1.5,
    setup_v1_max_bars=3,
    setup_try_v2_fallback=False,
    quality_min_score=4,
    quality_min_theoretical_r=1.8,
    quality_max_spread_pct=0.0005,
)

# Paper: más trades; riesgo acotado (stops, kill, sesión y calidad mínima intactos).
RELAXED = CryptoNightFilters(
    vol_pct_low=20.0,
    vol_pct_high=80.0,
    bias_swing_lookback_4h=4,
    bias_swing_lookback_1d=4,
    bias_relaxed_structure=True,
    eth_inherit_btc_bias_only=True,
    entry_delay_minutes_after_us_close=90,
    setup_min_volume_ratio=1.2,
    setup_v1_max_bars=10,
    setup_try_v2_fallback=True,
    quality_min_score=3,
    quality_min_theoretical_r=1.5,
    quality_max_spread_pct=0.0010,
)

# Paper/live alto volumen: vol amplia, vol setup 1.05x, calidad mínima 2/5, sin delay post-cierre.
AGGRESSIVE = CryptoNightFilters(
    vol_pct_low=10.0,
    vol_pct_high=90.0,
    bias_swing_lookback_4h=3,
    bias_swing_lookback_1d=3,
    bias_relaxed_structure=True,
    eth_inherit_btc_bias_only=True,
    entry_delay_minutes_after_us_close=0,
    setup_min_volume_ratio=1.05,
    setup_v1_max_bars=14,
    setup_try_v2_fallback=True,
    quality_min_score=2,
    quality_min_theoretical_r=1.15,
    quality_max_spread_pct=0.0015,
)


def parse_filter_profile(raw: str | None) -> str:
    key = (raw or "aggressive").strip().lower().replace("-", "_")
    if key in {"strict", "default", "fortress", "normal"}:
        return "strict"
    if key in {"relaxed", "flojo", "loose", "paper_relaxed", "more_trades"}:
        return "relaxed"
    if key in {"aggressive", "high_volume", "volume", "paper_aggressive"}:
        return "aggressive"
    raise ValueError(
        f"CRYPTO_NIGHT_FILTER_PROFILE invalido: {raw!r} (strict | relaxed | aggressive)"
    )


def filters_for_profile(profile: str) -> CryptoNightFilters:
    if profile == "aggressive":
        return AGGRESSIVE
    if profile == "relaxed":
        return RELAXED
    return STRICT
