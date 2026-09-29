"""Candado 1 — sesión US close → open (NY/ET)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

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


def in_night_trading_window(dt: datetime) -> bool:
    """Ventana permitida: US close → US open, sin viernes noche ni domingo/lunes pre-open."""
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


def entries_allowed(dt: datetime) -> bool:
    if not in_night_trading_window(dt):
        return False
    et = _to_et(dt)
    if et.weekday() < 5 and et.time() >= ENTRY_CUTOFF and et.time() < US_OPEN:
        return False
    if et.weekday() == 0 and et.time() >= ENTRY_CUTOFF and et.time() < US_OPEN:
        return False
    return True


def session_label(dt: datetime) -> str:
    et = _to_et(dt)
    return et.strftime("%Y-%m-%d %H:%M ET")
