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
    sanitize_timeframe,
)
from bot.security.secrets import register_secret

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    symbols: list[str]
    sma_fast: int
    sma_slow: int
    bar_timeframe: str
    lookback_bars: int
    max_open_positions: int
    position_size_pct: float
    max_notional_per_order: float
    allow_short: bool
    stop_loss_pct: float
    take_profit_pct: float
    atr_period: int
    atr_stop_mult: float
    momentum_bars: int
    max_spread_pct: float
    adverse_momentum_pct: float
    backtest_years: int
    backtest_cash: float
    api_data_per_minute: int
    order_per_minute: int
    order_per_day: int
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")

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
        if self.sma_fast >= self.sma_slow:
            raise ValidationError("SMA_FAST debe ser menor que SMA_SLOW")


def load_settings(env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or PROJECT_ROOT / ".env")

    api_key = os.getenv("APCA_API_KEY_ID", "").strip()
    api_secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
    register_secret(api_key)
    register_secret(api_secret)

    base_url = sanitize_api_base_url(
        os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    )
    paper = "paper" in url_host(base_url)

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
        symbols=sanitize_symbols(os.getenv("SYMBOLS"), ["AAPL"]),
        sma_fast=bounded_int(os.getenv("SMA_FAST"), 20, min_value=2, max_value=200, name="SMA_FAST"),
        sma_slow=bounded_int(os.getenv("SMA_SLOW"), 50, min_value=3, max_value=400, name="SMA_SLOW"),
        bar_timeframe=sanitize_timeframe(os.getenv("BAR_TIMEFRAME"), "1Day"),
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
            os.getenv("STOP_LOSS_PCT"), 0.02, min_value=0.001, max_value=0.25, name="STOP_LOSS_PCT"
        ),
        take_profit_pct=bounded_float(
            os.getenv("TAKE_PROFIT_PCT"), 0.05, min_value=0.002, max_value=0.50, name="TAKE_PROFIT_PCT"
        ),
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
    )
    settings.validate()
    return settings


def url_host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()
