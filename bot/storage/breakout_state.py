"""Estado de rupturas: posiciones abiertas por breakout y cooldown anti-whipsaw."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from bot.runtime_paths import data_file

logger = logging.getLogger(__name__)


class BreakoutStateStore:
    def __init__(self, path: Path | None = None, *, persist: bool = True) -> None:
        self.path = path or data_file("breakout_state.json")
        self.persist = persist
        self._open: set[str] = set()
        self._cooldowns: dict[str, dict] = {}
        if persist:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            opens = raw.get("open", [])
            if isinstance(opens, list):
                self._open = {str(s).upper() for s in opens}
            cds = raw.get("cooldowns", {})
            if isinstance(cds, dict):
                self._cooldowns = {
                    str(sym).upper(): row
                    for sym, row in cds.items()
                    if isinstance(row, dict)
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("No se pudo cargar estado de ruptura: %s", type(exc).__name__)

    def save(self) -> None:
        if not self.persist:
            return
        payload = {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "open": sorted(self._open),
            "cooldowns": self._cooldowns,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def mark_open(self, symbol: str) -> None:
        key = str(symbol).upper()
        self._open.add(key)
        self.save()

    def clear_open(self, symbol: str) -> None:
        key = str(symbol).upper()
        if key in self._open:
            self._open.discard(key)
            self.save()

    def is_open_breakout(self, symbol: str) -> bool:
        return str(symbol).upper() in self._open

    def arm_cooldown_if_breakout(
        self,
        symbol: str,
        bars_required: int,
        bar_time: str | None = None,
    ) -> bool:
        """Si la posición salió de una ruptura, arranca cooldown. True si se activó."""
        key = str(symbol).upper()
        if key not in self._open:
            return False
        self._open.discard(key)
        stamp = bar_time or datetime.now(timezone.utc).isoformat()
        self._cooldowns[key] = {
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "bar_time": stamp,
            "bars_required": int(max(1, bars_required)),
        }
        self.save()
        logger.info(
            "%s | cooldown ruptura | %s velas tras stop loss (desde %s)",
            key,
            bars_required,
            stamp,
        )
        return True

    def remaining_bars(self, symbol: str, bars: pd.DataFrame | None) -> int:
        key = str(symbol).upper()
        row = self._cooldowns.get(key)
        if not row:
            return 0
        required = int(row.get("bars_required") or 0)
        if required <= 0:
            self._cooldowns.pop(key, None)
            self.save()
            return 0
        elapsed = self._elapsed_bars(row.get("bar_time"), bars)
        left = max(0, required - elapsed)
        if left == 0:
            self._cooldowns.pop(key, None)
            self.save()
            logger.info("%s | cooldown ruptura terminado", key)
        return left

    @staticmethod
    def _elapsed_bars(bar_time: object, bars: pd.DataFrame | None) -> int:
        if bars is None or bars.empty:
            return 0
        try:
            start = pd.Timestamp(str(bar_time))
            if start.tzinfo is None and getattr(bars.index, "tz", None) is not None:
                start = start.tz_localize(bars.index.tz)
            elif start.tzinfo is not None and getattr(bars.index, "tz", None) is None:
                start = start.tz_localize(None)
        except (TypeError, ValueError):
            return 0
        try:
            return int((bars.index > start).sum())
        except (TypeError, ValueError):
            return 0
