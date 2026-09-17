"""Estado persistente del TP dinámico por ATR (órden límite + gap)."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from bot.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_PATH = PROJECT_ROOT / "data" / "dynamic_tp_state.json"


@dataclass
class DynamicTpRow:
    symbol: str
    entry_price: float
    tp_price: float
    tp_pct: float
    order_id: str
    order_qty: float
    filled_qty: float = 0.0
    gap_partial_done: bool = False
    atr_value: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> DynamicTpRow:
        return cls(
            symbol=str(data.get("symbol", "")).upper(),
            entry_price=float(data.get("entry_price", 0.0)),
            tp_price=float(data.get("tp_price", 0.0)),
            tp_pct=float(data.get("tp_pct", 0.0)),
            order_id=str(data.get("order_id", "")),
            order_qty=float(data.get("order_qty", 0.0)),
            filled_qty=float(data.get("filled_qty", 0.0) or 0.0),
            gap_partial_done=bool(data.get("gap_partial_done", False)),
            atr_value=(
                float(data["atr_value"])
                if data.get("atr_value") not in (None, "")
                else None
            ),
        )


class DynamicTpStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._rows: dict[str, DynamicTpRow] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._rows = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("dynamic_tp_state | no se pudo leer %s: %s", self.path, exc)
                self._rows = {}
                return
            rows: dict[str, DynamicTpRow] = {}
            for key, data in (raw or {}).items():
                if isinstance(data, dict):
                    row = DynamicTpRow.from_dict(data)
                    if row.symbol:
                        rows[row.symbol.upper()] = row
            self._rows = rows

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: asdict(v) for k, v in self._rows.items()}
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, symbol: str) -> DynamicTpRow | None:
        return self._rows.get(str(symbol).upper())

    def upsert(self, row: DynamicTpRow) -> None:
        key = str(row.symbol).upper()
        with self._lock:
            self._rows[key] = row
        self.save()

    def remove(self, symbol: str) -> None:
        key = str(symbol).upper()
        with self._lock:
            if key in self._rows:
                del self._rows[key]
        self.save()

    def list_rows(self) -> list[DynamicTpRow]:
        return list(self._rows.values())
