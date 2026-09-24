"""Entrada calibrada unificada (acciones + cripto).

Lógica nueva, independiente del orquestador multi-régimen y de breakout.py.
Solo usa velas **cerradas**; alinea contexto HTF + confirmación + impulso en la vela de entrada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from bot.config import Settings
from bot.strategy.indicators import atr as atr_series
from bot.strategy.indicators import rsi as rsi_series

STRATEGY_TAG = "sync_entry"

_TF_SECONDS = {
    "1Min": 60,
    "3Min": 180,
    "5Min": 300,
    "9Min": 540,
    "15Min": 900,
    "30Min": 1800,
    "1Hour": 3600,
    "1Day": 86400,
}


def timeframe_seconds(label: str) -> int:
    return int(_TF_SECONDS.get(str(label), 300))


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def closed_bars_only(
    bars: pd.DataFrame,
    timeframe_label: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Excluye la vela en formación (timestamp = inicio de vela en Alpaca)."""
    if bars is None or bars.empty:
        return bars
    bar_secs = timeframe_seconds(timeframe_label)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    last_start = _as_utc(bars.index[-1])
    bar_end = last_start + timedelta(seconds=bar_secs)
    if now_utc < bar_end:
        trimmed = bars.iloc[:-1]
        return trimmed.copy() if not trimmed.empty else trimmed
    return bars.copy()


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.astype(float).ewm(span=span, adjust=False, min_periods=span).mean()


def _body_high(row: pd.Series) -> float:
    return float(max(row["open"], row["close"]))


def _body_frac(row: pd.Series) -> float:
    hi = float(row["high"])
    lo = float(row["low"])
    rng = hi - lo
    if rng <= 0:
        return 0.0
    return abs(float(row["close"]) - float(row["open"])) / rng


def _volume_median(bars: pd.DataFrame, period: int) -> float | None:
    if "volume" not in bars.columns or len(bars) < period:
        return None
    vol = bars["volume"].astype(float).iloc[-period:]
    med = float(vol.median())
    return med if np.isfinite(med) and med > 0 else None


@dataclass(frozen=True)
class SyncEntryDecision:
    allowed: bool
    reason: str
    confidence: float
    htf_trend: str
    sl_atr_mult: float


def _ema_stack_bull(bars: pd.DataFrame, fast: int, slow: int) -> tuple[bool, str]:
    if bars.empty or len(bars) < slow + 3:
        return False, "pocas velas HTF"
    close = bars["close"].astype(float)
    ema_f = _ema(close, fast)
    ema_s = _ema(close, slow)
    c = float(close.iloc[-1])
    ef = float(ema_f.iloc[-1])
    es = float(ema_s.iloc[-1])
    es_prev = float(ema_s.iloc[-4])
    if not (np.isfinite(c) and np.isfinite(ef) and np.isfinite(es)):
        return False, "EMA HTF incompleta"
    slope_ok = es > es_prev
    if c > ef > es and slope_ok:
        return True, f"HTF alcista EMA{fast}>{slow} pendiente+"
    if c > es and ef > es:
        return True, f"HTF alcista relajada close>EMA{slow}"
    return False, f"HTF no alcista close={c:.4f} ef={ef:.4f} es={es:.4f}"


def _impulse_trigger(
    bars: pd.DataFrame,
    *,
    pivot_bars: int,
    body_min: float,
    rsi_min: float,
    rsi_max: float,
    vol_mult: float,
    atr_period: int,
) -> tuple[bool, str, float]:
    """Ruptura del máximo de cuerpos previos + vela decisiva + volumen + RSI + ATR en expansión."""
    need = max(pivot_bars + 5, atr_period + 5, 25)
    if bars.empty or len(bars) < need:
        return False, "pocas velas entrada", 0.0
    window = bars.iloc[-(pivot_bars + 1) :]
    signal = window.iloc[-1]
    prior = window.iloc[:-1]
    if prior.empty:
        return False, "sin historia pivot", 0.0

    body_frac = _body_frac(signal)
    if float(signal["close"]) <= float(signal["open"]):
        return False, "vela señal no alcista", 0.0
    if body_frac < body_min:
        return False, f"cuerpo débil {body_frac:.0%}<{body_min:.0%}", 0.0

    pivot_level = max(_body_high(prior.iloc[i]) for i in range(len(prior)))
    close_sig = float(signal["close"])
    if close_sig <= pivot_level:
        return False, f"sin ruptura cuerpos max={pivot_level:.4f}", 0.0

    closes = bars["close"].astype(float)
    rsi_val = rsi_series(closes, 14)
    r = float(rsi_val.iloc[-1]) if not rsi_val.empty else float("nan")
    if not np.isfinite(r) or r < rsi_min or r > rsi_max:
        return False, f"RSI fuera rango {r:.1f} not in [{rsi_min},{rsi_max}]", 0.0

    vol_med = _volume_median(bars.iloc[:-1], 20)
    if vol_med is not None:
        vol_sig = float(signal.get("volume", 0) or 0)
        if vol_sig < vol_med * vol_mult:
            return False, f"volumen bajo {vol_sig:.0f}<{vol_med * vol_mult:.0f}", 0.0

    atr_s = atr_series(bars, atr_period).dropna()
    if len(atr_s) < 6:
        return False, "ATR insuficiente", 0.0
    atr_now = float(atr_s.iloc[-1])
    atr_ref = float(atr_s.iloc[-6])
    if atr_now < atr_ref * 0.97:
        return False, "ATR sin expansión", 0.0

    chop = 0
    tail = bars.iloc[-4:-1]
    for _, row in tail.iterrows():
        rng = float(row["high"]) - float(row["low"])
        atr_local = float(atr_s.iloc[-2]) if len(atr_s) >= 2 else atr_now
        if atr_local > 0 and rng < 0.35 * atr_local:
            chop += 1
    if chop >= 3:
        return False, "mercado plano (3 velas estrechas)", 0.0

    conf = 55.0 + min(25.0, (close_sig / pivot_level - 1.0) * 10_000.0)
    conf += min(10.0, (body_frac - body_min) * 40.0)
    conf += min(10.0, max(0.0, (r - rsi_min) / max(1.0, rsi_max - rsi_min)) * 10.0)
    return True, f"impulso ruptura cuerpos>{pivot_level:.4f} RSI={r:.1f}", min(100.0, conf)


def evaluate_sync_entry(
    *,
    entry_bars: pd.DataFrame,
    regime_bars: pd.DataFrame,
    confirm_bars: pd.DataFrame,
    entry_tf: str,
    regime_tf: str,
    confirm_tf: str,
    has_long: bool,
    is_crypto: bool,
    settings: Settings,
    now: datetime | None = None,
) -> SyncEntryDecision:
    if has_long:
        return SyncEntryDecision(False, "posición larga abierta", 0.0, "n/a", 0.0)

    entry_c = closed_bars_only(entry_bars, entry_tf, now)
    regime_c = closed_bars_only(regime_bars, regime_tf, now)
    confirm_c = closed_bars_only(confirm_bars, confirm_tf, now)

    if entry_c.empty or len(entry_c) < 30:
        return SyncEntryDecision(False, "sin velas entrada cerradas", 0.0, "n/a", 0.0)

    fast = settings.crypto_sma_fast if is_crypto else settings.sma_fast
    slow = settings.crypto_sma_slow if is_crypto else settings.sma_slow
    slow = max(slow, fast + 2)

    regime_ok, regime_detail = _ema_stack_bull(regime_c, fast, slow)
    if not regime_ok:
        return SyncEntryDecision(False, f"régimen | {regime_detail}", 0.0, "bear", 0.0)

    confirm_ok, confirm_detail = _ema_stack_bull(confirm_c, fast, slow)
    if not confirm_ok:
        return SyncEntryDecision(
            False, f"confirmación {confirm_tf} | {confirm_detail}", 0.0, "sideways", 0.0
        )

    pivot = int(settings.sync_entry_pivot_bars)
    body_min = float(settings.sync_entry_body_min_frac)
    rsi_min = float(settings.sync_entry_rsi_min)
    rsi_max = float(settings.sync_entry_rsi_max)
    vol_mult = float(settings.sync_entry_volume_mult_crypto if is_crypto else settings.sync_entry_volume_mult_stock)

    trig_ok, trig_detail, conf = _impulse_trigger(
        entry_c,
        pivot_bars=pivot,
        body_min=body_min,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        vol_mult=vol_mult,
        atr_period=int(settings.atr_period),
    )
    if not trig_ok:
        return SyncEntryDecision(False, f"entrada | {trig_detail}", 0.0, "bull", 0.0)

    conf += 12.0  # régimen + confirmación alineados
    sl_mult = float(settings.crypto_atr_sl_mult if is_crypto else settings.stock_atr_sl_mult)
    reason = f"sync | {regime_detail} | {confirm_detail} | {trig_detail}"
    return SyncEntryDecision(True, reason, conf, "bull", sl_mult)
