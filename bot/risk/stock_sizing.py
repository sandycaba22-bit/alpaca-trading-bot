"""Defaults de tamaño de orden acciones (élite / Top 50) según paper vs live."""

from __future__ import annotations

import os

from bot.security.exceptions import ValidationError
from bot.security.sanitize import bounded_float

# Plantillas live ~$300 usaban MAX_NOTIONAL 40–45; en paper ~$100k bloquean toda compra.
LEGACY_TINY_MAX_NOTIONAL = 100.0

_PAPER: dict[str, dict[str, float]] = {
    "stocks": {
        "position_size_pct": 0.04,
        "max_notional_per_order": 12_000.0,
        "risk_percent_per_trade": 0.01,
    },
    "stocks_top50": {
        "position_size_pct": 0.04,
        "max_notional_per_order": 12_000.0,
        "risk_percent_per_trade": 0.005,
    },
}

_LIVE: dict[str, dict[str, float]] = {
    "stocks": {
        "position_size_pct": 0.14,
        "max_notional_per_order": 45.0,
        "risk_percent_per_trade": 0.01,
    },
    "stocks_top50": {
        "position_size_pct": 0.04,
        "max_notional_per_order": 12_000.0,
        "risk_percent_per_trade": 0.005,
    },
}


def _table(paper: bool, bot_profile: str) -> dict[str, float]:
    src = _PAPER if paper else _LIVE
    if bot_profile in src:
        return src[bot_profile]
    return {
        "position_size_pct": 0.05,
        "max_notional_per_order": 2000.0,
        "risk_percent_per_trade": 0.01,
    }


def _env_raw(key: str) -> str | None:
    raw = os.getenv(key)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def _is_legacy_tiny_notional(value: float, paper: bool, bot_profile: str) -> bool:
    return (
        paper
        and bot_profile in {"stocks", "stocks_top50"}
        and value <= LEGACY_TINY_MAX_NOTIONAL
    )


def resolve_position_size_pct(*, paper: bool, bot_profile: str) -> float:
    defaults = _table(paper, bot_profile)
    raw = _env_raw("POSITION_SIZE_PCT")
    default = defaults["position_size_pct"]
    if raw is None:
        return bounded_float(
            None, default, min_value=0.001, max_value=0.25, name="POSITION_SIZE_PCT"
        )
    val = bounded_float(raw, default, min_value=0.001, max_value=0.25, name="POSITION_SIZE_PCT")
    if paper and bot_profile in {"stocks", "stocks_top50"} and val >= 0.12:
        return default
    return val


def resolve_max_notional_per_order(*, paper: bool, bot_profile: str) -> float:
    defaults = _table(paper, bot_profile)
    raw = _env_raw("MAX_NOTIONAL_PER_ORDER")
    default = defaults["max_notional_per_order"]
    if raw is None:
        return bounded_float(
            None, default, min_value=1.0, max_value=50_000.0, name="MAX_NOTIONAL_PER_ORDER"
        )
    val = bounded_float(
        raw, default, min_value=1.0, max_value=50_000.0, name="MAX_NOTIONAL_PER_ORDER"
    )
    if _is_legacy_tiny_notional(val, paper, bot_profile):
        return default
    return val


def resolve_risk_percent_per_trade(*, paper: bool, bot_profile: str) -> float:
    defaults = _table(paper, bot_profile)
    raw = _env_raw("RISK_PERCENT_PER_TRADE")
    default = defaults["risk_percent_per_trade"]
    return bounded_float(
        raw, default, min_value=0.001, max_value=0.05, name="RISK_PERCENT_PER_TRADE"
    )


def sizing_override_note(*, paper: bool, bot_profile: str) -> str | None:
    """Mensaje si el .env trae caps de cuenta live pequeña en paper."""
    if not paper or bot_profile not in {"stocks", "stocks_top50"}:
        return None
    raw_max = _env_raw("MAX_NOTIONAL_PER_ORDER")
    if raw_max is None:
        return None
    try:
        val = float(raw_max.replace(",", "."))
    except ValueError as exc:
        raise ValidationError("MAX_NOTIONAL_PER_ORDER invalido") from exc
    if val <= LEGACY_TINY_MAX_NOTIONAL:
        eff = resolve_max_notional_per_order(paper=paper, bot_profile=bot_profile)
        return (
            f"MAX_NOTIONAL_PER_ORDER={val:.0f} es plantilla live (~$300); "
            f"en paper se usa ${eff:.0f} para permitir ≥1 acción con riesgo fijo"
        )
    return None
