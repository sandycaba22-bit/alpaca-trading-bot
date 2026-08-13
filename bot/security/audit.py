"""Registro de auditoria de eventos de seguridad (archivo dedicado)."""

from __future__ import annotations

import logging
from typing import Any

from bot.security.sanitize import sanitize_log_text
from bot.security.secrets import redact_text

audit_logger = logging.getLogger("bot.security.audit")


def audit(event: str, outcome: str, **fields: Any) -> None:
    """Escribe una linea estructurada. Nunca incluir secretos en fields."""
    parts = [f"event={sanitize_log_text(event, 64)}", f"outcome={sanitize_log_text(outcome, 32)}"]
    for key, value in fields.items():
        if value is None:
            continue
        safe_key = sanitize_log_text(str(key), 32).replace(" ", "_")
        safe_val = sanitize_log_text(redact_text(str(value)), 120)
        parts.append(f"{safe_key}={safe_val}")
    audit_logger.info(" ".join(parts))
