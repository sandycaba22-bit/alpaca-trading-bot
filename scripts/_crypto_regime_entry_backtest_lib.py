"""Backtest entrada cripto: régimen HTF + expansión ATR + asimétrico 1H/4H (solo research)."""

from __future__ import annotations

import pandas as pd

from bot.config import Settings
from bot.strategy.crypto_asymmetric import (
    evaluate_entry_at_bar,
    params_from_settings,
    resample_4h_from_1h,
)
from bot.strategy.indicators import atr as atr_series
from bot.strategy.crypto_regime_entry import (
    CryptoRegimeGate,
    atr_expansion_at_series,
    htf_trend_at,
    resample_1d_from_1h,
)

from _crypto_asymmetric_backtest_lib import (
    AsymmetricTrade,
    _simulate_bar_exits,
    _size_qty,
    summarize_trades,
)
from _stocks_asymmetric_backtest_lib import OOS_START, _entry_bar_volume_ok

CRYPTO_IS_START = pd.Timestamp("2021-01-01", tz="UTC")

GATE_BASELINE = CryptoRegimeGate(name="baseline_asim", htf_mode="none", atr_expansion_mult=None)
GATE_VOL2X_ONLY = CryptoRegimeGate(name="vol2x_asim", htf_mode="none", atr_expansion_mult=None)
GATE_TREND_4H_ATR12 = CryptoRegimeGate(
    name="trend_4h_atr_exp_1.2",
    htf_mode="4h",
    atr_expansion_mult=1.2,
    atr_expansion_period=20,
)
GATE_TREND_1D_ATR12 = CryptoRegimeGate(
    name="trend_1d_atr_exp_1.2",
    htf_mode="1d",
    atr_expansion_mult=1.2,
    atr_expansion_period=20,
)
GATE_TREND_4H_ATR15 = CryptoRegimeGate(
    name="trend_4h_atr_exp_1.3",
    htf_mode="4h",
    atr_expansion_mult=1.3,
    atr_expansion_period=20,
)
GATE_FULL_4H_ATR12 = CryptoRegimeGate(
    name="trend_4h_atr_exp_1.2_strict",
    htf_mode="4h",
    htf_fast=9,
    htf_slow=50,
    atr_expansion_mult=1.2,
    atr_expansion_period=20,
)

WF_OOS_STARTS = (
    "2022-07-01",
    "2023-04-01",
    "2024-01-01",
    "2024-10-01",
    "2025-07-01",
)


def gate_sma50_atr_exp(atr_expansion_mult: float) -> CryptoRegimeGate:
    tag = f"trend_4h_sma50_atr_exp_{atr_expansion_mult:.2f}"
    return CryptoRegimeGate(
        name=tag,
        htf_mode="4h",
        htf_fast=9,
        htf_slow=50,
        atr_expansion_mult=atr_expansion_mult,
        atr_expansion_period=20,
    )


def walkforward_windows(
    end: pd.Timestamp,
) -> list[tuple[int, pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    windows: list[tuple[int, pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]] = []
    oos_starts = [pd.Timestamp(s, tz="UTC") for s in WF_OOS_STARTS]
    for i, oos_start in enumerate(oos_starts):
        if oos_start >= end:
            break
        oos_end = oos_starts[i + 1] - pd.Timedelta(hours=1) if i + 1 < len(oos_starts) else end
        if oos_end <= oos_start:
            continue
        is_end = oos_start - pd.Timedelta(hours=1)
        if is_end <= CRYPTO_IS_START:
            continue
        windows.append((i + 1, CRYPTO_IS_START, is_end, oos_start, oos_end))
    return windows


def run_crypto_regime_period(
    settings: Settings,
    bars: pd.DataFrame,
    symbol: str,
    gate: CryptoRegimeGate,
    *,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
    vol2x: bool = False,
    vol_mult: float = 2.0,
    vol_period: int = 20,
) -> list[AsymmetricTrade]:
    params = params_from_settings(settings)
    bars_4h = resample_4h_from_1h(bars)
    bars_1d = resample_1d_from_1h(bars)
    cash0 = float(settings.backtest_cash)
    friction = (fee_pct + slippage_pct) / 100.0
    period_start_i = int(bars.index.searchsorted(period_start, side="left"))
    period_end_i = int(bars.index.searchsorted(period_end, side="right")) - 1
    period_end_i = max(period_start_i, min(period_end_i, len(bars) - 1))

    trades: list[AsymmetricTrade] = []
    trades_today: dict[str, int] = {}
    in_pos_until = period_start_i - 1
    atr_1h = (
        atr_series(bars, gate.atr_period)
        if gate.atr_expansion_mult is not None and gate.atr_expansion_mult > 0
        else None
    )

    for i in range(max(period_start_i, 2), period_end_i + 1):
        if i <= in_pos_until:
            continue
        sig_i = i - 1
        ts = bars.index[sig_i]

        if vol2x and not _entry_bar_volume_ok(bars, sig_i, vol_period, vol_mult):
            continue

        ok_trend, _ = htf_trend_at(bars_4h, bars_1d, ts, gate)
        if not ok_trend:
            continue
        if atr_1h is not None:
            ok_atr, _ = atr_expansion_at_series(atr_1h, i, gate)
            if not ok_atr:
                continue

        day_key = str(ts.date())
        n_day = trades_today.get(day_key, 0)
        sig = evaluate_entry_at_bar(
            bars,
            bars_4h,
            i,
            params,
            has_long=False,
            trades_today=n_day,
        )
        if not sig.allowed:
            continue

        entry = float(bars["open"].iloc[i])
        atr_val = float(sig.atr_value or 0.0)
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
        trades_today[day_key] = n_day + 1
        in_pos_until = exit_j

    return trades


def metrics_for_period(
    settings: Settings,
    bars: pd.DataFrame,
    symbol: str,
    gate: CryptoRegimeGate,
    *,
    scope_start: pd.Timestamp,
    scope_end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
    vol2x: bool = False,
) -> dict[str, float]:
    tr = run_crypto_regime_period(
        settings,
        bars,
        symbol,
        gate,
        period_start=scope_start,
        period_end=scope_end,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        vol2x=vol2x,
    )
    m = summarize_trades(tr, float(settings.backtest_cash))
    m["symbol"] = symbol
    return m
