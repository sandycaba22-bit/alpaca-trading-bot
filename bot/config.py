"""Carga y validación de configuración desde variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from bot.security.exceptions import ValidationError
from bot.security.sanitize import (
    bounded_float,
    bounded_int,
    sanitize_api_base_url,
    sanitize_log_level,
    sanitize_symbols,
    sanitize_crypto_symbols,
    sanitize_telegram_chat_id,
    sanitize_telegram_token,
    sanitize_timeframe,
    parse_symbol_float_map,
    parse_symbol_int_map,
)
from bot.security.secrets import register_secret

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_dust_cache():
    from bot.market.dust import DustThresholdCache

    return DustThresholdCache()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    api_key_id: str = field(repr=False)
    api_secret_key: str = field(repr=False)
    api_base_url: str
    paper: bool
    log_level: str
    dry_run: bool
    poll_interval_seconds: int
    scheduler_tick_seconds: int
    tf_3m_seconds: int
    tf_6m_seconds: int
    tf_9m_seconds: int
    tf_spike_threshold_pct: float
    tf_macro_strong_pct: float
    symbols: list[str]
    stock_symbols: list[str]
    crypto_symbols: list[str]
    sma_fast: int
    sma_slow: int
    crypto_sma_fast: int
    crypto_sma_slow: int
    bar_timeframe: str
    crypto_bar_timeframe: str
    crypto_regime_timeframe: str
    lookback_bars: int
    max_open_positions: int
    position_size_pct: float
    max_notional_per_order: float
    allow_short: bool
    stop_loss_pct: float
    take_profit_pct: float
    close_on_mode_switch: bool
    atr_period: int
    atr_stop_mult: float
    momentum_bars: int
    max_spread_pct: float
    order_limit_spread_pct: float
    order_retry_max: int
    order_retry_timeout_seconds: int
    order_retry_max_sl: int
    order_retry_timeout_sl_seconds: int
    order_retry_backoff_seconds: float
    crypto_maker_first_enabled: bool
    crypto_maker_timeout_seconds: int
    crypto_maker_fallback: str
    crypto_maker_max_retries: int
    crypto_maker_tick_inside: int
    crypto_maker_poll_seconds: float
    crypto_maker_fee_pct: float
    crypto_maker_taker_fee_pct: float
    adverse_momentum_pct: float
    backtest_years: int
    backtest_cash: float
    api_data_per_minute: int
    order_per_minute: int
    order_per_day: int
    telegram_bot_token: str = field(repr=False, default="")
    telegram_chat_id: str = field(repr=False, default="")
    backtest_train_years: int = 3
    backtest_validate_years: int = 3
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    atr_sl_mult: float = 1.5
    atr_tp_mult: float = 5.5
    atr_trailing_mult: float = 2.0
    breakeven_activate_pct: float = 0.0015
    breakeven_activate_atr_mult: float = 0.5
    breakeven_buffer: float = 0.0
    breakeven_buffer_atr_mult: float = 0.20
    min_tp_pct: float = 0.015
    # Cripto: SL más corto, TP ~5x ATR (>=3:1 vs SL 1.2), foco en el más fuerte
    crypto_atr_sl_mult: float = 1.2
    crypto_atr_tp_mult: float = 6.0
    crypto_atr_trailing_mult: float = 2.0
    crypto_min_tp_pct: float = 0.03
    crypto_breakeven_activate_pct: float = 0.0025
    crypto_breakeven_activate_atr_mult: float = 0.6
    crypto_breakeven_buffer_pct: float = 0.0025
    crypto_breakeven_buffer_atr_mult: float = 0.6
    crypto_trade_best_only: bool = True
    crypto_trend_pullback_rsi_max: float = 78.0
    # Acciones (sesión): solo comprar el ticker con mejor momentum HTF
    stock_trade_best_only: bool = True
    stock_atr_sl_mult: float = 2.0
    stock_min_stop_pct: float = 0.0035
    adx_period: int = 14
    adx_threshold: float = 20.0
    adx_threshold_overrides: dict[str, float] = field(default_factory=dict)
    adx_filter_enabled: bool = True
    volume_confirmation_period: int = 20
    volume_confirmation_mult: float = 1.0
    confirm_higher_tf: str = "15Min"
    confirm_momentum_bars: int = 5
    entry_confirmation_enabled: bool = True
    telegram_notify_filtered: bool = False
    breakout_lookback_periods: int = 20
    breakout_lookback_overrides: dict[str, int] = field(default_factory=dict)
    breakout_volume_mult: float = 1.5
    breakout_volume_mult_overrides: dict[str, float] = field(default_factory=dict)
    breakout_min_range_atr_mult: float = 0.5
    breakout_min_range_overrides: dict[str, float] = field(default_factory=dict)
    breakout_cooldown_bars: int = 5
    breakout_cooldown_overrides: dict[str, int] = field(default_factory=dict)
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    meanrev_atr_sl_mult: float = 1.0
    pullback_ema_period: int = 9
    pullback_volume_mult: float = 1.2
    pullback_ema_near_pct: float = 0.0045
    trend_pullback_rsi_min: float = 32.0
    trend_pullback_rsi_max: float = 70.0
    trend_near_high_pct: float = 0.003
    trend_micro_lookback: int = 3
    trend_range_lookback: int = 10
    trend_upper_frac: float = 0.66
    squeeze_width_lookback: int = 20
    squeeze_volume_mult: float = 2.0
    strategy_collision_priority: str = "breakout"
    strategy_collision_mode: str = "score"
    entry_score_enabled: bool = True
    entry_score_min: float = 50.0
    entry_score_macro_penalty: float = 40.0
    entry_score_spike_adverse_penalty: float = 35.0
    entry_score_spike_favor_bonus: float = 20.0
    risk_percent_per_trade: float = 0.01
    daily_loss_limit_pct: float = 0.03
    use_fixed_risk_sizing: bool = True
    stream_stale_seconds: float = 120.0
    stream_data_timeout_seconds: float = 0.0
    stream_ws_ping_interval: float = 10.0
    stream_ws_ping_timeout: float = 180.0
    stream_notify_debounce_seconds: float = 300.0
    stream_reconnect_min_seconds: float = 2.0
    stream_reconnect_max_seconds: float = 60.0
    dynamic_tp_enabled: bool = False
    dynamic_tp_atr_mult: float = 5.5
    dynamic_tp_base_pct: float = 0.015
    dynamic_tp_max_pct: float = 0.20
    dynamic_tp_gap_sell_pct: float = 0.70
    dust_threshold_pct: float = 0.0001
    dust_threshold_min_qty: float = 0.0
    dust_threshold_crypto_min: float = 0.0001
    dust_threshold_stock_min: float = 0.0
    dust_threshold_overrides: dict[str, float] = field(default_factory=dict)
    dust_threshold_refresh_seconds: int = 3600
    dust_cache: object = field(default_factory=_default_dust_cache, compare=False, hash=False)

    def validate(self) -> None:
        missing: list[str] = []
        if not self.api_key_id or self.api_key_id.startswith("your_"):
            missing.append("APCA_API_KEY_ID")
        if not self.api_secret_key or self.api_secret_key.startswith("your_"):
            missing.append("APCA_API_SECRET_KEY")
        if missing:
            raise ValidationError(
                "Credenciales Alpaca incompletas. Copia .env.example a .env y "
                f"completa: {', '.join(missing)}"
            )
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            raise ValidationError("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID deben ir juntos")
        if self.sma_fast >= self.sma_slow:
            raise ValidationError("SMA_FAST debe ser menor que SMA_SLOW")
        if self.crypto_sma_fast >= self.crypto_sma_slow:
            raise ValidationError("CRYPTO_SMA_FAST debe ser menor que CRYPTO_SMA_SLOW")


def load_settings(env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or PROJECT_ROOT / ".env")

    api_key = os.getenv("APCA_API_KEY_ID", "").strip()
    api_secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
    telegram_token = sanitize_telegram_token(os.getenv("TELEGRAM_BOT_TOKEN"))
    telegram_chat = sanitize_telegram_chat_id(os.getenv("TELEGRAM_CHAT_ID"))
    register_secret(api_key)
    register_secret(api_secret)
    register_secret(telegram_token)

    raw_base_url = os.getenv("APCA_API_BASE_URL")
    if raw_base_url is None or not raw_base_url.strip():
        raise ValidationError(
            "APCA_API_BASE_URL es obligatorio. "
            "Paper: https://paper-api.alpaca.markets | "
            "Live: https://api.alpaca.markets"
        )
    base_url = sanitize_api_base_url(raw_base_url)
    paper = "paper" in url_host(base_url)
    if api_key.startswith("AK") and paper:
        raise ValidationError(
            "APCA_API_KEY_ID parece live (AK…) pero APCA_API_BASE_URL apunta a paper. "
            "Para dinero real usa https://api.alpaca.markets"
        )
    if api_key.startswith("PK") and not paper:
        raise ValidationError(
            "APCA_API_KEY_ID parece paper (PK…) pero APCA_API_BASE_URL apunta a live. "
            "No se arranca para evitar mezclar cuentas"
        )

    stock_symbols = sanitize_symbols(os.getenv("SYMBOLS"), ["AAPL"])
    crypto_symbols = sanitize_crypto_symbols(
        os.getenv("CRYPTO_SYMBOLS"), ["BTC/USD", "ETH/USD"]
    )

    settings = Settings(
        api_key_id=api_key,
        api_secret_key=api_secret,
        api_base_url=base_url,
        paper=paper,
        log_level=sanitize_log_level(os.getenv("LOG_LEVEL"), "INFO"),
        dry_run=_as_bool(os.getenv("DRY_RUN"), default=True),
        poll_interval_seconds=bounded_int(
            os.getenv("POLL_INTERVAL_SECONDS"), 60, min_value=5, max_value=3600, name="POLL_INTERVAL_SECONDS"
        ),
        scheduler_tick_seconds=bounded_int(
            os.getenv("SCHEDULER_TICK_SECONDS"),
            bounded_int(os.getenv("POLL_INTERVAL_SECONDS"), 60, min_value=5, max_value=3600, name="POLL_INTERVAL_SECONDS"),
            min_value=5,
            max_value=3600,
            name="SCHEDULER_TICK_SECONDS",
        ),
        tf_3m_seconds=bounded_int(os.getenv("TF_3M_SECONDS"), 180, min_value=60, max_value=900, name="TF_3M_SECONDS"),
        tf_6m_seconds=bounded_int(os.getenv("TF_6M_SECONDS"), 360, min_value=120, max_value=1800, name="TF_6M_SECONDS"),
        tf_9m_seconds=bounded_int(os.getenv("TF_9M_SECONDS"), 540, min_value=180, max_value=3600, name="TF_9M_SECONDS"),
        tf_spike_threshold_pct=bounded_float(
            os.getenv("TF_SPIKE_THRESHOLD_PCT"), 0.005, min_value=0.001, max_value=0.05, name="TF_SPIKE_THRESHOLD_PCT"
        ),
        tf_macro_strong_pct=bounded_float(
            os.getenv("TF_MACRO_STRONG_PCT"), 4.0, min_value=1.0, max_value=20.0, name="TF_MACRO_STRONG_PCT"
        ),
        symbols=stock_symbols,
        stock_symbols=stock_symbols,
        crypto_symbols=crypto_symbols,
        sma_fast=bounded_int(os.getenv("SMA_FAST"), 20, min_value=2, max_value=200, name="SMA_FAST"),
        sma_slow=bounded_int(os.getenv("SMA_SLOW"), 50, min_value=3, max_value=400, name="SMA_SLOW"),
        crypto_sma_fast=bounded_int(
            os.getenv("CRYPTO_SMA_FAST"), 9, min_value=2, max_value=200, name="CRYPTO_SMA_FAST"
        ),
        crypto_sma_slow=bounded_int(
            os.getenv("CRYPTO_SMA_SLOW"), 21, min_value=3, max_value=400, name="CRYPTO_SMA_SLOW"
        ),
        bar_timeframe=sanitize_timeframe(os.getenv("BAR_TIMEFRAME"), "1Day"),
        crypto_bar_timeframe=sanitize_timeframe(
            os.getenv("CRYPTO_BAR_TIMEFRAME"), "15Min"
        ),
        crypto_regime_timeframe=sanitize_timeframe(
            os.getenv("CRYPTO_REGIME_TIMEFRAME"), "30Min"
        ),
        lookback_bars=bounded_int(
            os.getenv("LOOKBACK_BARS"), 120, min_value=30, max_value=2000, name="LOOKBACK_BARS"
        ),
        max_open_positions=bounded_int(
            os.getenv("MAX_OPEN_POSITIONS"), 3, min_value=1, max_value=20, name="MAX_OPEN_POSITIONS"
        ),
        position_size_pct=bounded_float(
            os.getenv("POSITION_SIZE_PCT"), 0.05, min_value=0.001, max_value=0.25, name="POSITION_SIZE_PCT"
        ),
        max_notional_per_order=bounded_float(
            os.getenv("MAX_NOTIONAL_PER_ORDER"), 2000.0, min_value=1.0, max_value=50_000.0, name="MAX_NOTIONAL_PER_ORDER"
        ),
        allow_short=_as_bool(os.getenv("ALLOW_SHORT"), default=False),
        stop_loss_pct=bounded_float(
            os.getenv("STOP_LOSS_PCT"), 0.01, min_value=0.001, max_value=0.25, name="STOP_LOSS_PCT"
        ),
        take_profit_pct=bounded_float(
            os.getenv("TAKE_PROFIT_PCT"), 0.015, min_value=0.002, max_value=0.50, name="TAKE_PROFIT_PCT"
        ),
        close_on_mode_switch=_as_bool(os.getenv("CLOSE_ON_MODE_SWITCH"), default=True),
        atr_period=bounded_int(os.getenv("ATR_PERIOD"), 14, min_value=5, max_value=50, name="ATR_PERIOD"),
        atr_stop_mult=bounded_float(
            os.getenv("ATR_STOP_MULT"), 1.5, min_value=0.5, max_value=5.0, name="ATR_STOP_MULT"
        ),
        momentum_bars=bounded_int(
            os.getenv("MOMENTUM_BARS"), 5, min_value=2, max_value=30, name="MOMENTUM_BARS"
        ),
        max_spread_pct=bounded_float(
            os.getenv("MAX_SPREAD_PCT"), 0.003, min_value=0.0001, max_value=0.05, name="MAX_SPREAD_PCT"
        ),
        order_limit_spread_pct=bounded_float(
            os.getenv("ORDER_LIMIT_SPREAD_PCT"),
            0.0015,
            min_value=0.0001,
            max_value=0.05,
            name="ORDER_LIMIT_SPREAD_PCT",
        ),
        order_retry_max=bounded_int(
            os.getenv("ORDER_RETRY_MAX"), 8, min_value=1, max_value=30, name="ORDER_RETRY_MAX"
        ),
        order_retry_timeout_seconds=bounded_int(
            os.getenv("ORDER_RETRY_TIMEOUT_SECONDS"),
            60,
            min_value=5,
            max_value=600,
            name="ORDER_RETRY_TIMEOUT_SECONDS",
        ),
        order_retry_max_sl=bounded_int(
            os.getenv("ORDER_RETRY_MAX_SL"), 5, min_value=1, max_value=30, name="ORDER_RETRY_MAX_SL"
        ),
        order_retry_timeout_sl_seconds=bounded_int(
            os.getenv("ORDER_RETRY_TIMEOUT_SL_SECONDS"),
            20,
            min_value=5,
            max_value=600,
            name="ORDER_RETRY_TIMEOUT_SL_SECONDS",
        ),
        order_retry_backoff_seconds=bounded_float(
            os.getenv("ORDER_RETRY_BACKOFF_SECONDS"),
            1.0,
            min_value=0.2,
            max_value=30.0,
            name="ORDER_RETRY_BACKOFF_SECONDS",
        ),
        crypto_maker_first_enabled=_as_bool(os.getenv("CRYPTO_MAKER_FIRST_ENABLED"), default=False),
        crypto_maker_timeout_seconds=bounded_int(
            os.getenv("CRYPTO_MAKER_TIMEOUT_SECONDS"),
            45,
            min_value=5,
            max_value=300,
            name="CRYPTO_MAKER_TIMEOUT_SECONDS",
        ),
        crypto_maker_fallback=(
            fb
            if (fb := (os.getenv("CRYPTO_MAKER_FALLBACK") or "taker").strip().lower())
            in {"taker", "retry", "cancel"}
            else "taker"
        ),
        crypto_maker_max_retries=bounded_int(
            os.getenv("CRYPTO_MAKER_MAX_RETRIES"),
            1,
            min_value=0,
            max_value=5,
            name="CRYPTO_MAKER_MAX_RETRIES",
        ),
        crypto_maker_tick_inside=bounded_int(
            os.getenv("CRYPTO_MAKER_TICK_INSIDE"),
            0,
            min_value=0,
            max_value=10,
            name="CRYPTO_MAKER_TICK_INSIDE",
        ),
        crypto_maker_poll_seconds=bounded_float(
            os.getenv("CRYPTO_MAKER_POLL_SECONDS"),
            2.0,
            min_value=0.5,
            max_value=15.0,
            name="CRYPTO_MAKER_POLL_SECONDS",
        ),
        crypto_maker_fee_pct=bounded_float(
            os.getenv("CRYPTO_MAKER_FEE_PCT"),
            0.15,
            min_value=0.0,
            max_value=1.0,
            name="CRYPTO_MAKER_FEE_PCT",
        ),
        crypto_maker_taker_fee_pct=bounded_float(
            os.getenv("CRYPTO_MAKER_TAKER_FEE_PCT"),
            0.25,
            min_value=0.0,
            max_value=1.0,
            name="CRYPTO_MAKER_TAKER_FEE_PCT",
        ),
        adverse_momentum_pct=bounded_float(
            os.getenv("ADVERSE_MOMENTUM_PCT"), 0.008, min_value=0.0001, max_value=0.10, name="ADVERSE_MOMENTUM_PCT"
        ),
        backtest_years=bounded_int(
            os.getenv("BACKTEST_YEARS"), 6, min_value=1, max_value=10, name="BACKTEST_YEARS"
        ),
        backtest_cash=bounded_float(
            os.getenv("BACKTEST_CASH"), 100_000.0, min_value=1000.0, max_value=10_000_000.0, name="BACKTEST_CASH"
        ),
        api_data_per_minute=bounded_int(
            os.getenv("API_DATA_PER_MINUTE"), 120, min_value=10, max_value=200, name="API_DATA_PER_MINUTE"
        ),
        order_per_minute=bounded_int(
            os.getenv("ORDER_PER_MINUTE"), 10, min_value=1, max_value=30, name="ORDER_PER_MINUTE"
        ),
        order_per_day=bounded_int(
            os.getenv("ORDER_PER_DAY"), 40, min_value=1, max_value=200, name="ORDER_PER_DAY"
        ),
        telegram_bot_token=telegram_token,
        telegram_chat_id=telegram_chat,
        backtest_train_years=bounded_int(
            os.getenv("BACKTEST_TRAIN_YEARS"), 3, min_value=1, max_value=8, name="BACKTEST_TRAIN_YEARS"
        ),
        backtest_validate_years=bounded_int(
            os.getenv("BACKTEST_VALIDATE_YEARS"), 3, min_value=1, max_value=8, name="BACKTEST_VALIDATE_YEARS"
        ),
        atr_sl_mult=bounded_float(
            os.getenv("ATR_SL_MULTIPLIER"),
            bounded_float(os.getenv("ATR_STOP_MULT"), 1.5, min_value=0.5, max_value=5.0, name="ATR_STOP_MULT"),
            min_value=0.5,
            max_value=8.0,
            name="ATR_SL_MULTIPLIER",
        ),
        atr_tp_mult=bounded_float(
            os.getenv("ATR_TP_MULTIPLIER"), 5.5, min_value=0.5, max_value=12.0, name="ATR_TP_MULTIPLIER"
        ),
        atr_trailing_mult=bounded_float(
            os.getenv("ATR_TRAILING_MULTIPLIER"),
            2.0,
            min_value=0.5,
            max_value=8.0,
            name="ATR_TRAILING_MULTIPLIER",
        ),
        breakeven_activate_pct=bounded_float(
            os.getenv("BREAKEVEN_ACTIVATE_PCT"),
            0.0015,
            min_value=0.0001,
            max_value=0.05,
            name="BREAKEVEN_ACTIVATE_PCT",
        ),
        breakeven_activate_atr_mult=bounded_float(
            os.getenv("BREAKEVEN_ACTIVATE_ATR_MULT"),
            0.5,
            min_value=0.1,
            max_value=5.0,
            name="BREAKEVEN_ACTIVATE_ATR_MULT",
        ),
        breakeven_buffer=bounded_float(
            os.getenv("BREAKEVEN_BUFFER"),
            0.0,
            min_value=0.0,
            max_value=50.0,
            name="BREAKEVEN_BUFFER",
        ),
        breakeven_buffer_atr_mult=bounded_float(
            os.getenv("BREAKEVEN_BUFFER_ATR_MULT"),
            0.20,
            min_value=0.01,
            max_value=1.0,
            name="BREAKEVEN_BUFFER_ATR_MULT",
        ),
        min_tp_pct=bounded_float(
            os.getenv("MIN_TP_PCT"),
            0.015,
            min_value=0.0,
            max_value=0.10,
            name="MIN_TP_PCT",
        ),
        crypto_atr_sl_mult=bounded_float(
            os.getenv("CRYPTO_ATR_SL_MULTIPLIER"),
            1.2,
            min_value=0.5,
            max_value=8.0,
            name="CRYPTO_ATR_SL_MULTIPLIER",
        ),
        crypto_atr_tp_mult=bounded_float(
            os.getenv("CRYPTO_ATR_TP_MULTIPLIER"),
            6.0,
            min_value=0.5,
            max_value=15.0,
            name="CRYPTO_ATR_TP_MULTIPLIER",
        ),
        crypto_atr_trailing_mult=bounded_float(
            os.getenv("CRYPTO_ATR_TRAILING_MULTIPLIER"),
            2.0,
            min_value=0.5,
            max_value=10.0,
            name="CRYPTO_ATR_TRAILING_MULTIPLIER",
        ),
        crypto_min_tp_pct=bounded_float(
            os.getenv("CRYPTO_MIN_TP_PCT"),
            0.03,
            min_value=0.0,
            max_value=0.20,
            name="CRYPTO_MIN_TP_PCT",
        ),
        crypto_breakeven_activate_pct=bounded_float(
            os.getenv("CRYPTO_BREAKEVEN_ACTIVATE_PCT"),
            0.0025,
            min_value=0.0001,
            max_value=0.08,
            name="CRYPTO_BREAKEVEN_ACTIVATE_PCT",
        ),
        crypto_breakeven_activate_atr_mult=bounded_float(
            os.getenv("CRYPTO_BREAKEVEN_ACTIVATE_ATR_MULT"),
            0.6,
            min_value=0.1,
            max_value=6.0,
            name="CRYPTO_BREAKEVEN_ACTIVATE_ATR_MULT",
        ),
        crypto_breakeven_buffer_pct=bounded_float(
            os.getenv("CRYPTO_BREAKEVEN_BUFFER_PCT"),
            0.0025,
            min_value=0.0,
            max_value=0.05,
            name="CRYPTO_BREAKEVEN_BUFFER_PCT",
        ),
        crypto_breakeven_buffer_atr_mult=bounded_float(
            os.getenv("CRYPTO_BREAKEVEN_BUFFER_ATR_MULT"),
            0.6,
            min_value=0.01,
            max_value=2.0,
            name="CRYPTO_BREAKEVEN_BUFFER_ATR_MULT",
        ),
        crypto_trade_best_only=_as_bool(os.getenv("CRYPTO_TRADE_BEST_ONLY"), default=True),
        crypto_trend_pullback_rsi_max=bounded_float(
            os.getenv("CRYPTO_TREND_PULLBACK_RSI_MAX"),
            78.0,
            min_value=55.0,
            max_value=90.0,
            name="CRYPTO_TREND_PULLBACK_RSI_MAX",
        ),
        stock_trade_best_only=_as_bool(os.getenv("STOCK_TRADE_BEST_ONLY"), default=True),
        stock_atr_sl_mult=bounded_float(
            os.getenv("STOCK_ATR_SL_MULTIPLIER"),
            2.0,
            min_value=0.5,
            max_value=8.0,
            name="STOCK_ATR_SL_MULTIPLIER",
        ),
        stock_min_stop_pct=bounded_float(
            os.getenv("STOCK_MIN_STOP_PCT"),
            0.0035,
            min_value=0.0,
            max_value=0.02,
            name="STOCK_MIN_STOP_PCT",
        ),
        adx_period=bounded_int(os.getenv("ADX_PERIOD"), 14, min_value=5, max_value=50, name="ADX_PERIOD"),
        adx_threshold=bounded_float(
            os.getenv("ADX_THRESHOLD"), 20.0, min_value=1.0, max_value=80.0, name="ADX_THRESHOLD"
        ),
        adx_threshold_overrides=_load_adx_overrides(stock_symbols, crypto_symbols),
        adx_filter_enabled=_as_bool(os.getenv("ADX_FILTER_ENABLED"), default=True),
        volume_confirmation_period=bounded_int(
            os.getenv("VOLUME_CONFIRMATION_PERIOD"),
            20,
            min_value=3,
            max_value=200,
            name="VOLUME_CONFIRMATION_PERIOD",
        ),
        volume_confirmation_mult=bounded_float(
            os.getenv("VOLUME_CONFIRMATION_MULT"),
            1.0,
            min_value=0.1,
            max_value=5.0,
            name="VOLUME_CONFIRMATION_MULT",
        ),
        confirm_higher_tf=sanitize_timeframe(os.getenv("CONFIRM_HIGHER_TF"), "15Min"),
        confirm_momentum_bars=bounded_int(
            os.getenv("CONFIRM_MOMENTUM_BARS"), 5, min_value=2, max_value=30, name="CONFIRM_MOMENTUM_BARS"
        ),
        entry_confirmation_enabled=_as_bool(os.getenv("ENTRY_CONFIRMATION_ENABLED"), default=True),
        telegram_notify_filtered=_as_bool(os.getenv("TELEGRAM_NOTIFY_FILTERED"), default=False),
        breakout_lookback_periods=bounded_int(
            os.getenv("BREAKOUT_LOOKBACK_PERIODS"),
            20,
            min_value=5,
            max_value=200,
            name="BREAKOUT_LOOKBACK_PERIODS",
        ),
        breakout_lookback_overrides=_load_int_overrides(
            "BREAKOUT_LOOKBACK_OVERRIDES",
            "BREAKOUT_LOOKBACK_",
            stock_symbols,
            crypto_symbols,
            min_value=5,
            max_value=200,
        ),
        breakout_volume_mult=bounded_float(
            os.getenv("BREAKOUT_VOLUME_MULT"),
            1.5,
            min_value=0.5,
            max_value=8.0,
            name="BREAKOUT_VOLUME_MULT",
        ),
        breakout_volume_mult_overrides=_load_float_overrides(
            "BREAKOUT_VOLUME_MULT_OVERRIDES",
            "BREAKOUT_VOLUME_MULT_",
            stock_symbols,
            crypto_symbols,
            min_value=0.5,
            max_value=8.0,
        ),
        breakout_min_range_atr_mult=bounded_float(
            os.getenv("BREAKOUT_MIN_RANGE_ATR_MULT"),
            0.5,
            min_value=0.1,
            max_value=5.0,
            name="BREAKOUT_MIN_RANGE_ATR_MULT",
        ),
        breakout_min_range_overrides=_load_float_overrides(
            "BREAKOUT_MIN_RANGE_OVERRIDES",
            "BREAKOUT_MIN_RANGE_ATR_MULT_",
            stock_symbols,
            crypto_symbols,
            min_value=0.1,
            max_value=5.0,
        ),
        breakout_cooldown_bars=bounded_int(
            os.getenv("BREAKOUT_COOLDOWN_BARS"),
            5,
            min_value=1,
            max_value=50,
            name="BREAKOUT_COOLDOWN_BARS",
        ),
        breakout_cooldown_overrides=_load_int_overrides(
            "BREAKOUT_COOLDOWN_OVERRIDES",
            "BREAKOUT_COOLDOWN_",
            stock_symbols,
            crypto_symbols,
            min_value=1,
            max_value=50,
        ),
        bb_period=bounded_int(os.getenv("BB_PERIOD"), 20, min_value=5, max_value=80, name="BB_PERIOD"),
        bb_std=bounded_float(os.getenv("BB_STD"), 2.0, min_value=0.5, max_value=4.0, name="BB_STD"),
        rsi_period=bounded_int(os.getenv("RSI_PERIOD"), 14, min_value=5, max_value=50, name="RSI_PERIOD"),
        rsi_oversold=bounded_float(
            os.getenv("RSI_OVERSOLD"), 30.0, min_value=5.0, max_value=45.0, name="RSI_OVERSOLD"
        ),
        rsi_overbought=bounded_float(
            os.getenv("RSI_OVERBOUGHT"), 70.0, min_value=55.0, max_value=95.0, name="RSI_OVERBOUGHT"
        ),
        meanrev_atr_sl_mult=bounded_float(
            os.getenv("MEANREV_ATR_SL_MULT"), 1.0, min_value=0.4, max_value=3.0, name="MEANREV_ATR_SL_MULT"
        ),
        pullback_ema_period=bounded_int(
            os.getenv("PULLBACK_EMA_PERIOD"), 9, min_value=3, max_value=50, name="PULLBACK_EMA_PERIOD"
        ),
        pullback_volume_mult=bounded_float(
            os.getenv("PULLBACK_VOLUME_MULT"), 1.2, min_value=0.5, max_value=5.0, name="PULLBACK_VOLUME_MULT"
        ),
        pullback_ema_near_pct=bounded_float(
            os.getenv("PULLBACK_EMA_NEAR_PCT"),
            0.0045,
            min_value=0.0,
            max_value=0.02,
            name="PULLBACK_EMA_NEAR_PCT",
        ),
        trend_pullback_rsi_min=bounded_float(
            os.getenv("TREND_PULLBACK_RSI_MIN"),
            32.0,
            min_value=20.0,
            max_value=50.0,
            name="TREND_PULLBACK_RSI_MIN",
        ),
        trend_pullback_rsi_max=bounded_float(
            os.getenv("TREND_PULLBACK_RSI_MAX"),
            70.0,
            min_value=45.0,
            max_value=80.0,
            name="TREND_PULLBACK_RSI_MAX",
        ),
        trend_near_high_pct=bounded_float(
            os.getenv("TREND_NEAR_HIGH_PCT"),
            0.003,
            min_value=0.0005,
            max_value=0.02,
            name="TREND_NEAR_HIGH_PCT",
        ),
        trend_micro_lookback=bounded_int(
            os.getenv("TREND_MICRO_LOOKBACK"),
            3,
            min_value=2,
            max_value=8,
            name="TREND_MICRO_LOOKBACK",
        ),
        trend_range_lookback=bounded_int(
            os.getenv("TREND_RANGE_LOOKBACK"),
            10,
            min_value=5,
            max_value=30,
            name="TREND_RANGE_LOOKBACK",
        ),
        trend_upper_frac=bounded_float(
            os.getenv("TREND_UPPER_FRAC"),
            0.66,
            min_value=0.50,
            max_value=0.90,
            name="TREND_UPPER_FRAC",
        ),
        squeeze_width_lookback=bounded_int(
            os.getenv("SQUEEZE_WIDTH_LOOKBACK"), 20, min_value=5, max_value=80, name="SQUEEZE_WIDTH_LOOKBACK"
        ),
        squeeze_volume_mult=bounded_float(
            os.getenv("SQUEEZE_VOLUME_MULT"), 2.0, min_value=1.0, max_value=8.0, name="SQUEEZE_VOLUME_MULT"
        ),
        strategy_collision_priority=(
            os.getenv("STRATEGY_COLLISION_PRIORITY", "breakout").strip().lower() or "breakout"
        ),
        strategy_collision_mode=(
            os.getenv("STRATEGY_COLLISION_MODE", "score").strip().lower() or "score"
        ),
        entry_score_enabled=_as_bool(os.getenv("ENTRY_SCORE_ENABLED"), default=True),
        entry_score_min=bounded_float(
            os.getenv("ENTRY_SCORE_MIN"), 50.0, min_value=20.0, max_value=120.0, name="ENTRY_SCORE_MIN"
        ),
        entry_score_macro_penalty=bounded_float(
            os.getenv("ENTRY_SCORE_MACRO_PENALTY"),
            40.0,
            min_value=10.0,
            max_value=80.0,
            name="ENTRY_SCORE_MACRO_PENALTY",
        ),
        entry_score_spike_adverse_penalty=bounded_float(
            os.getenv("ENTRY_SCORE_SPIKE_ADVERSE_PENALTY"),
            35.0,
            min_value=10.0,
            max_value=80.0,
            name="ENTRY_SCORE_SPIKE_ADVERSE_PENALTY",
        ),
        entry_score_spike_favor_bonus=bounded_float(
            os.getenv("ENTRY_SCORE_SPIKE_FAVOR_BONUS"),
            20.0,
            min_value=5.0,
            max_value=50.0,
            name="ENTRY_SCORE_SPIKE_FAVOR_BONUS",
        ),
        risk_percent_per_trade=bounded_float(
            os.getenv("RISK_PERCENT_PER_TRADE"),
            0.01,
            min_value=0.001,
            max_value=0.05,
            name="RISK_PERCENT_PER_TRADE",
        ),
        daily_loss_limit_pct=bounded_float(
            os.getenv("DAILY_LOSS_LIMIT_PERCENT"),
            0.03,
            min_value=0.005,
            max_value=0.20,
            name="DAILY_LOSS_LIMIT_PERCENT",
        ),
        use_fixed_risk_sizing=_as_bool(os.getenv("USE_FIXED_RISK_SIZING"), default=True),
        stream_stale_seconds=bounded_float(
            os.getenv("STREAM_STALE_SECONDS"),
            120.0,
            min_value=30.0,
            max_value=600.0,
            name="STREAM_STALE_SECONDS",
        ),
        stream_data_timeout_seconds=bounded_float(
            os.getenv("STREAM_DATA_TIMEOUT_SECONDS"),
            0.0,
            min_value=0.0,
            max_value=600.0,
            name="STREAM_DATA_TIMEOUT_SECONDS",
        ),
        stream_ws_ping_interval=bounded_float(
            os.getenv("STREAM_WS_PING_INTERVAL"),
            10.0,
            min_value=5.0,
            max_value=60.0,
            name="STREAM_WS_PING_INTERVAL",
        ),
        stream_ws_ping_timeout=bounded_float(
            os.getenv("STREAM_WS_PING_TIMEOUT"),
            180.0,
            min_value=20.0,
            max_value=300.0,
            name="STREAM_WS_PING_TIMEOUT",
        ),
        stream_notify_debounce_seconds=bounded_float(
            os.getenv("STREAM_NOTIFY_DEBOUNCE_SECONDS"),
            300.0,
            min_value=60.0,
            max_value=1800.0,
            name="STREAM_NOTIFY_DEBOUNCE_SECONDS",
        ),
        stream_reconnect_min_seconds=bounded_float(
            os.getenv("STREAM_RECONNECT_MIN_SECONDS"),
            2.0,
            min_value=1.0,
            max_value=30.0,
            name="STREAM_RECONNECT_MIN_SECONDS",
        ),
        stream_reconnect_max_seconds=bounded_float(
            os.getenv("STREAM_RECONNECT_MAX_SECONDS"),
            60.0,
            min_value=5.0,
            max_value=300.0,
            name="STREAM_RECONNECT_MAX_SECONDS",
        ),
        dynamic_tp_enabled=_as_bool(os.getenv("DYNAMIC_TP_ENABLED"), default=False),
        dynamic_tp_atr_mult=bounded_float(
            os.getenv("DYNAMIC_TP_ATR_MULT"),
            5.5,
            min_value=0.5,
            max_value=10.0,
            name="DYNAMIC_TP_ATR_MULT",
        ),
        dynamic_tp_base_pct=bounded_float(
            os.getenv("DYNAMIC_TP_BASE_PCT"),
            bounded_float(os.getenv("TAKE_PROFIT_PCT"), 0.015, min_value=0.001, max_value=0.5, name="TAKE_PROFIT_PCT"),
            min_value=0.001,
            max_value=0.5,
            name="DYNAMIC_TP_BASE_PCT",
        ),
        dynamic_tp_max_pct=bounded_float(
            os.getenv("DYNAMIC_TP_MAX_PCT"),
            0.20,
            min_value=0.005,
            max_value=0.5,
            name="DYNAMIC_TP_MAX_PCT",
        ),
        dynamic_tp_gap_sell_pct=bounded_float(
            os.getenv("DYNAMIC_TP_GAP_SELL_PCT"),
            0.70,
            min_value=0.1,
            max_value=0.95,
            name="DYNAMIC_TP_GAP_SELL_PCT",
        ),
        dust_threshold_pct=bounded_float(
            os.getenv("DUST_THRESHOLD_PCT"),
            0.0001,
            min_value=0.0,
            max_value=0.05,
            name="DUST_THRESHOLD_PCT",
        ),
        dust_threshold_min_qty=bounded_float(
            os.getenv("DUST_THRESHOLD_MIN_QTY"),
            0.0,
            min_value=0.0,
            max_value=1000.0,
            name="DUST_THRESHOLD_MIN_QTY",
        ),
        dust_threshold_crypto_min=bounded_float(
            os.getenv("DUST_THRESHOLD_CRYPTO_MIN"),
            0.0001,
            min_value=0.0,
            max_value=1.0,
            name="DUST_THRESHOLD_CRYPTO_MIN",
        ),
        dust_threshold_stock_min=bounded_float(
            os.getenv("DUST_THRESHOLD_STOCK_MIN"),
            0.0,
            min_value=0.0,
            max_value=100.0,
            name="DUST_THRESHOLD_STOCK_MIN",
        ),
        dust_threshold_overrides=_load_dust_overrides(stock_symbols, crypto_symbols),
        dust_threshold_refresh_seconds=bounded_int(
            os.getenv("DUST_THRESHOLD_REFRESH_SECONDS"),
            3600,
            min_value=0,
            max_value=86400 * 7,
            name="DUST_THRESHOLD_REFRESH_SECONDS",
        ),
    )
    settings.validate()
    return settings


def _load_adx_overrides(stock_symbols: list[str], crypto_symbols: list[str]) -> dict[str, float]:
    return _load_float_overrides(
        "ADX_THRESHOLD_OVERRIDES",
        "ADX_THRESHOLD_",
        stock_symbols,
        crypto_symbols,
        min_value=1.0,
        max_value=80.0,
    )


def _load_float_overrides(
    map_env: str,
    prefix: str,
    stock_symbols: list[str],
    crypto_symbols: list[str],
    *,
    min_value: float,
    max_value: float,
) -> dict[str, float]:
    overrides = parse_symbol_float_map(
        os.getenv(map_env),
        min_value=min_value,
        max_value=max_value,
        name=map_env,
    )
    for symbol in list(stock_symbols) + list(crypto_symbols):
        env_name = prefix + str(symbol).replace("/", "_")
        raw = os.getenv(env_name)
        if raw is None or not str(raw).strip():
            continue
        overrides[str(symbol).upper()] = bounded_float(
            raw, min_value, min_value=min_value, max_value=max_value, name=env_name
        )
    return overrides


def _load_dust_overrides(stock_symbols: list[str], crypto_symbols: list[str]) -> dict[str, float]:
    return _load_float_overrides(
        "DUST_THRESHOLD_OVERRIDES",
        "DUST_THRESHOLD_",
        stock_symbols,
        crypto_symbols,
        min_value=0.0,
        max_value=1000.0,
    )


def _load_int_overrides(
    map_env: str,
    prefix: str,
    stock_symbols: list[str],
    crypto_symbols: list[str],
    *,
    min_value: int,
    max_value: int,
) -> dict[str, int]:
    overrides = parse_symbol_int_map(
        os.getenv(map_env),
        min_value=min_value,
        max_value=max_value,
        name=map_env,
    )
    for symbol in list(stock_symbols) + list(crypto_symbols):
        env_name = prefix + str(symbol).replace("/", "_")
        raw = os.getenv(env_name)
        if raw is None or not str(raw).strip():
            continue
        overrides[str(symbol).upper()] = bounded_int(
            raw, min_value, min_value=min_value, max_value=max_value, name=env_name
        )
    return overrides


def url_host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()
