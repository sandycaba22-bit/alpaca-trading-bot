"""Utilidades compartidas para calibrar MIN_TP_TO_COST_RATIO (offline, no toca producción)."""

from __future__ import annotations

import pickle
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, Settings
from bot.risk.stops import StopTakeProfitPolicy
from bot.strategy.base import Signal, StrategyContext
from bot.strategy.indicators import last_atr, momentum_pct
from bot.strategy.multi_strategy import MultiStrategyOrchestrator
from bot.strategy.multi_tf_analysis import analyze_trend
from bot.strategy.signal_filters import SignalFilterLayer
from bot.storage.breakout_state import BreakoutStateStore

CACHE = PROJECT_ROOT / "data" / "bars_cache"
SYMBOLS = ("BTC/USD", "ETH/USD")
ENTRY_TFS = ("6Min", "1Hour")
RATIO_GRID = (0.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0)
OOS_START = pd.Timestamp("2025-01-01", tz="UTC")
BASE_ENTRY_MIN = 6
DEFAULT_CRYPTO_FEE_PCT = 0.25
DEFAULT_SLIPPAGE_PCT = 0.03
YEARS = 6


@dataclass(frozen=True)
class SignalCandidate:
    bar_idx: int
    tp_pct: float
    tp_to_cost_ratio: float
    tp_raw_pct: float
    tp_raw_to_cost_ratio: float
    atr: float


def projected_tp_pcts(
    entry_px: float, atr: float, policy: StopTakeProfitPolicy
) -> tuple[float, float]:
    """TP con piso (levels, producción) y TP ATR puro (sin min_tp_pct, para diagnóstico)."""
    lv = policy.levels(entry_px, 1.0, entry_px, atr)
    tp_levels = float(lv.take_profit_pct)
    if atr > 0 and entry_px > 0:
        tp_raw = min(policy.max_tp_pct, (atr * policy.atr_tp_mult) / entry_px)
    else:
        tp_raw = tp_levels
    return tp_levels, float(tp_raw)


def round_trip_cost_pct(fee_pct: float, slippage_pct: float) -> float:
    """Costo ida y vuelta: (fee + slippage) por lado × 2, en fracción (0.0056 = 0.56%)."""
    return 2.0 * (float(fee_pct) + float(slippage_pct)) / 100.0


def _tf_minutes(tf: str) -> int:
    return {"6Min": 6, "15Min": 15, "30Min": 30, "1Hour": 60, "1Day": 1440}[tf]


def _scale_period(period: int, entry_tf: str, *, base_entry_min: int = BASE_ENTRY_MIN) -> int:
    return max(2, round(int(period) * base_entry_min / _tf_minutes(entry_tf)))


def confirm_tf(entry_tf: str) -> str:
    return {"6Min": "15Min", "15Min": "30Min", "30Min": "1Hour", "1Hour": "1Day"}.get(
        entry_tf, "15Min"
    )


def scaled_settings(base: Settings, entry_tf: str) -> Settings:
    sc = lambda p: _scale_period(p, entry_tf)
    lookback = max(sc(base.lookback_bars), 40)
    return replace(
        base,
        crypto_sma_fast=sc(base.crypto_sma_fast),
        crypto_sma_slow=sc(base.crypto_sma_slow),
        atr_period=sc(base.atr_period),
        adx_period=sc(base.adx_period),
        lookback_bars=lookback,
        momentum_bars=sc(base.momentum_bars),
        confirm_momentum_bars=sc(base.confirm_momentum_bars),
        bb_period=sc(base.bb_period),
        rsi_period=sc(base.rsi_period),
        volume_confirmation_period=sc(base.volume_confirmation_period),
        breakout_lookback_periods=sc(base.breakout_lookback_periods),
        breakout_cooldown_bars=sc(base.breakout_cooldown_bars),
        squeeze_width_lookback=sc(base.squeeze_width_lookback),
        trend_micro_lookback=sc(base.trend_micro_lookback),
        trend_range_lookback=sc(base.trend_range_lookback),
    )


def crypto_policy(settings: Settings) -> StopTakeProfitPolicy:
    """Política SL/TP/trailing de producción para cripto (2 etapas, params crypto_*)."""
    return StopTakeProfitPolicy(
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        atr_stop_mult=settings.atr_stop_mult,
        atr_sl_mult=settings.crypto_atr_sl_mult,
        atr_tp_mult=settings.crypto_atr_tp_mult,
        atr_trailing_mult=settings.crypto_atr_trailing_mult,
        breakeven_activate_pct=settings.crypto_breakeven_activate_pct,
        breakeven_activate_atr_mult=settings.crypto_breakeven_activate_atr_mult,
        breakeven_buffer=settings.breakeven_buffer,
        breakeven_buffer_atr_mult=settings.breakeven_buffer_atr_mult,
        min_tp_pct=settings.crypto_min_tp_pct,
        use_breakeven_lock=True,
    )


def _cache_path(symbol: str, tf: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_{tf}.pkl"


def _candidates_cache_path(symbol: str, entry_tf: str, confirm_tf: str) -> Path:
    return CACHE / f"{symbol.replace('/', '-')}_{entry_tf}_{confirm_tf}_tp_candidates_v2.pkl"


def load_or_fetch(
    market: MarketDataService, symbol: str, tf: str, start: datetime, end: datetime
) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, tf)
    if path.exists():
        bars = pickle.loads(path.read_bytes())
        if isinstance(bars, pd.DataFrame) and not bars.empty:
            print(f"cache {symbol} {tf} bars={len(bars)} {bars.index.min()}->{bars.index.max()}")
            return bars
    print(f"fetch {symbol} {tf} {start.date()}->{end.date()} ...")
    bars = market.get_bars_range(symbol, tf, start=start, end=end)
    if not bars.empty:
        path.write_bytes(pickle.dumps(bars, protocol=pickle.HIGHEST_PROTOCOL))
        print(f"saved {symbol} {tf} bars={len(bars)}")
    else:
        print(f"empty {symbol} {tf}")
    return bars


def _confirm_ok(
    filters: SignalFilterLayer, symbol: str, entry_hist: pd.DataFrame, htf: pd.DataFrame | None
) -> bool:
    if not filters.settings.entry_confirmation_enabled:
        return True
    return filters.check_entry_confirmation(symbol, entry_hist, htf).allowed


def _htf_until(htf: pd.DataFrame, ts, htf_pos: int | None = None) -> pd.DataFrame:
    if htf is None or htf.empty:
        return htf
    if htf_pos is not None:
        return htf.iloc[:htf_pos]
    return htf.loc[htf.index < ts]


def precompute_signal_candidates(
    settings: Settings,
    symbol: str,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    entry_tf: str,
    confirm_tf: str,
    *,
    fee_pct: float,
    slippage_pct: float,
    policy: StopTakeProfitPolicy | None = None,
) -> list[SignalCandidate]:
    """Todas las señales BUY válidas sin bloqueo por posición (para filtrar por ratio después)."""
    path = _candidates_cache_path(symbol, entry_tf, confirm_tf)
    if path.exists():
        data = pickle.loads(path.read_bytes())
        if isinstance(data, list) and data and isinstance(data[0], SignalCandidate):
            print(f"cache {symbol} {entry_tf}/{confirm_tf} candidates={len(data)}")
            return data

    pol = policy or crypto_policy(settings)
    cost_pct = round_trip_cost_pct(fee_pct, slippage_pct)
    orch = MultiStrategyOrchestrator(settings)
    filters = SignalFilterLayer(settings, BreakoutStateStore(persist=False))
    lookback = max(int(settings.lookback_bars), 40)
    start_i = max(
        lookback,
        settings.crypto_sma_slow + 1,
        settings.bb_period + 25,
        settings.adx_period * 2 + 2,
    )
    candidates: list[SignalCandidate] = []
    n = len(bars)
    htf_pos = 0
    htf_len = len(htf) if htf is not None and not htf.empty else 0
    print(f"{symbol} {entry_tf} candidates on {n} bars start={start_i}")
    for i in range(start_i, n):
        if i % 10000 == 0:
            print(f"{symbol} {entry_tf} scan {i}/{n} candidates={len(candidates)}")
        hist = bars.iloc[i - lookback : i]
        if hist.empty:
            continue
        ts = bars.index[i]
        last_price = float(hist["close"].iloc[-1])
        if htf_len:
            while htf_pos < htf_len and htf.index[htf_pos] < ts:
                htf_pos += 1
            htf_hist = _htf_until(htf, ts, htf_pos)
        else:
            htf_hist = None
        trend, _ = analyze_trend(htf_hist if htf_hist is not None and len(htf_hist) > 20 else hist)
        if trend == "bear":
            continue
        ctx = StrategyContext(
            symbol=symbol,
            bars=hist,
            has_long_position=False,
            has_short_position=False,
            last_price=last_price,
            spread_pct=0.0,
            momentum_pct=momentum_pct(hist["close"], settings.momentum_bars),
            atr=last_atr(hist, settings.atr_period),
            htf_trend=trend,
        )
        signal = orch.generate_signal(ctx)
        if signal is not Signal.BUY:
            continue
        last_st = getattr(orch, "last_strategy", None)
        name = getattr(last_st, "value", "") if last_st else ""
        if name in {"", "breakout", "sma_crossover", "sma_crossover_tuned"}:
            slow = int(orch.slow_period(symbol))
            if not filters.check_sma_bias(symbol, signal, hist, slow).allowed:
                continue
            if settings.adx_filter_enabled and not filters.check_adx(symbol, hist).allowed:
                continue
        if not _confirm_ok(filters, symbol, hist, htf_hist):
            continue

        entry_px = float(bars["open"].iloc[i])
        atr_e = last_atr(hist, settings.atr_period)
        if entry_px <= 0 or not atr_e or atr_e <= 0:
            continue
        tp_levels, tp_raw = projected_tp_pcts(entry_px, float(atr_e), pol)
        ratio_levels = tp_levels / cost_pct if cost_pct > 0 else 0.0
        ratio_raw = tp_raw / cost_pct if cost_pct > 0 else 0.0
        candidates.append(
            SignalCandidate(
                bar_idx=i,
                tp_pct=tp_levels,
                tp_to_cost_ratio=ratio_levels,
                tp_raw_pct=tp_raw,
                tp_raw_to_cost_ratio=ratio_raw,
                atr=float(atr_e),
            )
        )

    print(f"{symbol} {entry_tf} candidates={len(candidates)}")
    path.write_bytes(pickle.dumps(candidates, protocol=pickle.HIGHEST_PROTOCOL))
    return candidates


def _size_qty(settings: Settings, cash: float, price: float, atr: float | None) -> float:
    if price <= 0:
        return 0.0
    cap = min(cash * settings.position_size_pct, settings.max_notional_per_order, cash)
    if settings.use_fixed_risk_sizing and atr and atr > 0:
        sl_dist = float(atr) * float(settings.crypto_atr_sl_mult)
        if sl_dist > 0:
            risk = cash * settings.risk_percent_per_trade
            qty = risk / sl_dist
            return max(0.0, min(qty, cap / price))
    return cap / price


def _simulate_exit(
    bars: pd.DataFrame,
    entry_idx: int,
    entry: float,
    atr: float,
    policy: StopTakeProfitPolicy,
    qty: float,
) -> tuple[int, float]:
    levels = policy.levels(entry, qty, entry, atr)
    sl = levels.stop_price
    tp = levels.take_profit_price
    peak = entry
    n = len(bars)
    for j in range(entry_idx + 1, n):
        h = float(bars["high"].iloc[j])
        l = float(bars["low"].iloc[j])
        c = float(bars["close"].iloc[j])
        peak = max(peak, h, c)
        new_sl, _src = policy.trailing_candidate(entry, qty, peak, sl, atr)
        if new_sl is not None:
            sl = new_sl
        if sl > 0 and l <= sl:
            return j, sl
        if tp > 0 and h >= tp:
            return j, tp
    last_j = n - 1
    return last_j, float(bars["close"].iloc[last_j])


def _candidate_ratio(cand: SignalCandidate, filter_source: str) -> float:
    if filter_source == "atr_raw":
        return cand.tp_raw_to_cost_ratio
    return cand.tp_to_cost_ratio


def simulate_with_ratio_filter(
    settings: Settings,
    bars: pd.DataFrame,
    candidates: list[SignalCandidate],
    policy: StopTakeProfitPolicy,
    *,
    min_tp_to_cost_ratio: float,
    fee_pct: float,
    slippage_pct: float,
    period_start: pd.Timestamp | None = None,
    filter_source: str = "levels",
) -> dict[str, float]:
    """Simula trades con bloqueo de posición + gate tp/cost. period_start limita ventana (OOS)."""
    cash0 = float(settings.backtest_cash)
    friction = (fee_pct + slippage_pct) / 100.0
    period_start_i = 0
    if period_start is not None:
        period_start_i = int(bars.index.searchsorted(period_start, side="left"))

    scoped = [c for c in candidates if c.bar_idx >= period_start_i]
    signals_total = len(scoped)
    signals_filtered = 0
    trades = 0
    wins = 0
    pnl_gross_sum = 0.0
    pnl_net_sum = 0.0
    gross_wins = 0.0
    gross_losses = 0.0
    tp_pcts: list[float] = []
    tp_ratios: list[float] = []
    tp_raw_pcts: list[float] = []
    in_pos_until = period_start_i - 1

    for cand in sorted(scoped, key=lambda c: c.bar_idx):
        if cand.bar_idx <= in_pos_until:
            continue
        if min_tp_to_cost_ratio > 0 and _candidate_ratio(cand, filter_source) < min_tp_to_cost_ratio:
            signals_filtered += 1
            continue

        entry = float(bars["open"].iloc[cand.bar_idx])
        if entry <= 0:
            continue
        qty = _size_qty(settings, cash0, entry, cand.atr)
        if qty <= 0:
            continue

        exit_j, exit_px = _simulate_exit(bars, cand.bar_idx, entry, cand.atr, policy, qty)

        notional_entry = entry * qty
        notional_exit = exit_px * qty
        pnl_gross = (exit_px - entry) * qty
        cost = (notional_entry + notional_exit) * friction
        pnl_net = pnl_gross - cost

        pnl_gross_sum += pnl_gross
        pnl_net_sum += pnl_net
        trades += 1
        tp_pcts.append(cand.tp_pct * 100.0)
        tp_ratios.append(_candidate_ratio(cand, filter_source))
        tp_raw_pcts.append(cand.tp_raw_pct * 100.0)
        if pnl_net > 0:
            wins += 1
            gross_wins += pnl_net
        elif pnl_net < 0:
            gross_losses += abs(pnl_net)
        in_pos_until = exit_j

    win_rate = (wins / trades * 100.0) if trades else 0.0
    ret_gross = (pnl_gross_sum / cash0 * 100.0) if cash0 else 0.0
    ret_net = (pnl_net_sum / cash0 * 100.0) if cash0 else 0.0
    if gross_losses > 0:
        profit_factor = gross_wins / gross_losses
    elif gross_wins > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    return {
        "signals_total": float(signals_total),
        "signals_filtered": float(signals_filtered),
        "trades": float(trades),
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "total_return_gross_pct": ret_gross,
        "total_return_net_pct": ret_net,
        "avg_tp_pct": (sum(tp_pcts) / len(tp_pcts)) if tp_pcts else 0.0,
        "avg_tp_raw_pct": (sum(tp_raw_pcts) / len(tp_raw_pcts)) if tp_raw_pcts else 0.0,
        "avg_tp_to_cost_ratio": (sum(tp_ratios) / len(tp_ratios)) if tp_ratios else 0.0,
    }


def sweep_symbol_combo(
    settings: Settings,
    market: MarketDataService,
    symbol: str,
    entry_tf: str,
    start: datetime,
    end: datetime,
    *,
    fee_pct: float,
    slippage_pct: float,
    scope: str,
    ratio_grid: tuple[float, ...] = RATIO_GRID,
    filter_source: str = "levels",
) -> list[str]:
    confirm = confirm_tf(entry_tf)
    cfg = scaled_settings(settings, entry_tf)
    policy = crypto_policy(cfg)
    bars = load_or_fetch(market, symbol, entry_tf, start, end)
    htf = load_or_fetch(market, symbol, confirm, start, end)
    if bars.empty or len(bars) < 200:
        return []

    period_start = OOS_START if scope == "oos" else None
    if scope == "oos":
        mask = bars.index >= OOS_START
        if not mask.any():
            print(f"{symbol} {entry_tf} no bars >= {OOS_START.date()}")
            return []
        rng = f"{OOS_START.date()}->{pd.Timestamp(bars.index[mask][-1]).date()}"
        bar_count = int(mask.sum())
    else:
        rng = f"{pd.Timestamp(bars.index.min()).date()}->{pd.Timestamp(bars.index.max()).date()}"
        bar_count = len(bars)

    candidates = precompute_signal_candidates(
        cfg,
        symbol,
        bars,
        htf,
        entry_tf,
        confirm,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        policy=policy,
    )
    rows: list[str] = []
    cost_pct = round_trip_cost_pct(fee_pct, slippage_pct)
    print(
        f"{symbol} {entry_tf}/{confirm} scope={scope} bars={bar_count} "
        f"candidates={len(candidates)} round_trip_cost={cost_pct*100:.2f}% filter={filter_source}"
    )

    for ratio in ratio_grid:
        res = simulate_with_ratio_filter(
            cfg,
            bars,
            candidates,
            policy,
            min_tp_to_cost_ratio=ratio,
            fee_pct=fee_pct,
            slippage_pct=slippage_pct,
            period_start=period_start,
            filter_source=filter_source,
        )
        pf = res["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.3f}"
        ratio_label = "baseline" if ratio <= 0 else f"{ratio:.1f}"
        row = (
            f"{symbol},{ratio_label},{entry_tf},{confirm},{scope},{filter_source},{bar_count},{rng},"
            f"{int(res['signals_total'])},{int(res['signals_filtered'])},{int(res['trades'])},"
            f"{res['win_rate_pct']:.1f},{pf_s},"
            f"{res['total_return_gross_pct']:.2f},{res['total_return_net_pct']:.2f},"
            f"{res['avg_tp_pct']:.3f},{res['avg_tp_raw_pct']:.3f},{res['avg_tp_to_cost_ratio']:.2f},"
            f"{fee_pct},{slippage_pct}"
        )
        rows.append(row)
        filt_pct = (
            res["signals_filtered"] / res["signals_total"] * 100.0 if res["signals_total"] else 0.0
        )
        print(
            f"  ratio={ratio_label:8} tr={int(res['trades']):4} "
            f"filt={int(res['signals_filtered']):4} ({filt_pct:.0f}%) "
            f"wr={res['win_rate_pct']:.1f}% pf={pf_s} "
            f"net={res['total_return_net_pct']:+.2f}%"
        )
    return rows


CSV_HEADER = (
    "symbol,min_tp_to_cost_ratio,entry_tf,confirm_tf,scope,filter_source,bars,range,signals_total,"
    "signals_filtered,trades,win_rate_pct,profit_factor,total_return_gross_pct,"
    "total_return_net_pct,avg_tp_pct,avg_tp_raw_pct,avg_tp_to_cost_ratio,crypto_fee_pct,slippage_pct"
)


def default_window() -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(YEARS * 365.25))
    return start, end
