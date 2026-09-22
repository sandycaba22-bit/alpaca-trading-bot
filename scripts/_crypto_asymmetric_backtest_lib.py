"""Backtest cripto asimétrico 1H/4H — SL ATR ceñido, trailing ancho, sin TP fijo."""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, Settings
from bot.strategy.crypto_asymmetric import (
    CryptoAsymmetricParams,
    evaluate_entry_at_bar,
    params_from_settings,
    resample_4h_from_1h,
)
from bot.strategy.indicators import atr as atr_series

CACHE = PROJECT_ROOT / "data" / "bars_cache"
HISTORY_START = pd.Timestamp("2021-01-01", tz="UTC")
OOS_START = pd.Timestamp("2025-01-01", tz="UTC")


@dataclass
class AsymmetricTrade:
    symbol: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_gross: float
    pnl_net: float
    exit_reason: str


def _cache_path(symbol: str, tf: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_{tf}.pkl"


def load_1h(
    market: MarketDataService, symbol: str, start: datetime, end: datetime
) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, "1Hour")
    if path.exists():
        bars = pickle.loads(path.read_bytes())
        if isinstance(bars, pd.DataFrame) and not bars.empty:
            print(f"cache {symbol} 1Hour bars={len(bars)}")
            return bars
    print(f"fetch {symbol} 1Hour ...")
    bars = market.get_bars_range(symbol, "1Hour", start=start, end=end)
    if not bars.empty:
        path.write_bytes(pickle.dumps(bars, protocol=pickle.HIGHEST_PROTOCOL))
    return bars


def _size_qty(settings: Settings, cash: float, price: float, sl_atr: float, atr: float) -> float:
    if price <= 0 or atr <= 0:
        return 0.0
    cap = min(cash * settings.position_size_pct, settings.max_notional_per_order, cash)
    sl_dist = atr * sl_atr
    if settings.use_fixed_risk_sizing and sl_dist > 0:
        risk = cash * settings.risk_percent_per_trade
        qty = risk / sl_dist
        return max(0.0, min(qty, cap / price))
    return cap / price


def _simulate_bar_exits(
    bars: pd.DataFrame,
    ei: int,
    entry: float,
    qty: float,
    atr_entry: float,
    params: CryptoAsymmetricParams,
    period_end_i: int,
) -> tuple[float, int, str]:
    sl = entry - params.sl_atr_mult * atr_entry
    peak = entry
    trail_active = False
    exit_px = None
    exit_j = period_end_i
    reason = "period_end"

    for j in range(ei + 1, min(period_end_i + 1, len(bars))):
        lo = float(bars["low"].iloc[j])
        hi = float(bars["high"].iloc[j])
        cl = float(bars["close"].iloc[j])
        peak = max(peak, hi, cl)
        if not trail_active and peak >= entry + params.trail_activate_atr_mult * atr_entry:
            trail_active = True
        if trail_active:
            trail_sl = peak - params.trail_atr_mult * atr_entry
            sl = max(sl, trail_sl)
        if lo <= sl:
            exit_px = sl
            exit_j = j
            reason = "trail" if trail_active else "sl"
            break

    if exit_px is None:
        exit_px = float(bars["close"].iloc[min(period_end_i, len(bars) - 1)])
        exit_j = min(period_end_i, len(bars) - 1)
    return exit_px, exit_j, reason


def run_backtest(
    settings: Settings,
    market: MarketDataService,
    symbol: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    fee_pct: float,
    slippage_pct: float,
) -> tuple[list[AsymmetricTrade], dict[str, float]]:
    params = params_from_settings(settings)
    start_dt = start.to_pydatetime()
    end_dt = end.to_pydatetime()
    bars = load_1h(market, symbol, start_dt, end_dt)
    if bars.empty or len(bars) < 300:
        return [], {}

    bars_4h = resample_4h_from_1h(bars)
    cash0 = float(settings.backtest_cash)
    friction = (fee_pct + slippage_pct) / 100.0
    period_start_i = int(bars.index.searchsorted(start, side="left"))
    period_end_i = int(bars.index.searchsorted(end, side="right")) - 1
    period_end_i = max(period_start_i, min(period_end_i, len(bars) - 1))

    trades: list[AsymmetricTrade] = []
    trades_today: dict[str, int] = {}
    in_pos_until = period_start_i - 1

    for i in range(max(period_start_i, 1), period_end_i + 1):
        if i <= in_pos_until:
            continue
        day_key = str(bars.index[i - 1].date())
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

    metrics = summarize_trades(trades, cash0)
    metrics["symbol"] = symbol
    return trades, metrics


def summarize_trades(trades: list[AsymmetricTrade], cash0: float) -> dict[str, float]:
    if not trades or cash0 <= 0:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_return_net_pct": 0.0,
            "avg_win_net": 0.0,
            "avg_loss_net": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
        }
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gross_w = sum(t.pnl_net for t in wins)
    gross_l = sum(abs(t.pnl_net) for t in losses)
    pf = (gross_w / gross_l) if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)
    avg_win = (sum(t.pnl_net for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_net for t in losses) / len(losses)) if losses else 0.0
    return {
        "trades": float(len(trades)),
        "win_rate_pct": len(wins) / len(trades) * 100.0,
        "profit_factor": pf,
        "total_return_net_pct": sum(t.pnl_net for t in trades) / cash0 * 100.0,
        "avg_win_net": avg_win,
        "avg_loss_net": avg_loss,
        "avg_win_r": (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0,
        "avg_loss_r": 1.0 if avg_loss != 0 else 0.0,
    }
