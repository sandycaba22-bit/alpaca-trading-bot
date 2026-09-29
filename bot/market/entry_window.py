"""Ventana horaria ET para nuevas entradas (turnos élite / Top 50 en una sola paper)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.security.exceptions import ValidationError

_ET = ZoneInfo("America/New_York")
_WINDOW_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$"
)


@dataclass(frozen=True)
class StockEntryWindow:
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int

    @property
    def start_minutes(self) -> int:
        return self.start_hour * 60 + self.start_minute

    @property
    def end_minutes(self) -> int:
        return self.end_hour * 60 + self.end_minute

    def label(self) -> str:
        return (
            f"{self.start_hour:02d}:{self.start_minute:02d}-"
            f"{self.end_hour:02d}:{self.end_minute:02d} ET"
        )


def parse_stock_entry_window_et(raw: str | None) -> StockEntryWindow | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    match = _WINDOW_RE.match(text)
    if not match:
        raise ValidationError(
            "STOCK_ENTRY_WINDOW_ET debe ser HH:MM-HH:MM en hora New York "
            f"(ej. 09:30-13:30), recibido: {raw!r}"
        )
    sh, sm, eh, em = (int(match.group(i)) for i in range(1, 5))
    for h, m, name in ((sh, sm, "inicio"), (eh, em, "fin")):
        if h < 0 or h > 23 or m < 0 or m > 59:
            raise ValidationError(f"STOCK_ENTRY_WINDOW_ET {name} invalido: {h}:{m}")
    window = StockEntryWindow(sh, sm, eh, em)
    if window.start_minutes >= window.end_minutes:
        raise ValidationError(
            "STOCK_ENTRY_WINDOW_ET: la hora de inicio debe ser anterior al fin "
            f"({window.label()})"
        )
    return window


def entry_window_allows(
    window: StockEntryWindow | None,
    *,
    now: datetime | None = None,
) -> bool:
    if window is None:
        return True
    if now is None:
        now = datetime.now(_ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    else:
        now = now.astimezone(_ET)
    minutes = now.hour * 60 + now.minute
    return window.start_minutes <= minutes < window.end_minutes
