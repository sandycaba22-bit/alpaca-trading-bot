"""Feed compartido de operaciones (todos los bots) para panel web y respaldo."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from bot.config import PROJECT_ROOT

_LOCK = threading.Lock()
_MAX_LINES = 600


def feed_path() -> Path:
    return PROJECT_ROOT / "data" / "trade_alerts.jsonl"


def append_trade_alert(
    *,
    source: str,
    kind: str,
    symbol: str,
    headline: str,
    body: str = "",
    event_id: str | None = None,
) -> int:
    """Registra alerta; devuelve id monotónico (ms)."""
    alert_id = int(time.time() * 1000)
    row = {
        "id": alert_id,
        "ts": alert_id,
        "source": (source or "bot").strip()[:32],
        "kind": (kind or "info").strip()[:24],
        "symbol": (symbol or "").strip()[:24],
        "headline": headline.strip()[:240],
        "body": (body or "").strip()[:4000],
        "event_id": (event_id or "")[:120],
    }
    path = feed_path()
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        _trim_file(path)
    return alert_id


def _trim_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= _MAX_LINES:
        return
    tail = lines[-_MAX_LINES:]
    path.write_text("\n".join(tail) + "\n", encoding="utf-8")


def list_alerts(*, after_id: int = 0, limit: int = 80) -> list[dict]:
    path = feed_path()
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for raw in reversed(lines):
        if len(out) >= limit:
            break
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        aid = int(row.get("id") or 0)
        if aid <= after_id:
            continue
        out.append(row)
    out.reverse()
    return out
