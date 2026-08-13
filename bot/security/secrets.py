"""Redaccion de secretos en logs, excepciones y representaciones."""

from __future__ import annotations

import logging
import re
from typing import Any

_PATTERNS = (
    re.compile(r"(?i)(api[_-]?secret|secret[_-]?key|authorization|bearer)[\"'\s:=]+[^\s\"']+"),
    re.compile(r"\bPK[A-Z0-9]{12,}\b"),
    re.compile(r"\bAK[A-Z0-9]{12,}\b"),
    re.compile(r"(?i)(APCA_API_SECRET_KEY|APCA_API_KEY_ID)\s*=\s*\S+"),
)

_extra_secrets: list[str] = []


def register_secret(value: str | None) -> None:
    if value and len(value) >= 8 and value not in _extra_secrets:
        _extra_secrets.append(value)


def mask_secret(value: str | None) -> str:
    if not value:
        return "***"
    if len(value) <= 8:
        return "****"
    return f"{value[:2]}***{value[-4:]}"


def redact_text(text: str) -> str:
    redacted = str(text)
    for secret in _extra_secrets:
        if secret:
            redacted = redacted.replace(secret, mask_secret(secret))
    for pattern in _PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def public_exception_message(exc: BaseException) -> str:
    """Mensaje seguro para consola: tipo, sin cuerpo de API ni secretos."""
    name = type(exc).__name__
    if name in {"ValidationError", "SecurityError", "RateLimitError"}:
        return redact_text(str(exc))
    return f"Error interno ({name})"


class RedactingFilter(logging.Filter):
    """Filtra secretos en msg, args y exception text de cualquier handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = _redact_args(record.args)
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact_text(
                logging.Formatter().formatException(record.exc_info)
            )
        return True


def _redact_args(args: Any) -> Any:
    if isinstance(args, dict):
        return {k: redact_text(str(v)) if isinstance(v, str) else v for k, v in args.items()}
    if isinstance(args, tuple):
        return tuple(redact_text(a) if isinstance(a, str) else a for a in args)
    if isinstance(args, str):
        return redact_text(args)
    return args
