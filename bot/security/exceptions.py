"""Errores de seguridad: mensajes públicos, sin secretos ni payloads."""

from __future__ import annotations


class SecurityError(Exception):
    """Fallo de política de seguridad. El mensaje es seguro para logs/UI."""


class ValidationError(SecurityError):
    """Entrada rechazada por formato, rango o allowlist."""


class RateLimitError(SecurityError):
    """Demasiadas llamadas en la ventana de tiempo."""
