"""Capa de seguridad: validacion, redaccion, rate limit y auditoria."""

from .audit import audit
from .errors import log_caught
from .exceptions import RateLimitError, SecurityError, ValidationError
from .ratelimit import RateLimitConfig, RateLimiter
from .sanitize import (
    bounded_float,
    bounded_int,
    safe_path_under,
    sanitize_api_base_url,
    sanitize_qty,
    sanitize_symbol,
    sanitize_symbols,
)
from .secrets import RedactingFilter, mask_secret, register_secret

__all__ = [
    "RateLimitConfig",
    "RateLimiter",
    "RateLimitError",
    "RedactingFilter",
    "SecurityError",
    "ValidationError",
    "audit",
    "bounded_float",
    "bounded_int",
    "log_caught",
    "mask_secret",
    "register_secret",
    "safe_path_under",
    "sanitize_api_base_url",
    "sanitize_qty",
    "sanitize_symbol",
    "sanitize_symbols",
]
