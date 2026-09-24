"""Entrada combinada (research): vol_2x + ADX + pullback EMA + vol pullback + skip apertura.

No toca producción. Salidas: acciones asimétricas 1.2/2.75; cripto asimétrico 1H exits.
Sin lógica 3/6/9 ni multi-régimen breakout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot.config import Settings
from bot.risk.stops import StopTakeProfitPolicy
from bot.strategy.base import Signal
from bot.strategy.crypto_asymmetric import (
    CryptoAsymmetricParams,
    params_from_settings,
    resample_4h_from_1h,
)
from bot.strategy.indicators import last_adx, last_atr
from bot.strategy.multi_tf_analysis import analyze_trend
from bot.strategy.pullback import detect_pullback

from _crypto_asymmetric_backtest_lib import (
    _simulate_bar_exits,
    _size_qty,
    load_1h,
)
from _stocks_asymmetric_backtest_lib import (
    CONFIRM_TF,
    ENTRY_TF,
    OOS_START,
    EntryVariant,
    _entry_bar_volume_ok,
    _htf_until,
    load_bars,
    policy_asymmetric,
    precompute_entries,
    simulate_trades,
    summarize_trades,
)

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CombinedEntryProfile:
    name: str
    volume_mult: float = 2.0
    volume_period: int = 20
    adx_min: float | None = 20.0
    market_open_skip_minutes: int = 30
    crypto_hour0_skip_minutes: int = 0
    pullback_volume_mult: float | None = None


PROFILE_VOL2X_PULLBACK = CombinedEntryProfile(
    name="vol2x_pullback",
    adx_min=None,
    market_open_skip_minutes=0,
    crypto_hour0_skip_minutes=0,
    # Solo vol_2x en vela de entrada; sin filtro extra de vol en el pullback.
    pullback_volume_mult=1.0,
)

PROFILE_COMBINED_FULL = CombinedEntryProfile(
    name="combined_full",
    adx_min=20.0,
    market_open_skip_minutes=30,
    crypto_hour0_skip_minutes=60,
    pullback_volume_mult=1.2,
)


def _in_us_open_chop(ts: pd.Timestamp, skip_minutes: int) -> bool:
    if skip_minutes <= 0:
        return False
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    et = ts.tz_convert(NY)
    if et.weekday() >= 5:
        return False
    session_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
    return session_open <= et < session_open + pd.Timedelta(minutes=skip_minutes)


def _in_crypto_hour0_chop(ts: pd.Timestamp, skip_minutes: int) -> bool:
    if skip_minutes <= 0:
        return False
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    utc = ts.tz_convert("UTC")
    day_start = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start <= utc < day_start + pd.Timedelta(minutes=skip_minutes)


def precompute_stocks_combined(
    settings: Settings,
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    sma_slow_by_symbol: dict[str, int],
    profile: CombinedEntryProfile,
) -> list[int]:
    slow = int(sma_slow_by_symbol.get(symbol.upper(), settings.sma_slow))
    lookback = max(int(settings.lookback_bars), 80)
    start_i = max(
        lookback + 1,
        slow + 3,
        settings.adx_period * 2 + 2,
        settings.pullback_ema_period + 5,
    )
    asym = policy_asymmetric(settings)
    entries: list[int] = []
    in_pos_until = -1
    n = len(bars)
    htf_pos = 0
    htf_len = len(htf) if htf is not None and not htf.empty else 0

    eff = settings
    if profile.pullback_volume_mult is not None:
        from dataclasses import replace

        eff = replace(eff, pullback_volume_mult=float(profile.pullback_volume_mult))

    for i in range(start_i, n):
        if i <= in_pos_until:
            continue
        if _in_us_open_chop(bars.index[i], profile.market_open_skip_minutes):
            continue
        hist = bars.iloc[i - lookback : i]
        if len(hist) < lookback // 2:
            continue
        sig_i = i - 1
        ts = bars.index[sig_i]
        if htf_len:
            while htf_pos < htf_len and htf.index[htf_pos] <= ts:
                htf_pos += 1
            htf_hist = _htf_until(htf, ts, htf_pos)
        else:
            htf_hist = None
        trend, _ = analyze_trend(
            htf_hist if htf_hist is not None and len(htf_hist) > 20 else hist
        )
        if trend == "bear":
            continue
        if profile.adx_min is not None:
            adx_v = last_adx(hist, settings.adx_period)
            if adx_v is None or adx_v < float(profile.adx_min):
                continue
        signal, _ = detect_pullback(
            hist,
            settings=eff,
            has_long=False,
            slow_period=slow,
            symbol=symbol,
        )
        if signal is not Signal.BUY:
            continue
        if not _entry_bar_volume_ok(bars, sig_i, profile.volume_period, profile.volume_mult):
            continue

        entries.append(i)
        entry_px = float(bars["open"].iloc[i])
        atr_e = last_atr(hist, settings.atr_period)
        lv = asym.levels(entry_px, 1.0, entry_px, atr_e)
        sl, tp, peak = lv.stop_price, lv.take_profit_price, entry_px
        exit_j = n - 1
        for j in range(i + 1, n):
            h = float(bars["high"].iloc[j])
            lo = float(bars["low"].iloc[j])
            c = float(bars["close"].iloc[j])
            peak = max(peak, h, c)
            new_sl, _ = asym.trailing_candidate(entry_px, 1.0, peak, sl, atr_e)
            if new_sl is not None:
                sl = new_sl
            if sl > 0 and lo <= sl:
                exit_j = j
                break
            if tp > 0 and h >= tp:
                exit_j = j
                break
        in_pos_until = exit_j
    return entries


def evaluate_combined_crypto_at_bar(
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    bar_index: int,
    params: CryptoAsymmetricParams,
    settings: Settings,
    profile: CombinedEntryProfile,
    *,
    slow_period: int,
) -> tuple[bool, str, float | None]:
    i = bar_index
    if i < max(params.htf_slow + 5, settings.adx_period * 2, 30):
        return False, "warmup", None
    if _in_crypto_hour0_chop(bars_1h.index[i], profile.crypto_hour0_skip_minutes):
        return False, "skip hora 0 UTC", None

    sig_i = i - 1
    hist = bars_1h.iloc[max(0, sig_i - int(settings.lookback_bars)) : sig_i + 1]
    if len(hist) < 25:
        return False, "pocas velas", None

    ts = bars_1h.index[sig_i]
    htf_hist = bars_4h.loc[bars_4h.index <= ts]
    trend, _ = analyze_trend(htf_hist, fast=params.htf_fast, slow=params.htf_slow)
    if trend != "bull":
        return False, f"4H no alcista ({trend})", None

    if profile.adx_min is not None:
        adx_v = last_adx(hist, settings.adx_period)
        if adx_v is None or adx_v < float(profile.adx_min):
            return False, "ADX bajo", None

    eff = settings
    if profile.pullback_volume_mult is not None:
        from dataclasses import replace

        eff = replace(eff, pullback_volume_mult=float(profile.pullback_volume_mult))

    signal, _ = detect_pullback(
        hist,
        settings=eff,
        has_long=False,
        slow_period=slow_period,
        symbol="",
    )
    if signal is not Signal.BUY:
        return False, "sin pullback EMA", None
    if not _entry_bar_volume_ok(bars_1h, sig_i, profile.volume_period, profile.volume_mult):
        return False, "vol señal < 2x", None

    atr_e = last_atr(hist, params.atr_period)
    return True, "combined crypto pullback", atr_e


def run_crypto_combined_period(
    settings: Settings,
    bars: pd.DataFrame,
    symbol: str,
    profile: CombinedEntryProfile,
    *,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
) -> list:
    from _crypto_asymmetric_backtest_lib import AsymmetricTrade

    params = params_from_settings(settings)
    bars_4h = resample_4h_from_1h(bars)
    slow = params.htf_slow
    cash0 = float(settings.backtest_cash)
    friction = (fee_pct + slippage_pct) / 100.0
    period_start_i = int(bars.index.searchsorted(period_start, side="left"))
    period_end_i = int(bars.index.searchsorted(period_end, side="right")) - 1
    period_end_i = max(period_start_i, min(period_end_i, len(bars) - 1))

    trades: list[AsymmetricTrade] = []
    in_pos_until = period_start_i - 1

    for i in range(max(period_start_i, 2), period_end_i + 1):
        if i <= in_pos_until:
            continue
        ok, _, atr_val = evaluate_combined_crypto_at_bar(
            bars, bars_4h, i, params, settings, profile, slow_period=slow
        )
        if not ok or not atr_val or atr_val <= 0:
            continue
        entry = float(bars["open"].iloc[i])
        qty = _size_qty(settings, cash0, entry, params.sl_atr_mult, atr_val)
        if qty <= 0:
            continue
        exit_px, exit_j, reason = _simulate_bar_exits(
            bars, i, entry, qty, atr_val, params, period_end_i
        )
        notional_in = entry * qty
        notional_out = exit_px * qty
        pnl_gross = (exit_px - entry) * qty
        pnl_net = pnl_gross - (notional_in + notional_out) * friction
        trades.append(
            AsymmetricTrade(
                symbol=symbol,
                entry_idx=i,
                exit_idx=exit_j,
                entry_price=entry,
                exit_price=exit_px,
                qty=qty,
                pnl_gross=pnl_gross,
                pnl_net=pnl_net,
                exit_reason=reason,
            )
        )
        in_pos_until = exit_j
    return trades


def metrics_stocks_scope(
    settings: Settings,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    symbol: str,
    sma_map: dict[str, int],
    entries: list[int],
    *,
    scope_start: pd.Timestamp,
    scope_end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
) -> dict[str, float]:
    asym = policy_asymmetric(settings)
    cash0 = float(settings.backtest_cash)
    tr = simulate_trades(
        settings,
        bars,
        entries,
        asym,
        symbol=symbol,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        period_start=scope_start,
        period_end=scope_end,
    )
    m = summarize_trades(tr, cash0)
    m["symbol"] = symbol
    return m


def metrics_legacy_vol2x(
    settings: Settings,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    symbol: str,
    sma_map: dict[str, int],
    *,
    scope_start: pd.Timestamp,
    scope_end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
) -> dict[str, float]:
    variant = EntryVariant(name="vol_2x", volume_mult=2.0)
    entries = precompute_entries(
        settings, symbol, bars, htf, sma_map, variant=variant, quiet=True
    )
    return metrics_stocks_scope(
        settings,
        bars,
        htf,
        symbol,
        sma_map,
        entries,
        scope_start=scope_start,
        scope_end=scope_end,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
    )


def pf_str(pf: float) -> str:
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"
