"""Entrada aislada — Crypto Night Fortress (no importa motor stocks)."""

from __future__ import annotations

import logging
import sys

from bot.crypto_night_settings import load_crypto_night_settings
from bot.logging_setup import setup_logging
from bot.security.exceptions import ValidationError
from strategies.crypto_night.live_engine import CryptoNightEngine

logger = logging.getLogger("crypto_night")


def main() -> int:
    try:
        settings = load_crypto_night_settings()
    except ValidationError as exc:
        print(f"Configuracion invalida: {exc}", file=sys.stderr)
        return 1

    setup_logging("INFO", settings.log_dir)
    engine = CryptoNightEngine(settings)
    try:
        engine.run_loop()
    except KeyboardInterrupt:
        engine.request_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
