"""Manejo de errores sin filtrar secretos ni respuestas crudas de la API."""

from __future__ import annotations

import logging

from bot.security.audit import audit
from bot.security.exceptions import RateLimitError, SecurityError, ValidationError
from bot.security.secrets import public_exception_message, redact_text

logger = logging.getLogger(__name__)


def log_caught(log: logging.Logger, event: str, exc: BaseException, **fields: object) -> None:
    """Log publico corto + auditoria. No usa logger.exception (evita traceback en consola)."""
    public = public_exception_message(exc)
    log.error("%s: %s", event, public)
    audit(
        event,
        "error",
        error_type=type(exc).__name__,
        detail=redact_text(str(exc))[:200],
        **fields,
    )


def is_security_error(exc: BaseException) -> bool:
    return isinstance(exc, (SecurityError, ValidationError, RateLimitError))
