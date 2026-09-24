"""Barras OHLC de Alpaca + marcadores de compra/venta para el grafico."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService, resolve_stock_data_feed
from bot.config import load_settings
from bot.market.assets import is_crypto_symbol
from bot.runtime_paths import configure_runtime_paths
from bot.security.sanitize import sanitize_symbol, sanitize_timeframe
from bot.storage.journal import TradeJournal
from bot.strategy.sync_entry import closed_bars_only

_LOOKBACK = {
    "1Min": 240,
    "3Min": 200,
    "5Min": 200,
    "6Min": 200,
    "9Min": 200,
    "15Min": 180,
    "30Min": 180,
    "1Hour": 200,
    "1Day": 180,
}


def _unix(ts: pd.Timestamp) -> int:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp())


def _trade_unix(created_at: str) -> int | None:
    text = created_at.replace(" UTC", "").strip()
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def _snap_time(trade_ts: int, candle_times: list[int]) -> int | None:
    chosen = None
    for t in candle_times:
        if t <= trade_ts:
            chosen = t
        else:
            break
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description="OHLC + markers")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="15Min")
    args = parser.parse_args()

    symbol = sanitize_symbol(args.symbol)
    timeframe = sanitize_timeframe(args.timeframe, "15Min")
    settings = load_settings()
    configure_runtime_paths(settings)
    client = AlpacaClient(settings)
    feed = resolve_stock_data_feed(settings.stock_data_feed)
    market = MarketDataService(client, feed=feed)
    lookback = _LOOKBACK.get(timeframe, 180)
    bars_raw = market.get_bars(symbol, timeframe, lookback, skip_cache=True)
    now = datetime.now(timezone.utc)
    bars = closed_bars_only(bars_raw, timeframe, now)
    if bars.empty and not bars_raw.empty:
        bars = bars_raw.copy()

    crypto = is_crypto_symbol(symbol)
    bot_entry_tf = settings.crypto_bar_timeframe if crypto else settings.stock_entry_timeframe
    bot_regime_tf = settings.crypto_regime_timeframe if crypto else "9Min"
    bot_confirm_tf = settings.crypto_regime_timeframe if crypto else settings.confirm_higher_tf

    candles: list[dict] = []
    last_bar_utc = ""
    last_bar_local = ""
    if not bars.empty:
        for ts, row in bars.iterrows():
            candles.append(
                {
                    "time": _unix(pd.Timestamp(ts)),
                    "open": round(float(row["open"]), 4),
                    "high": round(float(row["high"]), 4),
                    "low": round(float(row["low"]), 4),
                    "close": round(float(row["close"]), 4),
                }
            )
        candles.sort(key=lambda item: item["time"])
        last_ts = pd.Timestamp(bars.index[-1])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        last_bar_utc = last_ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            from zoneinfo import ZoneInfo

            display_tz = ZoneInfo("UTC") if crypto else ZoneInfo("America/New_York")
            last_bar_local = last_ts.astimezone(display_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            last_bar_local = last_bar_utc

    times = [c["time"] for c in candles]
    markers: list[dict] = []
    journal = TradeJournal()
    for trade in journal.list_trades(500):
        if trade.symbol != symbol or trade.price <= 0:
            continue
        trade_ts = _trade_unix(trade.created_at)
        if trade_ts is None:
            continue
        candle_time = _snap_time(trade_ts, times) or trade_ts
        buy = trade.side == "buy"
        markers.append(
            {
                "time": candle_time,
                "position": "belowBar" if buy else "aboveBar",
                "color": "#22c55e" if buy else "#ef4444",
                "shape": "arrowUp" if buy else "arrowDown",
                "text": "COMPRA" if buy else "VENTA",
                "price": trade.price,
                "side": trade.side,
            }
        )
    markers.sort(key=lambda item: item["time"])

    json.dump(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": candles,
            "markers": markers,
            "meta": {
                "feed": getattr(feed, "value", str(feed)),
                "adjustment": "none" if crypto else "split",
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "last_bar_start_utc": last_bar_utc,
                "last_bar_start_local": last_bar_local,
                "chart_time_zone": "UTC" if crypto else "America/New_York",
                "bot_profile": settings.bot_profile,
                "sync_entry_enabled": bool(settings.sync_entry_enabled),
                "bot_entry_timeframe": bot_entry_tf,
                "bot_regime_timeframe": bot_regime_tf,
                "bot_confirm_timeframe": bot_confirm_tf,
                "candles_closed_only": True,
                "matches_bot_entry_tf": timeframe == bot_entry_tf,
                "compare_hint": (
                    "Velas cerradas = misma base que sync_entry. Acciones en hora NY. "
                    "Feed IEX vs SIP puede cambiar OHLC vs la app Alpaca."
                ),
            },
        },
        sys.stdout,
        ensure_ascii=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        json.dump({"error": "No se pudieron obtener las velas."}, sys.stdout)
        raise SystemExit(1)
