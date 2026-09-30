"""Candado 1 — sesión US close → open (NY/ET) o 24/7 (`CRYPTO_NIGHT_SESSION_MODE=always`)."""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_ALWAYS_MODES = frozenset({"always", "24_7", "247", "continuous"})


def crypto_session_mode(raw: str | None = None) -> str:
    mode = (raw if raw is not None else os.getenv("CRYPTO_NIGHT_SESSION_MODE", "night")).strip().lower()
    return mode or "night"


def crypto_session_is_always(mode: str | None = None) -> bool:
    return crypto_session_mode(mode) in _ALWAYS_MODES

# Regular US equity session (NYSE)
US_OPEN = time(9, 30)
US_CLOSE = time(16, 0)
ENTRY_CUTOFF = time(9, 0)  # 30 min antes del open


def _to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ET)


def is_friday_night_off(dt: datetime) -> bool:
    """Viernes tras el cierre US → no operar esa noche."""
    et = _to_et(dt)
    return et.weekday() == 4 and et.time() >= US_CLOSE


def is_sunday_night_off(dt: datetime) -> bool:
    """Domingo (ventana pre-lunes) OFF."""
    et = _to_et(dt)
    return et.weekday() == 6


def is_us_regular_session(dt: datetime) -> bool:
    et = _to_et(dt)
    if et.weekday() >= 5:
        return False
    t = et.time()
    return US_OPEN <= t < US_CLOSE


def in_night_trading_window(dt: datetime, *, session_mode: str | None = None) -> bool:
    """Ventana permitida: US close → US open, sin viernes noche ni domingo/lunes pre-open."""
    if crypto_session_is_always(session_mode):
        return True
    et = _to_et(dt)
    if is_sunday_night_off(dt):
        return False
    if is_friday_night_off(dt):
        return False
    if is_us_regular_session(dt):
        return False
    wd, t = et.weekday(), et.time()
    if wd == 0 and t < US_OPEN:
        return False
    if wd in (0, 1, 2, 3) and t >= US_CLOSE:
        return True
    if wd in (1, 2, 3, 4) and t < ENTRY_CUTOFF:
        return True
    return False


def entries_allowed(
    dt: datetime,
    *,
    min_minutes_after_us_close: int = 0,
    session_mode: str | None = None,
) -> bool:
    if crypto_session_is_always(session_mode):
        return True
    if not in_night_trading_window(dt, session_mode=session_mode):
        return False
    et = _to_et(dt)
    if et.weekday() < 5 and et.time() >= ENTRY_CUTOFF and et.time() < US_OPEN:
        return False
    if et.weekday() == 0 and et.time() >= ENTRY_CUTOFF and et.time() < US_OPEN:
        return False
    delay = max(0, int(min_minutes_after_us_close))
    if delay > 0 and et.weekday() < 5 and et.time() >= US_CLOSE:
        close_at = et.replace(hour=US_CLOSE.hour, minute=US_CLOSE.minute, second=0, microsecond=0)
        if et < close_at + timedelta(minutes=delay):
            return False
    return True


def session_label(dt: datetime) -> str:
    et = _to_et(dt)
    return et.strftime("%Y-%m-%d %H:%M ET")


def should_flatten_crypto_positions(dt: datetime, *, session_mode: str | None = None) -> bool:
    """Cierra cripto nocturna al abrir sesión US (no cruzar con acciones)."""
    if crypto_session_is_always(session_mode):
        return False
    return is_us_regular_session(dt)
