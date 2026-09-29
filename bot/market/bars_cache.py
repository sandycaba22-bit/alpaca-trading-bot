"""Cache en disco de velas OHLC (backtest / research). No la usan los bots live."""

from __future__ import annotations

import json
import logging
import os
import pickle
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT
from bot.universe import ELITE_STOCK_SYMBOLS, TOP50_US_STOCK_SYMBOLS

logger = logging.getLogger(__name__)

DEFAULT_YEARS = 6
MANIFEST_NAME = "manifest.json"

# Perfil → símbolos + TFs que usa cada bot / sweep habitual
WARM_PRESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "crypto_night": {
        "symbols": ("BTC/USD", "ETH/USD"),
        "timeframes": ("15Min", "1Hour", "4Hour", "1Day"),
    },
    "stocks_elite": {
        "symbols": ELITE_STOCK_SYMBOLS,
        "timeframes": ("5Min", "15Min", "1Hour", "4Hour", "1Day"),
    },
    "stocks_top50": {
        "symbols": TOP50_US_STOCK_SYMBOLS,
        "timeframes": ("5Min", "15Min", "1Hour"),
    },
}


def cache_dir() -> Path:
    raw = os.getenv("BARS_CACHE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return PROJECT_ROOT / "data" / "bars_cache"


def symbol_cache_key(symbol: str) -> str:
    return symbol.replace("/", "-").strip().upper()


def cache_path(symbol: str, timeframe: str) -> Path:
    return cache_dir() / f"{symbol_cache_key(symbol)}_{timeframe}.pkl"


def _expected_columns() -> list[str]:
    return ["open", "high", "low", "close", "volume"]


def normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=_expected_columns())
    out = frame.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, utc=True)
    elif out.index.tz is None:
        out.index = out.index.tz_localize(timezone.utc)
    cols = [c for c in _expected_columns() if c in out.columns]
    out = out[cols].sort_index()
    return out[~out.index.duplicated(keep="first")]


def load_bars(symbol: str, timeframe: str) -> pd.DataFrame | None:
    path = cache_path(symbol, timeframe)
    if not path.is_file():
        return None
    try:
        raw = pickle.loads(path.read_bytes())
    except Exception as exc:
        logger.warning("cache corrupto %s: %s", path, exc)
        return None
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None
    return normalize_bars(raw)


def save_bars(symbol: str, timeframe: str, bars: pd.DataFrame) -> Path:
    path = cache_path(symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = normalize_bars(bars)
    path.write_bytes(pickle.dumps(frame, protocol=pickle.HIGHEST_PROTOCOL))
    return path


def range_for_years(years: float, *, end: datetime | None = None) -> tuple[datetime, datetime]:
    finish = end or datetime.now(timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    start = finish - timedelta(days=int(float(years) * 365.25))
    return start, finish


def load_or_fetch(
    market: MarketDataService,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    force: bool = False,
) -> pd.DataFrame:
    """Lee .pkl local o descarga rango desde Alpaca y guarda."""
    if not force:
        cached = load_bars(symbol, timeframe)
        if cached is not None and not cached.empty:
            logger.info(
                "cache hit %s %s | bars=%s | %s -> %s",
                symbol,
                timeframe,
                len(cached),
                cached.index.min(),
                cached.index.max(),
            )
            return cached

    logger.info("fetch %s %s | %s -> %s", symbol, timeframe, start.date(), end.date())
    bars = market.get_bars_range(symbol, timeframe, start=start, end=end)
    bars = normalize_bars(bars)
    if not bars.empty:
        save_bars(symbol, timeframe, bars)
        _manifest_update(symbol, timeframe, bars)
    return bars


def _manifest_path() -> Path:
    return cache_dir() / MANIFEST_NAME


def _manifest_update(symbol: str, timeframe: str, bars: pd.DataFrame) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    key = f"{symbol_cache_key(symbol)}_{timeframe}"
    data[key] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": int(len(bars)),
        "start": bars.index.min().isoformat() if not bars.empty else None,
        "end": bars.index.max().isoformat() if not bars.empty else None,
        "file": cache_path(symbol, timeframe).name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def expand_presets(names: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    symbols: set[str] = set()
    tfs: set[str] = set()
    for name in names:
        key = name.strip().lower()
        if key == "all":
            for preset in WARM_PRESETS.values():
                symbols.update(preset["symbols"])
                tfs.update(preset["timeframes"])
            continue
        preset = WARM_PRESETS.get(key)
        if preset is None:
            raise ValueError(f"preset desconocido: {name!r} ({', '.join(WARM_PRESETS)} | all)")
        symbols.update(preset["symbols"])
        tfs.update(preset["timeframes"])
    return tuple(sorted(symbols)), tuple(sorted(tfs, key=_tf_sort_key))


def _tf_sort_key(tf: str) -> tuple[int, str]:
    order = {
        "1Min": 1,
        "3Min": 2,
        "5Min": 3,
        "6Min": 4,
        "9Min": 5,
        "15Min": 6,
        "30Min": 7,
        "1Hour": 8,
        "4Hour": 9,
        "1Day": 10,
    }
    return (order.get(tf, 99), tf)


def warm_many(
    market: MarketDataService,
    symbols: tuple[str, ...],
    timeframes: tuple[str, ...],
    *,
    years: float = DEFAULT_YEARS,
    force: bool = False,
    pause_sec: float = 0.15,
) -> list[tuple[str, str, int]]:
    """Descarga (o reutiliza) cada par símbolo×TF. Devuelve filas (symbol, tf, bar_count)."""
    start, end = range_for_years(years)
    results: list[tuple[str, str, int]] = []
    total = len(symbols) * len(timeframes)
    n = 0
    for symbol in symbols:
        for tf in timeframes:
            n += 1
            print(f"[{n}/{total}] {symbol} {tf} ...", flush=True)
            bars = load_or_fetch(market, symbol, tf, start, end, force=force)
            results.append((symbol, tf, len(bars)))
            if pause_sec > 0:
                time.sleep(pause_sec)
    return results
