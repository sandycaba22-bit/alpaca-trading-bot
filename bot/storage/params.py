"""Parámetros de estrategia aprendidos en el walk-forward."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.market.assets import asset_class_for
from bot.runtime_paths import data_file


def _params_path() -> Path:
    return data_file("strategy_params.json")


@dataclass(frozen=True)
class SymbolParams:
    sma_fast: int
    sma_slow: int
    stop_loss_pct: float
    take_profit_pct: float
    atr_stop_mult: float
    asset_class: str = "stock"
    source: str = "default"
    train_score: float | None = None
    train_return_pct: float | None = None
    train_sharpe: float | None = None
    train_trades: int | None = None
    validate_return_pct: float | None = None
    validate_sharpe: float | None = None
    validate_trades: int | None = None
    train_start: str | None = None
    train_end: str | None = None
    validate_start: str | None = None
    validate_end: str | None = None


def load_symbol_params() -> dict[str, SymbolParams]:
    data = _read()
    symbols = data.get("symbols")
    if not isinstance(symbols, dict):
        return {}
    out: dict[str, SymbolParams] = {}
    for symbol, raw in symbols.items():
        if not isinstance(raw, dict):
            continue
        try:
            out[str(symbol).upper()] = SymbolParams(
                sma_fast=int(raw["sma_fast"]),
                sma_slow=int(raw["sma_slow"]),
                stop_loss_pct=float(raw["stop_loss_pct"]),
                take_profit_pct=float(raw["take_profit_pct"]),
                atr_stop_mult=float(raw.get("atr_stop_mult", 1.5)),
                asset_class=str(raw.get("asset_class", asset_class_for(str(symbol)))),
                source=str(raw.get("source", "optimized")),
                train_score=_opt_float(raw.get("train_score")),
                train_return_pct=_opt_float(raw.get("train_return_pct")),
                train_sharpe=_opt_float(raw.get("train_sharpe")),
                train_trades=_opt_int(raw.get("train_trades")),
                validate_return_pct=_opt_float(raw.get("validate_return_pct")),
                validate_sharpe=_opt_float(raw.get("validate_sharpe")),
                validate_trades=_opt_int(raw.get("validate_trades")),
                train_start=_opt_str(raw.get("train_start")),
                train_end=_opt_str(raw.get("train_end")),
                validate_start=_opt_str(raw.get("validate_start")),
                validate_end=_opt_str(raw.get("validate_end")),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_symbol_params(params: dict[str, SymbolParams]) -> Path:
    path = _params_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "symbols": {symbol: asdict(row) for symbol, row in params.items()},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def public_strategy_snapshot() -> dict:
    data = _read()
    symbols = {}
    for symbol, row in load_symbol_params().items():
        symbols[symbol] = {
            "sma_fast": row.sma_fast,
            "sma_slow": row.sma_slow,
            "stop_loss_pct": row.stop_loss_pct,
            "take_profit_pct": row.take_profit_pct,
            "asset_class": row.asset_class,
            "source": row.source,
            "train_return_pct": row.train_return_pct,
            "train_sharpe": row.train_sharpe,
            "validate_return_pct": row.validate_return_pct,
            "validate_sharpe": row.validate_sharpe,
            "validate_trades": row.validate_trades,
            "train_start": row.train_start,
            "validate_start": row.validate_start,
        }
    return {
        "generated_at": data.get("generated_at"),
        "symbols": symbols,
    }


def _read() -> dict:
    path = _params_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
