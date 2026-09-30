"""Simulación offline por sesiones nocturnas (mismos 5 candados)."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from strategies.crypto_night.bias import resolve_night_bias
from strategies.crypto_night.quality import score_setup
from strategies.crypto_night.risk import NightRiskState, RiskLimits, can_open_trade, register_trade_result
from strategies.crypto_night.session import entries_allowed, in_night_trading_window
from strategies.crypto_night.types import RejectReason, SimulatedTrade, TradeSide
from strategies.crypto_night.variants import SweepVariant, find_setup_for_variant
from strategies.crypto_night.volatility import atr_percent_series, volatility_gate

logger = logging.getLogger(__name__)

FEE_SLIP_R = 0.15


@dataclass
class VariantStats:
    variant: str
    signals: int = 0
    trades: int = 0
    wins: int = 0
    r_net_sum: float = 0.0
    r_net_list: list[float] = field(default_factory=list)
    rejections: Counter = field(default_factory=Counter)
    profit_locks: int = 0
    kill_switches: int = 0
    max_dd_night: float = 0.0


def _vol_ma(bars_15m: pd.DataFrame, period: int = 20) -> pd.Series:
    return bars_15m["volume"].astype(float).rolling(period, min_periods=period).mean()


def _simulate_exit(
    bars_15m: pd.DataFrame,
    entry_idx: int,
    setup,
    *,
    max_bars: int = 12,
    tp_reward_risk: float = 2.5,
    scale_at_1r: bool = False,
) -> tuple[float, str]:
    """Salida: TP fijo tp_reward_risk × R, o legacy scale @1R si scale_at_1r."""
    entry = setup.limit_price
    risk = abs(entry - setup.stop_price)
    if risk <= 0:
        return 0.0, "invalid_risk"
    from strategies.crypto_night.asymmetric_tp import take_profit_from_stop

    side = setup.side
    side_s = side.value if hasattr(side, "value") else str(side)
    tp_px = take_profit_from_stop(
        entry_price=entry,
        stop_price=setup.stop_price,
        side=side_s,
        reward_risk=tp_reward_risk,
    )
    partial_taken = False
    runner_sl = setup.stop_price
    best_r = 0.0
    end = min(entry_idx + max_bars, len(bars_15m) - 1)
    for j in range(entry_idx + 1, end + 1):
        row = bars_15m.iloc[j]
        hi, lo = float(row.high), float(row.low)
        if side == TradeSide.LONG:
            if tp_px > 0 and hi >= tp_px:
                return float(tp_reward_risk) - FEE_SLIP_R, "take_profit"
            if lo <= runner_sl:
                r = (runner_sl - entry) / risk
                if partial_taken:
                    return 0.5 * 1.0 + 0.5 * r - FEE_SLIP_R, "stop"
                return r - FEE_SLIP_R, "stop"
            cur_r = (hi - entry) / risk
        else:
            if tp_px > 0 and lo <= tp_px:
                return float(tp_reward_risk) - FEE_SLIP_R, "take_profit"
            if hi >= runner_sl:
                r = (entry - runner_sl) / risk
                if partial_taken:
                    return 0.5 * 1.0 + 0.5 * r - FEE_SLIP_R, "stop"
                return r - FEE_SLIP_R, "stop"
            cur_r = (entry - lo) / risk
        best_r = max(best_r, cur_r)
        if scale_at_1r and not partial_taken and cur_r >= 1.0:
            partial_taken = True
            runner_sl = entry + 0.3 * risk if side == TradeSide.LONG else entry - 0.3 * risk
        if scale_at_1r and partial_taken and cur_r >= 2.0:
            runner_sl = (
                entry + 1.5 * risk if side == TradeSide.LONG else entry - 1.5 * risk
            )
    if best_r < 0.5:
        return best_r - FEE_SLIP_R, "time_stop"
    return best_r - FEE_SLIP_R, "time_exit"


def _night_session_starts(bars_15m: pd.DataFrame) -> list[pd.Timestamp]:
    """Aproxima inicios de sesión nocturna (primera vela tras US close)."""
    starts: list[pd.Timestamp] = []
    last_day = None
    for ts in bars_15m.index:
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if not in_night_trading_window(dt):
            continue
        day = ts.date()
        if day != last_day and entries_allowed(dt):
            starts.append(ts)
            last_day = day
    return starts


def run_variant_backtest(
    variant: SweepVariant,
    *,
    symbol: str,
    bars_15m: pd.DataFrame,
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    bars_1d: pd.DataFrame,
    oos_start: pd.Timestamp | None = None,
    mode_min_score: int = 4,
    filters=None,
    limits: RiskLimits | None = None,
    tp_reward_risk: float = 2.5,
    scale_at_1r: bool = False,
    vol_pct_low: float = 30.0,
    vol_pct_high: float = 70.0,
    min_volume_ratio: float = 1.5,
    min_theoretical_r: float = 1.8,
    max_spread_pct: float = 0.0005,
) -> VariantStats:
    stats = VariantStats(variant=variant.value)
    if bars_15m.empty:
        return stats
    vol_ma = _vol_ma(bars_15m)
    atr_pct_s = atr_percent_series(bars_1h)
    if filters is not None:
        vol_pct_low = filters.vol_pct_low
        vol_pct_high = filters.vol_pct_high
        mode_min_score = filters.quality_min_score
        min_theoretical_r = filters.quality_min_theoretical_r
        max_spread_pct = filters.quality_max_spread_pct
        min_volume_ratio = filters.setup_min_volume_ratio
    limits = limits or RiskLimits(limits_enabled=False, max_trades_night=0)
    state = NightRiskState()

    for ts in bars_15m.index:
        dt = ts.to_pydatetime()
        if oos_start is not None and ts < oos_start:
            continue
        if not entries_allowed(dt):
            continue
        stats.signals += 1

        vgate, atr_now, _ = volatility_gate(
            bars_1h, ts, pct_low=vol_pct_low, pct_high=vol_pct_high
        )
        if not vgate.ok:
            stats.rejections[vgate.reject or RejectReason.VOLATILITY] += 1
            continue

        b4 = bars_4h.loc[bars_4h.index <= ts]
        b1 = bars_1d.loc[bars_1d.index <= ts]
        relaxed = filters.bias_relaxed_structure if filters is not None else False
        lb4 = filters.bias_swing_lookback_4h if filters is not None else 6
        lb1 = filters.bias_swing_lookback_1d if filters is not None else 5
        bgate, bias = resolve_night_bias(
            b4,
            b1,
            symbol,
            bias_mode="4h_only",
            swing_lookback_4h=lb4,
            swing_lookback_1d=lb1,
            relaxed_structure=relaxed,
        )
        if not bgate.ok or bias is None:
            stats.rejections[RejectReason.BIAS] += 1
            continue

        session_start = ts - pd.Timedelta(hours=8)
        setup = find_setup_for_variant(
            variant,
            bars_15m=bars_15m,
            bars_1d=bars_1d,
            session_start=session_start,
            at_ts=ts,
            bias=bias,
            vol_ma=vol_ma,
            min_volume_ratio=min_volume_ratio,
        )
        if setup is None:
            stats.rejections[RejectReason.ENTRY] += 1
            continue
        setup.atr_1h = float(
            bars_1h["close"].astype(float).diff().abs().rolling(14).mean().iloc[-1]
            if len(bars_1h) > 14
            else 0.0
        )
        spread_pct = 0.0003
        q = score_setup(
            setup,
            spread_pct=spread_pct,
            atr_pct=atr_now,
            mode_min=mode_min_score,
            min_theoretical_r=min_theoretical_r,
            max_spread_pct=max_spread_pct,
            min_volume_ratio=min_volume_ratio,
        )
        if not q.ok:
            stats.rejections[RejectReason.QUALITY] += 1
            continue

        ok, reason = can_open_trade(state, limits, mode="normal")
        if not ok:
            stats.rejections[RejectReason.RISK] += 1
            if "kill" in reason:
                stats.kill_switches += 1
            if "profit_lock" in reason:
                stats.profit_locks += 1
            continue

        idx = bars_15m.index.get_indexer([ts], method="pad")[0]
        r_net, exit_reason = _simulate_exit(
            bars_15m,
            idx,
            setup,
            tp_reward_risk=tp_reward_risk,
            scale_at_1r=scale_at_1r,
        )
        stats.trades += 1
        stats.r_net_sum += r_net
        stats.r_net_list.append(r_net)
        if r_net > 0:
            stats.wins += 1
        pnl_pct = r_net * limits.risk_normal_pct
        register_trade_result(state, limits, pnl_pct)
        if state.night_pnl_pct >= limits.profit_lock_total_pct:
            stats.profit_locks += 1

    if stats.r_net_list:
        eq = np.cumsum(stats.r_net_list)
        peak = np.maximum.accumulate(eq)
        stats.max_dd_night = float((peak - eq).max()) if len(eq) else 0.0
    return stats


def summarize_variants(all_stats: list[VariantStats]) -> tuple[str, str | None]:
    rows = []
    champion: str | None = None
    best_score = -1e9
    for s in all_stats:
        wr = (s.wins / s.trades * 100.0) if s.trades else 0.0
        avg_r = (s.r_net_sum / s.trades) if s.trades else 0.0
        rows.append(
            {
                "variant": s.variant,
                "signals": s.signals,
                "trades": s.trades,
                "winrate_pct": round(wr, 2),
                "r_net_avg": round(avg_r, 3),
                "r_net_sum": round(s.r_net_sum, 3),
                "max_dd_night": round(s.max_dd_night, 3),
                "profit_locks": s.profit_locks,
                "kill_switches": s.kill_switches,
                "reject_session": s.rejections.get(RejectReason.SESSION, 0),
                "reject_vol": s.rejections.get(RejectReason.VOLATILITY, 0),
                "reject_bias": s.rejections.get(RejectReason.BIAS, 0),
                "reject_entry": s.rejections.get(RejectReason.ENTRY, 0),
                "reject_quality": s.rejections.get(RejectReason.QUALITY, 0),
                "reject_risk": s.rejections.get(RejectReason.RISK, 0),
            }
        )
        score = avg_r * 10 - s.max_dd_night * 5 + s.profit_locks * 0.5 - s.kill_switches * 2
        if s.trades >= 5 and score > best_score and avg_r > 0:
            best_score = score
            champion = s.variant

    import csv
    import io
    from pathlib import Path

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    csv_text = buf.getvalue()
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "crypto_night_mapping_sweep.csv").write_text(csv_text, encoding="utf-8")
    summary_lines = [
        "Crypto Night Fortress — mapping sweep",
        f"CHAMPION: {champion or 'ninguna pasa umbral (>=5 trades, R neto > 0)'}",
        "",
        csv_text,
    ]
    return "\n".join(summary_lines), champion
