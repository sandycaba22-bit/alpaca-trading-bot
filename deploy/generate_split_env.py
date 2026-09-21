#!/usr/bin/env python3
"""Genera .env.stocks y .env.crypto desde el .env híbrido existente (sin secretos en git)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _write_env(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OK {path.name} ({len(lines)} lineas)")


def main() -> int:
    source = ROOT / ".env"
    if not source.is_file():
        print("ERROR: no existe .env en la raiz del repo.", file=sys.stderr)
        return 1

    env = _load_env(source)
    required = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_API_BASE_URL")
    missing = [key for key in required if not env.get(key)]
    if missing:
        print(f"ERROR: .env incompleto, faltan: {', '.join(missing)}", file=sys.stderr)
        return 1

    key = env["APCA_API_KEY_ID"]
    secret = env["APCA_API_SECRET_KEY"]
    base_url = env["APCA_API_BASE_URL"]
    paper = "paper" in base_url.lower()

    tg_token = env.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = env.get("TELEGRAM_CHAT_ID", "")
    sess = env.get("SESSION_SECRET", "change_me_to_a_long_random_string")
    admin_u = env.get("ADMIN_USER", "admin")
    admin_p = env.get("ADMIN_PASS", "change_me")
    cookie_secure = env.get("COOKIE_SECURE", "false")
    log_level = env.get("LOG_LEVEL", "INFO")
    dry_run = env.get("DRY_RUN", "false")
    sched = env.get("SCHEDULER_TICK_SECONDS", env.get("POLL_INTERVAL_SECONDS", "60"))
    poll = env.get("POLL_INTERVAL_SECONDS", sched)

    stocks_lines = [
        "# Generado por deploy/generate_split_env.py — no editar a mano salvo keys live",
        "BOT_PROFILE=stocks",
        "DATA_DIR=data-stocks",
        "TELEGRAM_PREFIX=[ACCIONES]",
        "",
        f"APCA_API_KEY_ID={key}",
        f"APCA_API_SECRET_KEY={secret}",
        f"APCA_API_BASE_URL={base_url}",
        "",
        f"LOG_LEVEL={log_level}",
        f"DRY_RUN={dry_run}",
        f"SCHEDULER_TICK_SECONDS={sched}",
        f"POLL_INTERVAL_SECONDS={poll}",
        "",
        "SYMBOLS=AAPL,MSFT",
        "CRYPTO_SYMBOLS=",
        "CLOSE_ON_MODE_SWITCH=false",
        "",
        "SMA_FAST=20",
        "SMA_SLOW=50",
        "BAR_TIMEFRAME=1Day",
        "LOOKBACK_BARS=120",
        "MAX_OPEN_POSITIONS=3",
        "POSITION_SIZE_PCT=0.05",
        "MAX_NOTIONAL_PER_ORDER=2000",
        "ALLOW_SHORT=false",
        "",
        f"TELEGRAM_BOT_TOKEN={tg_token}",
        f"TELEGRAM_CHAT_ID={tg_chat}",
        "",
        "PORT=3000",
        "HOST=0.0.0.0",
        f"COOKIE_SECURE={cookie_secure}",
        f"SESSION_SECRET={sess}",
        f"ADMIN_USER={admin_u}",
        f"ADMIN_PASS={admin_p}",
    ]
    if paper:
        stocks_lines.insert(1, "# NOTA: usa keys LIVE (AK...) cuando las tengas; ahora corre en paper")

    crypto_lines = [
        "# Generado por deploy/generate_split_env.py",
        "BOT_PROFILE=crypto",
        "DATA_DIR=data-crypto",
        "TELEGRAM_PREFIX=[CRIPTO]",
        "",
        f"APCA_API_KEY_ID={key}",
        f"APCA_API_SECRET_KEY={secret}",
        "APCA_API_BASE_URL=https://paper-api.alpaca.markets",
        "",
        f"LOG_LEVEL={log_level}",
        f"DRY_RUN={dry_run}",
        f"SCHEDULER_TICK_SECONDS={sched}",
        f"POLL_INTERVAL_SECONDS={poll}",
        "",
        "SYMBOLS=",
        "CRYPTO_SYMBOLS=BTC/USD,ETH/USD",
        "CLOSE_ON_MODE_SWITCH=false",
        "",
        "CRYPTO_SMA_FAST=9",
        "CRYPTO_SMA_SLOW=21",
        "CRYPTO_BAR_TIMEFRAME=15Min",
        "CRYPTO_REGIME_TIMEFRAME=30Min",
        "LOOKBACK_BARS=120",
        "MAX_OPEN_POSITIONS=2",
        "POSITION_SIZE_PCT=0.05",
        "MAX_NOTIONAL_PER_ORDER=2000",
        "",
        f"TELEGRAM_BOT_TOKEN={tg_token}",
        f"TELEGRAM_CHAT_ID={tg_chat}",
        "",
        "PORT=3001",
        "HOST=0.0.0.0",
        f"COOKIE_SECURE={cookie_secure}",
        f"SESSION_SECRET={sess}",
        f"ADMIN_USER={admin_u}",
        f"ADMIN_PASS={admin_p}",
    ]
    if not paper:
        crypto_lines.insert(
            5,
            "# NOTA: crypto exige paper; si .env era live, pon keys PK... manualmente aqui",
        )

    _write_env(ROOT / ".env.stocks", stocks_lines)
    _write_env(ROOT / ".env.crypto", crypto_lines)

    mode = "paper" if paper else "live"
    print(f"Listo | fuente=.env ({mode}) | stocks=AAPL,MSFT | crypto=BTC/USD,ETH/USD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
