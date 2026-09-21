"""Diario de operaciones en SQLite (compras, ventas y P&L realizado)."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.runtime_paths import data_file


@dataclass(frozen=True)
class TradeRecord:
    id: int
    created_at: str
    symbol: str
    side: str
    qty: float
    price: float
    pnl_abs: float | None
    pnl_pct: float | None
    reason: str
    dry_run: bool


class TradeJournal:
    """Guarda cada fill paper en un archivo local. No almacena secretos."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or data_file("trades.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    pnl_abs REAL,
                    pnl_pct REAL,
                    reason TEXT NOT NULL DEFAULT '',
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    paper INTEGER NOT NULL DEFAULT 1,
                    order_id TEXT
                )
                """
            )
            conn.commit()

    def record(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        *,
        entry_price: float | None = None,
        reason: str = "",
        dry_run: bool = False,
        order_id: str | None = None,
    ) -> TradeRecord:
        side = side.lower()
        pnl_abs = pnl_pct = None
        if side == "sell" and entry_price and entry_price > 0:
            pnl_abs = (price - entry_price) * qty
            pnl_pct = (price / entry_price - 1.0) * 100.0
        created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    created_at, symbol, side, qty, price, pnl_abs, pnl_pct,
                    reason, dry_run, paper, order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    created,
                    symbol.upper(),
                    side,
                    float(qty),
                    float(price),
                    pnl_abs,
                    pnl_pct,
                    reason,
                    1 if dry_run else 0,
                    order_id,
                ),
            )
            row_id = int(cur.lastrowid)
            conn.commit()
        return TradeRecord(
            id=row_id,
            created_at=created,
            symbol=symbol.upper(),
            side=side,
            qty=float(qty),
            price=float(price),
            pnl_abs=pnl_abs,
            pnl_pct=pnl_pct,
            reason=reason,
            dry_run=dry_run,
        )

    def list_trades(self, limit: int = 200) -> list[TradeRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [self._row(r) for r in rows]

    def realized_loss_sum_on_utc_date(self, day: str) -> float:
        """Suma de pérdidas realizadas (pnl_abs < 0) en el día UTC `YYYY-MM-DD`."""
        prefix = str(day).strip()[:10]
        if not prefix:
            return 0.0
        with self._connect() as conn:
            value = conn.execute(
                """
                SELECT COALESCE(SUM(pnl_abs), 0) FROM trades
                WHERE pnl_abs IS NOT NULL AND pnl_abs < 0
                  AND created_at LIKE ?
                """,
                (f"{prefix}%",),
            ).fetchone()[0]
        return abs(float(value or 0.0))

    def realized_pnl(self) -> float:
        with self._connect() as conn:
            value = conn.execute(
                "SELECT COALESCE(SUM(pnl_abs), 0) FROM trades WHERE pnl_abs IS NOT NULL"
            ).fetchone()[0]
        return float(value or 0.0)

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    @staticmethod
    def _row(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=int(row["id"]),
            created_at=str(row["created_at"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            qty=float(row["qty"]),
            price=float(row["price"]),
            pnl_abs=None if row["pnl_abs"] is None else float(row["pnl_abs"]),
            pnl_pct=None if row["pnl_pct"] is None else float(row["pnl_pct"]),
            reason=str(row["reason"] or ""),
            dry_run=bool(row["dry_run"]),
        )
