"""Settings aislados — BOT_PROFILE=crypto_night (no mezclar con Settings stocks)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from bot.config import PROJECT_ROOT, ValidationError
from bot.security.sanitize import sanitize_api_base_url, sanitize_telegram_chat_id, sanitize_telegram_token
from bot.security.secrets import register_secret
from strategies.crypto_night.bias import parse_bias_mode
from strategies.crypto_night.filter_profile import (
    CryptoNightFilters,
    filters_for_profile,
    parse_filter_profile,
)
from strategies.crypto_night.variants import SweepVariant, parse_sweep_variant


@dataclass(frozen=True)
class CryptoNightSettings:
    api_key_id: str
    api_secret_key: str
    api_base_url: str
    paper: bool
    bot_profile: str
    data_dir: Path
    log_dir: Path
    symbols: tuple[str, ...]
    sweep_variant: SweepVariant
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_prefix: str
    poll_seconds: int
    dry_run: bool
    client_order_prefix: str
    max_positions: int
    bias_mode: str
    filter_profile: str
    filters: CryptoNightFilters


def load_crypto_night_settings(env_path: Path | None = None) -> CryptoNightSettings:
    if env_path is None:
        env_file = os.getenv("ENV_FILE", ".env.crypto_night").strip()
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = PROJECT_ROOT / env_path
    load_dotenv(env_path)

    profile = (os.getenv("BOT_PROFILE") or "crypto_night").strip().lower()
    if profile != "crypto_night":
        raise ValidationError(f"Este loader es solo crypto_night, got {profile!r}")

    api_key = os.getenv("APCA_API_KEY_ID", "").strip()
    api_secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
    if not api_key or not api_secret:
        raise ValidationError("Credenciales Alpaca incompletas para crypto_night")

    base_url = sanitize_api_base_url(os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"))
    paper = "paper" in base_url
    register_secret(api_key)
    register_secret(api_secret)

    data_dir = PROJECT_ROOT / (os.getenv("DATA_DIR") or "data-crypto-night")
    log_dir = PROJECT_ROOT / "logs"
    syms_raw = os.getenv("SYMBOLS", "BTC/USD,ETH/USD")
    symbols = tuple(s.strip() for s in syms_raw.split(",") if s.strip())
    variant = parse_sweep_variant(os.getenv("SWEEP_VARIANT", "V1"))
    try:
        bias_mode = parse_bias_mode(os.getenv("CRYPTO_NIGHT_BIAS_MODE", "4h_1d"))
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    try:
        filter_profile = parse_filter_profile(os.getenv("CRYPTO_NIGHT_FILTER_PROFILE", "strict"))
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    filters = filters_for_profile(filter_profile)

    tg_token = sanitize_telegram_token(os.getenv("TELEGRAM_BOT_TOKEN"))
    tg_chat = sanitize_telegram_chat_id(os.getenv("TELEGRAM_CHAT_ID"))
    if bool(tg_token) ^ bool(tg_chat):
        raise ValidationError("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID deben ir juntos")

    return CryptoNightSettings(
        api_key_id=api_key,
        api_secret_key=api_secret,
        api_base_url=base_url,
        paper=paper,
        bot_profile=profile,
        data_dir=data_dir,
        log_dir=log_dir,
        symbols=symbols,
        sweep_variant=variant,
        telegram_bot_token=tg_token,
        telegram_chat_id=tg_chat,
        telegram_prefix=(os.getenv("TELEGRAM_PREFIX") or "[CN]").strip()[:32],
        poll_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        dry_run=os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"},
        client_order_prefix=(os.getenv("CLIENT_ORDER_PREFIX") or "CN-").strip()[:8],
        max_positions=int(os.getenv("MAX_POSITIONS", "1")),
        bias_mode=bias_mode,
        filter_profile=filter_profile,
        filters=filters,
    )
