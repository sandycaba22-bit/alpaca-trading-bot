"""Comparación 6 años: estructura actual (6m+9m) vs nueva (5m+15m confirmación SMA)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.backtest.engine import SimulatedTrade
from bot.backtest.metrics import compute_metrics
from bot.config import Settings
from bot.market.assets import all_symbols, is_crypto_symbol
from bot.risk.stops import ExitReason, StopTakeProfitPolicy
from bot.strategy.base import Signal
from bot.strategy.indicators import adx, atr, classify_regime, sma
from bot.strategy.signal_filters import SignalFilterLayer
from bot.storage.breakout_state import BreakoutStateStore

logger = logging.getLogger(__name__)

BASELINE_SMA_6M: dict[str, tuple[int, int]] = {
    "AAPL": (15, 30),
    "MSFT": (10, 30),
    "BTC/USD": (9, 21),
    "ETH/USD": (9, 21),
}


def _scale_period(period: int, *, from_min: int = 6, to_min: int = 5) -> int:
    return max(2, round(period * from_min / to_min))


def _sma_5m(symbol: str) -> tuple[int, int]:
    fast, slow = BASELINE_SMA_6M.get(symbol.upper(), (15, 30))
    return _scale_period(fast), _scale_period(slow)


def _periods_5m(settings: Settings) -> tuple[int, int]:
    return _scale_period(settings.atr_period), _scale_period(settings.adx_period)


@dataclass(frozen=True)
class MtfStructureSpec:
    label: str
    entry_tf: str
    trend_tf: str | None
    confirm_tf: str | None
    sma_by_symbol: dict[str, tuple[int, int]]
    atr_period: int
    adx_period: int
    use_9m_trend: bool
    use_15m_confirm: bool


@dataclass
class MtfCompareRow:
    structure: str
    symbol: str
    trades: int
    signals_raw: int
    signals_discarded: int
    win_rate_pct: float
    max_drawdown_pct: float
    total_return_pct: float
    sharpe: float


def baseline_spec(settings: Settings) -> MtfStructureSpec:
    by_symbol = dict(BASELINE_SMA_6M)
    for sym in settings.crypto_symbols:
        key = sym.upper()
        if key not in by_symbol:
            by_symbol[key] = (settings.crypto_sma_fast, settings.crypto_sma_slow)
    for sym in settings.stock_symbols:
        key = sym.upper()
        if key not in by_symbol:
            by_symbol[key] = (settings.sma_fast, settings.sma_slow)
    return MtfStructureSpec(
        label="baseline_6m_9m",
        entry_tf="6Min",
        trend_tf="9Min",
        confirm_tf=None,
        sma_by_symbol=by_symbol,
        atr_period=settings.atr_period,
        adx_period=settings.adx_period,
        use_9m_trend=True,
        use_15m_confirm=False,
    )


def proposed_spec(settings: Settings) -> MtfStructureSpec:
    by_symbol = {sym.upper(): _sma_5m(sym) for sym in all_symbols(settings)}
    atr_p, adx_p = _periods_5m(settings)
    return MtfStructureSpec(
        label="proposed_5m_15m",
        entry_tf="5Min",
        trend_tf=None,
        confirm_tf="15Min",
        sma_by_symbol=by_symbol,
        atr_period=atr_p,
        adx_period=adx_p,
        use_9m_trend=False,
        use_15m_confirm=True,
    )


def _htf_trend_series(htf_bars: pd.DataFrame) -> pd.Series:
    closes = htf_bars["close"].astype(float)
    fast_ma = sma(closes, 5)
    slow_ma = sma(closes, 13)
    trend = pd.Series("sideways", index=htf_bars.index, dtype=object)
    trend = trend.mask(fast_ma > slow_ma, "bull")
    trend = trend.mask(fast_ma < slow_ma, "bear")
    return trend


def _align_htf_trend(entry_index: pd.DatetimeIndex, htf_bars: pd.DataFrame | None) -> np.ndarray:
    if htf_bars is None or htf_bars.empty:
        return np.array(["sideways"] * len(entry_index), dtype=object)
    htf = pd.DataFrame({"trend": _htf_trend_series(htf_bars).to_numpy()}, index=htf_bars.index)
    left = pd.DataFrame({"ts": pd.DatetimeIndex(entry_index)})
    right = htf.reset_index(names="ts")
    merged = pd.merge_asof(
        left.sort_values("ts"),
        right.sort_values("ts"),
        on="ts",
        direction="backward",
    )
    return merged["trend"].fillna("sideways").to_numpy()


def _cross_signals(closes: pd.Series, fast: int, slow: int) -> tuple[np.ndarray, np.ndarray]:
    fast_ma = sma(closes, fast)
    slow_ma = sma(closes, slow)
    prev_f = fast_ma.shift(1)
    prev_s = slow_ma.shift(1)
    golden = ((prev_f <= prev_s) & (fast_ma > slow_ma)).fillna(False).to_numpy()
    death = ((prev_f >= prev_s) & (fast_ma < slow_ma)).fillna(False).to_numpy()
    return golden, death


def _trend_allows(signal: str, trend: str, has_long: bool) -> bool:
    if signal == "buy":
        return trend != "bear"
    if signal == "sell" and not has_long:
        return trend != "bull"
    return True


class MtfStructureSimulator:
    """Simula SMA crossover con filtros multi-timeframe (sin look-ahead, indicadores precalculados)."""

    def __init__(self, settings: Settings, spec: MtfStructureSpec) -> None:
        self.settings = settings
        self.spec = spec
        self.filters = SignalFilterLayer(settings, BreakoutStateStore(persist=False))
        self.stops = StopTakeProfitPolicy(
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            atr_stop_mult=settings.atr_stop_mult,
            atr_sl_mult=settings.atr_sl_mult,
            atr_tp_mult=settings.atr_tp_mult,
            atr_trailing_mult=settings.atr_trailing_mult,
        )

    def run(
        self,
        symbol: str,
        entry_bars: pd.DataFrame,
        trend_bars: pd.DataFrame | None,
        confirm_bars: pd.DataFrame | None,
    ) -> tuple[list[SimulatedTrade], pd.Series, int, int]:
        spec = self.spec
        fast, slow = spec.sma_by_symbol.get(symbol.upper(), (self.settings.sma_fast, self.settings.sma_slow))
        closes = entry_bars["close"].astype(float)
        golden, death = _cross_signals(closes, fast, slow)
        slow_ma = sma(closes, slow).to_numpy()
        atr_series = atr(entry_bars, spec.atr_period).to_numpy()
        adx_series = adx(entry_bars, spec.adx_period).to_numpy()
        adx_threshold = self.filters.adx_threshold_for(symbol)

        signal_ts = entry_bars.index
        trend_9 = _align_htf_trend(signal_ts, trend_bars) if spec.use_9m_trend else None
        trend_15 = _align_htf_trend(signal_ts, confirm_bars) if spec.use_15m_confirm else None

        cash = self.settings.backtest_cash
        trades: list[SimulatedTrade] = []
        position_qty = 0.0
        entry_price = 0.0
        entry_time: pd.Timestamp | None = None
        entry_regime = "sideways"
        pending_buy = False
        cooldown_left = 0
        signals_raw = 0
        signals_discarded = 0
        equity = np.zeros(len(entry_bars), dtype=float)

        start_i = max(slow + 2, spec.atr_period + 2, spec.adx_period * 2 + 2)

        for i in range(len(entry_bars)):
            if i < start_i:
                equity[i] = cash
                continue

            sig_idx = i - 1
            row = entry_bars.iloc[i]
            ts = entry_bars.index[i]
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            atr_value = float(atr_series[sig_idx]) if np.isfinite(atr_series[sig_idx]) else None

            if pending_buy and position_qty == 0:
                qty = self._size_qty(symbol, cash, o, atr_value)
                if qty > 0:
                    position_qty = float(qty)
                    entry_price = o
                    entry_time = ts
                    cash -= qty * o
                    entry_regime = classify_regime(closes.iloc[:i])
                pending_buy = False

            if position_qty > 0:
                reason, exit_px = self.stops.evaluate_bar(entry_price, position_qty, h, l, c, atr_value)
                sell_sig = self._sell_allowed(sig_idx, death, closes.to_numpy(), slow_ma, adx_series, adx_threshold)
                if reason is ExitReason.NONE and sell_sig:
                    trades.append(
                        self._close(symbol, entry_time or ts, ts, entry_price, c, position_qty, "signal", entry_regime, cash)
                    )
                    cash += position_qty * c
                    position_qty = 0.0
                    entry_price = 0.0
                elif reason is not ExitReason.NONE:
                    closed = self._close(
                        symbol, entry_time or ts, ts, entry_price, exit_px, position_qty, reason.value, entry_regime, cash
                    )
                    trades.append(closed)
                    cash += position_qty * exit_px
                    position_qty = 0.0
                    entry_price = 0.0
                    if closed.pnl_abs < 0 and reason is ExitReason.STOP_LOSS:
                        cooldown_left = int(self.settings.breakout_cooldown_bars)

            if cooldown_left > 0:
                cooldown_left -= 1

            if position_qty == 0 and not pending_buy and cooldown_left <= 0:
                if self._buy_raw(sig_idx, golden, closes.to_numpy(), slow_ma, adx_series, adx_threshold):
                    signals_raw += 1
                    trend = "sideways"
                    if trend_9 is not None:
                        trend = str(trend_9[sig_idx])
                    if not _trend_allows("buy", trend, has_long=False):
                        signals_discarded += 1
                    elif trend_15 is not None and str(trend_15[sig_idx]) != "bull":
                        signals_discarded += 1
                    elif not self.filters.check_cooldown(symbol, entry_bars.iloc[:i]).allowed:
                        signals_discarded += 1
                    else:
                        pending_buy = True

            equity[i] = cash + position_qty * c

        if position_qty > 0:
            last_ts = entry_bars.index[-1]
            last_px = float(closes.iloc[-1])
            trades.append(
                self._close(symbol, entry_time or last_ts, last_ts, entry_price, last_px, position_qty, "eod", entry_regime, cash)
            )
            cash += position_qty * last_px
            equity[-1] = cash

        equity_series = pd.Series(equity, index=entry_bars.index, name="equity")
        return trades, equity_series, signals_raw, signals_discarded

    def _buy_raw(
        self,
        idx: int,
        golden: np.ndarray,
        closes: np.ndarray,
        slow_ma: np.ndarray,
        adx_series: np.ndarray,
        adx_threshold: float,
    ) -> bool:
        if idx < 0 or not golden[idx]:
            return False
        if closes[idx] <= slow_ma[idx]:
            return False
        if self.settings.adx_filter_enabled:
            adx_val = adx_series[idx]
            if np.isfinite(adx_val) and adx_val <= adx_threshold:
                return False
        return True

    def _sell_allowed(
        self,
        idx: int,
        death: np.ndarray,
        closes: np.ndarray,
        slow_ma: np.ndarray,
        adx_series: np.ndarray,
        adx_threshold: float,
    ) -> bool:
        if idx < 0 or not death[idx]:
            return False
        if closes[idx] >= slow_ma[idx]:
            return False
        if self.settings.adx_filter_enabled:
            adx_val = adx_series[idx]
            if np.isfinite(adx_val) and adx_val <= adx_threshold:
                return False
        return True

    def _size_qty(self, symbol: str, cash: float, price: float, atr_value: float | None) -> float:
        import math

        if price <= 0:
            return 0.0
        cap = min(cash * self.settings.position_size_pct, self.settings.max_notional_per_order, cash)
        if self.settings.use_fixed_risk_sizing:
            sl_dist = (
                float(atr_value) * float(self.settings.atr_sl_mult)
                if atr_value and atr_value > 0
                else price * self.settings.stop_loss_pct
            )
            if sl_dist <= 0:
                return 0.0
            qty = min((cash * self.settings.risk_percent_per_trade) / sl_dist, cap / price)
        else:
            qty = cap / price
        if is_crypto_symbol(symbol):
            qty = round(float(qty), 6)
            return qty if qty >= 0.0001 else 0.0
        qty = math.floor(float(qty))
        return float(qty) if qty >= 1 else 0.0

    def _close(
        self,
        symbol: str,
        entry_time: pd.Timestamp,
        exit_time: pd.Timestamp,
        entry_price: float,
        exit_price: float,
        qty: float,
        reason: str,
        regime: str,
        cash: float,
    ) -> SimulatedTrade:
        from bot.reporting.pnl import PnLEvent, compute_pnl

        snap = compute_pnl(symbol, qty, entry_price, exit_price, PnLEvent.CLOSED)
        return SimulatedTrade(
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            pnl_abs=snap.pnl_abs,
            pnl_pct=snap.pnl_pct,
            reason=reason,
            regime=regime,
            strategy="sma_crossover_tuned",
        )


def _fetch_bars(
    market_data: MarketDataService,
    symbol: str,
    timeframe: str,
    years: int,
) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    logger.info("Descargando %s %s | %s -> %s", symbol, timeframe, start.date(), end.date())
    print(f"Descargando {symbol} {timeframe}...", flush=True)
    bars = market_data.get_bars_range(symbol, timeframe, start=start, end=end)
    print(f"  {symbol} {timeframe}: {len(bars)} barras", flush=True)
    return bars


def run_mtf_compare(settings: Settings, market_data: MarketDataService) -> list[MtfCompareRow]:
    years = settings.backtest_years
    specs = (baseline_spec(settings), proposed_spec(settings))
    rows: list[MtfCompareRow] = []
    symbols = all_symbols(settings)
    bar_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def bars(symbol: str, tf: str) -> pd.DataFrame:
        key = (symbol, tf)
        if key not in bar_cache:
            bar_cache[key] = _fetch_bars(market_data, symbol, tf, years)
        return bar_cache[key]

    for spec in specs:
        logger.info("=== Estructura %s | entrada=%s ===", spec.label, spec.entry_tf)
        for symbol in symbols:
            entry = bars(symbol, spec.entry_tf)
            slow_need = spec.sma_by_symbol.get(symbol.upper(), (20, 50))[1]
            if entry.empty or len(entry) < slow_need + 5:
                logger.warning("%s | barras insuficientes en %s (%s)", symbol, spec.entry_tf, len(entry))
                continue
            trend = bars(symbol, spec.trend_tf) if spec.trend_tf else None
            confirm = bars(symbol, spec.confirm_tf) if spec.confirm_tf else None
            print(f"Simulando {spec.label} / {symbol} ({len(entry)} barras)...", flush=True)
            sim = MtfStructureSimulator(settings, spec)
            trades, equity, raw, discarded = sim.run(symbol, entry, trend, confirm)
            print(
                f"  -> trades={len(trades)} win={compute_metrics(equity, trades, settings.backtest_cash).win_rate_pct:.1f}%",
                flush=True,
            )
            metrics = compute_metrics(equity, trades, settings.backtest_cash)
            rows.append(
                MtfCompareRow(
                    structure=spec.label,
                    symbol=symbol,
                    trades=metrics.trades,
                    signals_raw=raw,
                    signals_discarded=discarded,
                    win_rate_pct=metrics.win_rate_pct,
                    max_drawdown_pct=metrics.max_drawdown_pct,
                    total_return_pct=metrics.total_return_pct,
                    sharpe=metrics.sharpe,
                )
            )
            logger.info(
                "%s | %s | trades=%s win=%.1f%% maxDD=%.2f%% descartadas=%s/%s",
                spec.label,
                symbol,
                metrics.trades,
                metrics.win_rate_pct,
                metrics.max_drawdown_pct,
                discarded,
                raw,
            )
    return rows


def aggregate_rows(rows: list[MtfCompareRow]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for structure in ("baseline_6m_9m", "proposed_5m_15m"):
        subset = [r for r in rows if r.structure == structure]
        if not subset:
            continue
        total_trades = sum(r.trades for r in subset)
        total_raw = sum(r.signals_raw for r in subset)
        total_disc = sum(r.signals_discarded for r in subset)
        wins_weighted = sum(r.win_rate_pct * r.trades for r in subset if r.trades > 0)
        win_rate = wins_weighted / total_trades if total_trades else 0.0
        worst_dd = min(r.max_drawdown_pct for r in subset) if subset else 0.0
        out[structure] = {
            "trades": float(total_trades),
            "signals_raw": float(total_raw),
            "signals_discarded": float(total_disc),
            "win_rate_pct": win_rate,
            "max_drawdown_pct": worst_dd,
        }
    return out


def format_mtf_report(rows: list[MtfCompareRow]) -> str:
    lines = [
        "=== COMPARATIVA MULTI-TIMEFRAME (6 años) ===",
        "Baseline: entrada 6Min + filtro tendencia 9Min (sin confirmación 15Min)",
        "Propuesta: entrada 5Min + confirmación SMA tendencia 15Min",
        "",
        f"{'Estructura':<20} {'Símbolo':<10} {'Trades':>7} {'Win%':>7} {'MaxDD%':>8} {'Descart.':>10}",
        "-" * 70,
    ]
    for r in rows:
        disc = f"{r.signals_discarded}/{r.signals_raw}" if r.signals_raw else "—"
        lines.append(
            f"{r.structure:<20} {r.symbol:<10} {r.trades:>7} {r.win_rate_pct:>6.1f}% "
            f"{r.max_drawdown_pct:>7.2f}% {disc:>10}"
        )
    agg = aggregate_rows(rows)
    lines.extend(["", "=== TOTALES (4 símbolos) ==="])
    for key, label in (
        ("baseline_6m_9m", "Baseline 6m+9m"),
        ("proposed_5m_15m", "Propuesta 5m+15m"),
    ):
        if key not in agg:
            continue
        a = agg[key]
        lines.append(
            f"{label}: trades={int(a['trades'])} | win rate={a['win_rate_pct']:.1f}% | "
            f"max drawdown={a['max_drawdown_pct']:.2f}% | "
            f"señales descartadas={int(a['signals_discarded'])}/{int(a['signals_raw'])}"
        )
    lines.extend(
        [
            "",
            "Períodos 5m recalibrados (6m×6/5):",
            "  AAPL 18/36 | MSFT 12/36 | BTC/ETH 11/25 | ATR/ADX period 17",
        ]
    )
    return "\n".join(lines)
