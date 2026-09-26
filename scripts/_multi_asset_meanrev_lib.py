"""Research: mean-reversion + volumen + horas muertas (multi-activo, sin producción)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

import pandas as pd

from bot.config import Settings
from bot.strategy.crypto_asymmetric import resample_4h_from_1h
from bot.strategy.indicators import bollinger, last_adx, last_atr
from bot.strategy.base import Signal
from bot.strategy.mean_reversion import mean_reversion_criteria
from bot.strategy.multi_tf_analysis import analyze_trend

from _crypto_asymmetric_backtest_lib import (
    AsymmetricTrade,
    _simulate_bar_exits,
    _size_qty,
    load_1h,
    summarize_trades,
)
from _crypto_regime_entry_backtest_lib import CRYPTO_IS_START
from _stocks_asymmetric_backtest_lib import (
    CONFIRM_TF,
    ENTRY_TF,
    OOS_START,
    load_bars,
    policy_asymmetric,
    simulate_trades,
    summarize_trades as summarize_stock_trades,
)

NY = ZoneInfo("America/New_York")
STOCK_IS_START = pd.Timestamp("2020-01-01", tz="UTC")


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    STOCK = "stock"


@dataclass(frozen=True)
class MeanRevResearchConfig:
    symbol: str
    asset_class: AssetClass
    entry_tf: str
    confirm_tf: str
    fee_pct: float
    slippage_pct: float
    is_start: pd.Timestamp
    # Horas muertas
    stock_skip_open_minutes: int = 30
    stock_skip_lunch: bool = True
    crypto_dead_utc_hours: tuple[int, ...] = (22, 23, 0, 1, 2, 3, 4, 5)
    # Volumen institucional: exige vol vela >= media * ratio (1.0 = no operar bajo la media)
    volume_period: int = 20
    min_volume_ratio: float = 1.0
    # Mean-reversion / bandas
    max_confirm_adx: float = 26.0
    require_htf_not_bear: bool = True


def default_crypto_configs(symbols: list[str]) -> list[MeanRevResearchConfig]:
    return [
        MeanRevResearchConfig(
            symbol=s,
            asset_class=AssetClass.CRYPTO,
            entry_tf="1Hour",
            confirm_tf="4H",
            fee_pct=0.25,
            slippage_pct=0.03,
            is_start=CRYPTO_IS_START,
        )
        for s in symbols
    ]


def default_stock_configs(symbols: list[str]) -> list[MeanRevResearchConfig]:
    return [
        MeanRevResearchConfig(
            symbol=s.upper(),
            asset_class=AssetClass.STOCK,
            entry_tf=ENTRY_TF,
            confirm_tf=CONFIRM_TF,
            fee_pct=0.0,
            slippage_pct=0.03,
            is_start=STOCK_IS_START,
            stock_skip_open_minutes=30,
            min_volume_ratio=1.0,
        )
        for s in symbols
    ]


def _in_crypto_dead_hour(ts: pd.Timestamp, dead_hours: tuple[int, ...]) -> bool:
    if not dead_hours:
        return False
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    h = int(ts.tz_convert("UTC").hour)
    return h in dead_hours


def _in_stock_dead_hour(
    ts: pd.Timestamp,
    *,
    skip_open_min: int,
    skip_lunch: bool,
) -> bool:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    et = ts.tz_convert(NY)
    if et.weekday() >= 5:
        return True
    open_t = et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = et.replace(hour=16, minute=0, second=0, microsecond=0)
    if et < open_t or et >= close_t:
        return True
    if skip_open_min > 0 and et < open_t + pd.Timedelta(minutes=skip_open_min):
        return True
    if skip_lunch:
        lunch0 = et.replace(hour=12, minute=0, second=0, microsecond=0)
        lunch1 = et.replace(hour=13, minute=30, second=0, microsecond=0)
        if lunch0 <= et < lunch1:
            return True
    return False


def _volume_ok(bars: pd.DataFrame, bar_i: int, period: int, min_ratio: float) -> tuple[bool, str]:
    if min_ratio <= 0 or "volume" not in bars.columns:
        return True, "vol off"
    if bar_i <= 0:
        return False, "vol idx"
    window = bars["volume"].iloc[max(0, bar_i - period) : bar_i]
    if window.empty:
        return False, "vol ventana"
    avg = float(window.astype(float).mean())
    if avg <= 0:
        return False, "vol avg 0"
    vol = float(bars["volume"].iloc[bar_i - 1])
    ratio = vol / avg
    if vol < avg * min_ratio:
        return False, f"vol bajo {ratio:.2f}x < {min_ratio:.2f}x"
    return True, f"vol ok {ratio:.2f}x"


def _bb_width_ok(bars: pd.DataFrame, settings: Settings, min_width_atr: float = 0.8) -> bool:
    """Banda con volatilidad mínima (ancho BB vs ATR)."""
    period = int(settings.bb_period)
    if len(bars) < period + 5:
        return False
    lower, mid, upper = bollinger(bars["close"], period, settings.bb_std)
    if pd.isna(lower.iloc[-1]) or pd.isna(upper.iloc[-1]):
        return False
    width = float(upper.iloc[-1] - lower.iloc[-1])
    atr_v = last_atr(bars, settings.atr_period)
    if not atr_v or atr_v <= 0:
        return False
    return width >= min_width_atr * atr_v


def precompute_meanrev_entries_crypto(
    settings: Settings,
    bars_1h: pd.DataFrame,
    cfg: MeanRevResearchConfig,
) -> list[int]:
    bars_4h = resample_4h_from_1h(bars_1h)
    params = settings
    lookback = max(int(settings.lookback_bars), 80, settings.bb_period + settings.rsi_period + 5)
    entries: list[int] = []
    for i in range(lookback, len(bars_1h)):
        ts = bars_1h.index[i - 1]
        if _in_crypto_dead_hour(ts, cfg.crypto_dead_utc_hours):
            continue
        ok_v, _ = _volume_ok(bars_1h, i, cfg.volume_period, cfg.min_volume_ratio)
        if not ok_v:
            continue
        hist = bars_1h.iloc[i - lookback : i]
        if cfg.require_htf_not_bear:
            htf = bars_4h.loc[bars_4h.index <= ts].tail(120)
            trend, _ = analyze_trend(htf if len(htf) > 20 else hist)
            if trend == "bear":
                continue
        htf = bars_4h.loc[bars_4h.index <= ts]
        if len(htf) >= settings.adx_period + 5:
            adx_v = last_adx(htf, settings.adx_period)
            if adx_v is not None and adx_v > cfg.max_confirm_adx:
                continue
        if not _bb_width_ok(hist, settings):
            continue
        sig, _ = mean_reversion_criteria(
            hist, settings=settings, has_long=False, symbol=cfg.symbol
        )
        if sig is not Signal.BUY:
            continue
        entries.append(i)
    return entries


def precompute_meanrev_entries_stock(
    settings: Settings,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    cfg: MeanRevResearchConfig,
) -> list[int]:
    lookback = max(int(settings.lookback_bars), 80, settings.bb_period + settings.rsi_period + 5)
    entries: list[int] = []
    htf_pos = 0
    htf_len = len(htf) if htf is not None and not htf.empty else 0
    for i in range(lookback, len(bars)):
        ts = bars.index[i - 1]
        if _in_stock_dead_hour(
            ts,
            skip_open_min=cfg.stock_skip_open_minutes,
            skip_lunch=cfg.stock_skip_lunch,
        ):
            continue
        ok_v, _ = _volume_ok(bars, i, cfg.volume_period, cfg.min_volume_ratio)
        if not ok_v:
            continue
        hist = bars.iloc[i - lookback : i]
        if cfg.require_htf_not_bear and htf_len:
            while htf_pos < htf_len and htf.index[htf_pos] < ts:
                htf_pos += 1
            htf_hist = htf.iloc[:htf_pos]
            trend, _ = analyze_trend(htf_hist if len(htf_hist) > 20 else hist)
            if trend == "bear":
                continue
            if len(htf_hist) >= settings.adx_period + 5:
                adx_v = last_adx(htf_hist, settings.adx_period)
                if adx_v is not None and adx_v > cfg.max_confirm_adx:
                    continue
        if not _bb_width_ok(hist, settings):
            continue
        sig, _ = mean_reversion_criteria(
            hist, settings=settings, has_long=False, symbol=cfg.symbol
        )
        if sig is not Signal.BUY:
            continue
        entries.append(i)
    return entries


def run_crypto_meanrev_period(
    settings: Settings,
    bars: pd.DataFrame,
    cfg: MeanRevResearchConfig,
    *,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
) -> list[AsymmetricTrade]:
    from bot.strategy.crypto_asymmetric import params_from_settings

    params = params_from_settings(settings)
    entries = precompute_meanrev_entries_crypto(settings, bars, cfg)
    friction = (cfg.fee_pct + cfg.slippage_pct) / 100.0
    cash0 = float(settings.backtest_cash)
    period_start_i = int(bars.index.searchsorted(period_start, side="left"))
    period_end_i = int(bars.index.searchsorted(period_end, side="right")) - 1
    period_end_i = max(period_start_i, min(period_end_i, len(bars) - 1))
    trades: list[AsymmetricTrade] = []
    in_pos_until = period_start_i - 1
    lookback = max(int(settings.lookback_bars), 80)

    for ei in entries:
        if ei <= in_pos_until or ei < period_start_i or bars.index[ei] > period_end:
            continue
        hist = bars.iloc[max(0, ei - lookback) : ei]
        atr_val = last_atr(hist, settings.atr_period) or 0.0
        entry = float(bars["open"].iloc[ei])
        qty = _size_qty(settings, cash0, entry, params.sl_atr_mult, float(atr_val))
        if qty <= 0:
            continue
        exit_px, exit_j, reason = _simulate_bar_exits(
            bars, ei, entry, qty, float(atr_val), params, period_end_i
        )
        pnl_net = (exit_px - entry) * qty - (entry + exit_px) * qty * friction
        trades.append(
            AsymmetricTrade(
                symbol=cfg.symbol,
                entry_idx=ei,
                exit_idx=exit_j,
                entry_price=entry,
                exit_price=exit_px,
                qty=qty,
                pnl_gross=(exit_px - entry) * qty,
                pnl_net=pnl_net,
                exit_reason=reason,
            )
        )
        in_pos_until = exit_j
    return trades


def metrics_crypto_period(
    settings: Settings,
    bars: pd.DataFrame,
    cfg: MeanRevResearchConfig,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, float]:
    tr = run_crypto_meanrev_period(settings, bars, cfg, period_start=start, period_end=end)
    m = summarize_trades(tr, float(settings.backtest_cash))
    m["symbol"] = cfg.symbol
    m["entries_signal"] = float(len(precompute_meanrev_entries_crypto(settings, bars, cfg)))
    return m


def metrics_stock_is_oos(
    settings: Settings,
    bars: pd.DataFrame,
    htf: pd.DataFrame,
    cfg: MeanRevResearchConfig,
    *,
    end: pd.Timestamp,
) -> tuple[dict[str, float], dict[str, float]]:
    asym = policy_asymmetric(settings)
    entries = precompute_meanrev_entries_stock(settings, bars, htf, cfg)
    cash0 = float(settings.backtest_cash)
    is_end = OOS_START - pd.Timedelta(minutes=5)
    is_tr = simulate_trades(
        settings,
        bars,
        entries,
        asym,
        symbol=cfg.symbol,
        fee_pct=cfg.fee_pct,
        slippage_pct=cfg.slippage_pct,
        period_start=cfg.is_start,
        period_end=is_end,
    )
    oos_tr = simulate_trades(
        settings,
        bars,
        entries,
        asym,
        symbol=cfg.symbol,
        fee_pct=cfg.fee_pct,
        slippage_pct=cfg.slippage_pct,
        period_start=OOS_START,
        period_end=end,
    )
    m_is = summarize_stock_trades(is_tr, cash0)
    m_oos = summarize_stock_trades(oos_tr, cash0)
    m_is["entries_signal"] = float(len(entries))
    m_oos["entries_signal"] = float(len(entries))
    m_is["symbol"] = cfg.symbol
    m_oos["symbol"] = cfg.symbol
    return m_is, m_oos
