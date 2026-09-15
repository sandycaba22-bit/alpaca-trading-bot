"""Validación y sanitización de entradas (inyección, path traversal, SSRF)."""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlparse

from bot.security.exceptions import ValidationError

# Tickers Alpaca: AAPL, BRK.B. Cripto: BTC/USD, ETH/USD.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_CRYPTO_SYMBOL_RE = re.compile(r"^[A-Z]{2,10}/USD$")
_UNSAFE_SYMBOL = re.compile(r"[^A-Z0-9.]|\.\.|^\.|\.$")
_CRLF = re.compile(r"[\r\n\x00\x1b]")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

ALLOWED_API_HOSTS = frozenset(
    {
        "paper-api.alpaca.markets",
        "api.alpaca.markets",
    }
)
ALLOWED_TIMEFRAMES = frozenset({"1Min", "3Min", "5Min", "6Min", "9Min", "15Min", "1Hour", "1Day"})
ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
MAX_SYMBOLS = 8
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{20,120}$")
_TELEGRAM_CHAT_RE = re.compile(r"^-?\d{5,20}$")


def sanitize_log_text(value: str, max_len: int = 500) -> str:
    """Elimina CRLF/ANSI para evitar inyección en logs."""
    cleaned = _CRLF.sub(" ", str(value))
    cleaned = "".join(ch if 32 <= ord(ch) <= 126 or ord(ch) >= 160 else " " for ch in cleaned)
    return cleaned[:max_len]


def sanitize_symbol(raw: str) -> str:
    if raw is None:
        raise ValidationError("Simbolo vacio")
    symbol = str(raw).strip().upper()
    if "/" in symbol:
        if not _CRYPTO_SYMBOL_RE.match(symbol):
            raise ValidationError("Simbolo cripto rechazado: use formato BTC/USD")
        return symbol
    if not symbol or _UNSAFE_SYMBOL.search(symbol) or not _SYMBOL_RE.match(symbol):
        raise ValidationError("Simbolo rechazado: formato invalido")
    return symbol


def sanitize_crypto_symbols(raw: str | list[str] | None, default: list[str] | None = None) -> list[str]:
    if raw is None or raw == "":
        values = list(default or [])
    elif isinstance(raw, str):
        values = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        values = list(raw)

    symbols = [sanitize_symbol(item) for item in values if "/" in str(item)]
    if not symbols:
        raise ValidationError("CRYPTO_SYMBOLS no puede estar vacio")
    if len(symbols) > MAX_SYMBOLS:
        raise ValidationError(f"Demasiados simbolos cripto (max {MAX_SYMBOLS})")
    if len(set(symbols)) != len(symbols):
        raise ValidationError("CRYPTO_SYMBOLS contiene duplicados")
    return symbols


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


def parse_symbol_float_map(
    raw: str | None,
    *,
    min_value: float,
    max_value: float,
    name: str,
) -> dict[str, float]:
    """Parsea 'AAPL:18,MSFT:22,BTC/USD:25' a umbrales por símbolo."""
    if raw is None or not str(raw).strip():
        return {}
    out: dict[str, float] = {}
    for part in str(raw).split(","):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValidationError(f"{name} debe ser SIMBOLO:valor (recibido: {item})")
        symbol_raw, value_raw = item.rsplit(":", 1)
        symbol = sanitize_symbol(symbol_raw.strip())
        out[symbol] = bounded_float(
            value_raw.strip(),
            min_value,
            min_value=min_value,
            max_value=max_value,
            name=f"{name}:{symbol}",
        )
    return out


def parse_symbol_int_map(
    raw: str | None,
    *,
    min_value: int,
    max_value: int,
    name: str,
) -> dict[str, int]:
    """Parsea 'AAPL:15,MSFT:20,BTC/USD:24' a enteros por símbolo."""
    if raw is None or not str(raw).strip():
        return {}
    out: dict[str, int] = {}
    for part in str(raw).split(","):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValidationError(f"{name} debe ser SIMBOLO:valor (recibido: {item})")
        symbol_raw, value_raw = item.rsplit(":", 1)
        symbol = sanitize_symbol(symbol_raw.strip())
        out[symbol] = bounded_int(
            value_raw.strip(),
            min_value,
            min_value=min_value,
            max_value=max_value,
            name=f"{name}:{symbol}",
        )
    return out


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


def floor_fractional_qty(qty: float | str, *, decimals: int = 6) -> float:
    """
    Redondea hacia abajo a N decimales (nunca hacia arriba).

    Crítico en ventas crypto: round() puede pedir 0.768566 cuando el broker
    solo tiene 0.768565825 → Alpaca 40301000 insufficient balance.
    """
    raw = Decimal(str(qty).strip())
    if raw <= 0:
        return 0.0
    quant = Decimal(1).scaleb(-int(decimals))
    floored = raw.quantize(quant, rounding=ROUND_DOWN)
    return float(floored)


def sanitize_qty(
    qty: float,
    *,
    fractional: bool = False,
    mode: str = "round",
) -> float:
    """
    mode:
      - round: compras / sizing (comportamiento histórico)
      - floor: cierres / ventas — nunca superar el disponible en el broker
    """
    if not math.isfinite(qty) or qty <= 0 or qty > 1_000_000:
        raise ValidationError("Cantidad de orden invalida")
    if fractional:
        if str(mode).lower() == "floor":
            floored = floor_fractional_qty(qty, decimals=6)
            if floored <= 0:
                raise ValidationError("Cantidad de orden invalida")
            return floored
        return round(float(qty), 6)
    return float(qty)


def _is_telegram_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        not lowered
        or lowered.startswith("your_")
        or lowered.startswith("pega_aqui")
        or lowered.startswith("change_me")
    )


def sanitize_telegram_token(raw: str | None) -> str:
    if raw is None or _is_telegram_placeholder(str(raw)):
        return ""
    token = str(raw).strip()
    if not _TELEGRAM_TOKEN_RE.fullmatch(token):
        raise ValidationError("TELEGRAM_BOT_TOKEN formato invalido")
    return token


def sanitize_telegram_chat_id(raw: str | None) -> str:
    if raw is None or _is_telegram_placeholder(str(raw)):
        return ""
    chat_id = str(raw).strip()
    if not _TELEGRAM_CHAT_RE.fullmatch(chat_id):
        raise ValidationError("TELEGRAM_CHAT_ID formato invalido")
    return chat_id
