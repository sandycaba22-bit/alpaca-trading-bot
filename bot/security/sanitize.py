"""Validación y sanitización de entradas (inyección, path traversal, SSRF)."""

from __future__ import annotations

import math
import re
from pathlib import Path
from urllib.parse import urlparse

from bot.security.exceptions import ValidationError

# Tickers Alpaca: AAPL, BRK.B, BF.B. Sin espacios, shells ni rutas.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_UNSAFE_SYMBOL = re.compile(r"[^A-Z0-9.]|\.\.|^\.|\.$")
_CRLF = re.compile(r"[\r\n\x00\x1b]")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

ALLOWED_API_HOSTS = frozenset(
    {
        "paper-api.alpaca.markets",
        "api.alpaca.markets",
    }
)
ALLOWED_TIMEFRAMES = frozenset({"1Min", "5Min", "15Min", "1Hour", "1Day"})
ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
MAX_SYMBOLS = 8


def sanitize_log_text(value: str, max_len: int = 500) -> str:
    """Elimina CRLF/ANSI para evitar inyección en logs."""
    cleaned = _CRLF.sub(" ", str(value))
    cleaned = "".join(ch if 32 <= ord(ch) <= 126 or ord(ch) >= 160 else " " for ch in cleaned)
    return cleaned[:max_len]


def sanitize_symbol(raw: str) -> str:
    if raw is None:
        raise ValidationError("Simbolo vacio")
    symbol = str(raw).strip().upper()
    if not symbol or _UNSAFE_SYMBOL.search(symbol) or not _SYMBOL_RE.match(symbol):
        raise ValidationError("Simbolo rechazado: formato invalido")
    return symbol


def sanitize_symbols(raw: str | list[str] | None, default: list[str] | None = None) -> list[str]:
    if raw is None or raw == "":
        values = list(default or [])
    elif isinstance(raw, str):
        values = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        values = list(raw)

    symbols = [sanitize_symbol(item) for item in values]
    if not symbols:
        raise ValidationError("SYMBOLS no puede estar vacio")
    if len(symbols) > MAX_SYMBOLS:
        raise ValidationError(f"Demasiados simbolos (max {MAX_SYMBOLS})")
    if len(set(symbols)) != len(symbols):
        raise ValidationError("SYMBOLS contiene duplicados")
    return symbols


def sanitize_api_base_url(raw: str) -> str:
    if not raw or not str(raw).strip():
        raise ValidationError("APCA_API_BASE_URL vacio")
    url = str(raw).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValidationError("APCA_API_BASE_URL debe usar HTTPS")
    if parsed.username or parsed.password:
        raise ValidationError("APCA_API_BASE_URL no admite credenciales en la URL")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_API_HOSTS:
        raise ValidationError("APCA_API_BASE_URL fuera de la allowlist de Alpaca")
    if parsed.query or parsed.fragment or parsed.params:
        raise ValidationError("APCA_API_BASE_URL no admite query ni fragmento")
    if parsed.path not in {"", "/"}:
        raise ValidationError("APCA_API_BASE_URL no admite path extra")
    return f"https://{host}"


def sanitize_log_level(raw: str | None, default: str = "INFO") -> str:
    level = (raw or default).strip().upper()
    if level not in ALLOWED_LOG_LEVELS:
        raise ValidationError("LOG_LEVEL no permitido")
    return level


def sanitize_timeframe(raw: str | None, default: str = "1Day") -> str:
    value = (raw or default).strip()
    if value not in ALLOWED_TIMEFRAMES:
        raise ValidationError("BAR_TIMEFRAME no permitido")
    return value


def bounded_int(
    raw: str | None,
    default: int,
    *,
    min_value: int,
    max_value: int,
    name: str,
) -> int:
    if raw is None or str(raw).strip() == "":
        value = default
    else:
        text = str(raw).strip()
        if not re.fullmatch(r"-?\d+", text):
            raise ValidationError(f"{name} no es un entero valido")
        value = int(text)
    if value < min_value or value > max_value:
        raise ValidationError(f"{name} fuera de rango")
    return value


def bounded_float(
    raw: str | None,
    default: float,
    *,
    min_value: float,
    max_value: float,
    name: str,
) -> float:
    if raw is None or str(raw).strip() == "":
        value = default
    else:
        text = str(raw).strip()
        try:
            value = float(text)
        except ValueError as exc:
            raise ValidationError(f"{name} no es un numero valido") from None
        if not math.isfinite(value):
            raise ValidationError(f"{name} no es finito")
    if value < min_value or value > max_value:
        raise ValidationError(f"{name} fuera de rango")
    return value


def sanitize_filename(raw: str) -> str:
    name = str(raw).strip()
    if not _FILENAME_RE.match(name):
        raise ValidationError("Nombre de archivo rechazado")
    return name


def safe_path_under(base_dir: Path, filename: str) -> Path:
    """Impide path traversal al escribir bajo logs/."""
    base = base_dir.resolve()
    name = sanitize_filename(filename)
    path = (base / name).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValidationError("Ruta fuera del directorio permitido") from exc
    return path


def sanitize_qty(qty: float) -> float:
    if not math.isfinite(qty) or qty <= 0 or qty > 1_000_000:
        raise ValidationError("Cantidad de orden invalida")
    return float(qty)
