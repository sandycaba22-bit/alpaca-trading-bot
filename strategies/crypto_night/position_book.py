"""Estado de trades nocturnos (entrada pendiente / abierta / stop en broker)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class NightTradeRecord:
    symbol: str
    side: str
    variant: str
    qty: float
    qty_open: float
    entry_order_id: str
    stop_order_id: str = ""
    entry_price: float | None = None
    stop_price: float = 0.0
    runner_stop: float = 0.0
    entry_time_utc: str = ""
    partial_taken: bool = False
    best_r: float = 0.0
    status: str = "pending"  # pending | open | closed
    meta: dict[str, Any] = field(default_factory=dict)


def load_trades(path: Path) -> list[NightTradeRecord]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw if isinstance(raw, list) else raw.get("trades", [])
        out: list[NightTradeRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                NightTradeRecord(
                    symbol=str(row.get("symbol", "")),
                    side=str(row.get("side", "long")),
                    variant=str(row.get("variant", "")),
                    qty=float(row.get("qty", 0)),
                    qty_open=float(row.get("qty_open", row.get("qty", 0))),
                    entry_order_id=str(row.get("entry_order_id", "")),
                    stop_order_id=str(row.get("stop_order_id", "")),
                    entry_price=(
                        float(row["entry_price"])
                        if row.get("entry_price") is not None
                        else None
                    ),
                    stop_price=float(row.get("stop_price", 0)),
                    runner_stop=float(row.get("runner_stop", row.get("stop_price", 0))),
                    entry_time_utc=str(row.get("entry_time_utc", "")),
                    partial_taken=bool(row.get("partial_taken", False)),
                    best_r=float(row.get("best_r", 0)),
                    status=str(row.get("status", "pending")),
                    meta=dict(row.get("meta") or {}),
                )
            )
        return [t for t in out if t.symbol and t.status != "closed"]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def save_trades(path: Path, trades: list[NightTradeRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(t) for t in trades if t.status != "closed"]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
