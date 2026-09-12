"""Descarga de barras históricas y tape en tiempo real."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.alpaca.client import AlpacaClient
from bot.security.errors import log_caught
from bot.security.exceptions import RateLimitError, ValidationError
from bot.market.assets import is_crypto_symbol
from bot.security.sanitize import sanitize_symbol

logger = logging.getLogger(__name__)


def _by_symbol(payload: object, symbol: str):
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get(symbol)
    try:
        return payload[symbol]  # type: ignore[index]
    except Exception:
        return None

_TIMEFRAMES: dict[str, TimeFrame] = {
    "1Min": TimeFrame.Minute,
    "3Min": TimeFrame(3, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "6Min": TimeFrame(6, TimeFrameUnit.Minute),
    "9Min": TimeFrame(9, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame.Hour,
    "1Day": TimeFrame.Day,
}


@dataclass(frozen=True)
class LiveTape:
    """Último trade + spread bid/ask para decidir con el flujo actual."""

    symbol: str
    last_price: float
    bid: float | None
    ask: float | None
    spread_pct: float | None
    source: str


def parse_timeframe(value: str) -> TimeFrame:
    try:
        return _TIMEFRAMES[value]
    except KeyError as exc:
        raise ValidationError("BAR_TIMEFRAME no permitido") from exc


class MarketDataService:
    def __init__(self, client: AlpacaClient, feed: DataFeed = DataFeed.IEX) -> None:
        self.client = client
        self.feed = feed

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        lookback_bars: int,
    ) -> pd.DataFrame:
        symbol = sanitize_symbol(symbol)
        tf = parse_timeframe(timeframe)
        start = self._start_for_lookback(tf, lookback_bars, crypto=is_crypto_symbol(symbol))
        if is_crypto_symbol(symbol):
            return self._fetch_crypto_bars(symbol, tf, start=start)
        return self._fetch_stock_bars(symbol, tf, start=start)

    def get_bars_range(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        tf = parse_timeframe(timeframe)
        symbol = sanitize_symbol(symbol)
        finish = end or datetime.now(timezone.utc)
        cursor = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        if finish.tzinfo is None:
            finish = finish.replace(tzinfo=timezone.utc)

        fetch = self._fetch_crypto_bars if is_crypto_symbol(symbol) else self._fetch_stock_bars
        frames: list[pd.DataFrame] = []
        while cursor < finish:
            chunk = fetch(
                symbol,
                tf,
                start=cursor,
                end=finish,
                adjustment=Adjustment.ALL if not is_crypto_symbol(symbol) else None,
                limit=10_000,
            )
            if chunk.empty:
                break
            frames.append(chunk)
            last = chunk.index[-1].to_pydatetime()
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            nxt = last + timedelta(seconds=1)
            if nxt <= cursor or len(chunk) < 10_000:
                break
            cursor = nxt

        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        bars = pd.concat(frames).sort_index()
        return bars[~bars.index.duplicated(keep="first")]

    def get_live_tape(self, symbol: str, fallback_price: float | None = None) -> LiveTape | None:
        """Prioriza quote+trade; cripto opera 24/7."""
        symbol = sanitize_symbol(symbol)
        if is_crypto_symbol(symbol):
            return self._get_crypto_tape(symbol, fallback_price)
        return self._get_stock_tape(symbol, fallback_price)

    def _get_stock_tape(self, symbol: str, fallback_price: float | None = None) -> LiveTape | None:
        bid = ask = spread = None
        last_price: float | None = None
        source = "none"

        try:
            self.client.limiter.acquire("data")
            quote_req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.feed)
            quotes = self.client.data.get_stock_latest_quote(quote_req)
            quote = _by_symbol(quotes, symbol)
            if quote is not None:
                bid = float(quote.bid_price or 0) or None
                ask = float(quote.ask_price or 0) or None
                if bid and ask and ask > 0:
                    spread = (ask - bid) / ask
                    last_price = (bid + ask) / 2.0
                    source = "quote"
        except RateLimitError:
            raise
        except Exception:
            logger.debug("Sin quote en vivo para %s", symbol)

        try:
            self.client.limiter.acquire("data")
            trade_req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.feed)
            trades = self.client.data.get_stock_latest_trade(trade_req)
            trade = _by_symbol(trades, symbol)
            if trade is not None and float(trade.price) > 0:
                last_price = float(trade.price)
                source = "trade" if source == "none" else f"{source}+trade"
        except RateLimitError:
            raise
        except Exception:
            logger.debug("Sin trade en vivo para %s", symbol)

        if last_price is None and fallback_price and fallback_price > 0:
            last_price = fallback_price
            source = "bar_close"

        if last_price is None:
            logger.warning("Sin tape para %s", symbol)
            return None

        return LiveTape(
            symbol=symbol,
            last_price=last_price,
            bid=bid,
            ask=ask,
            spread_pct=spread,
            source=source,
        )

    def _get_crypto_tape(self, symbol: str, fallback_price: float | None = None) -> LiveTape | None:
        bid = ask = spread = None
        last_price: float | None = None
        source = "none"

        try:
            self.client.limiter.acquire("data")
            quote_req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.client.crypto_data.get_crypto_latest_quote(quote_req)
            quote = _by_symbol(quotes, symbol)
            if quote is not None:
                bid = float(quote.bid_price or 0) or None
                ask = float(quote.ask_price or 0) or None
                if bid and ask and ask > 0:
                    spread = (ask - bid) / ask
                    last_price = (bid + ask) / 2.0
                    source = "quote"
        except RateLimitError:
            raise
        except Exception:
            logger.debug("Sin quote cripto para %s", symbol)

        try:
            self.client.limiter.acquire("data")
            trade_req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            trades = self.client.crypto_data.get_crypto_latest_trade(trade_req)
            trade = _by_symbol(trades, symbol)
            if trade is not None and float(trade.price) > 0:
                last_price = float(trade.price)
                source = "trade" if source == "none" else f"{source}+trade"
        except RateLimitError:
            raise
        except Exception:
            logger.debug("Sin trade cripto para %s", symbol)

        if last_price is None and fallback_price and fallback_price > 0:
            last_price = fallback_price
            source = "bar_close"

        if last_price is None:
            logger.warning("Sin tape cripto para %s", symbol)
            return None

        return LiveTape(
            symbol=symbol,
            last_price=last_price,
            bid=bid,
            ask=ask,
            spread_pct=spread,
            source=source,
        )

    def _fetch_stock_bars(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
        adjustment: Adjustment | None = Adjustment.SPLIT,
        limit: int = 10_000,
    ) -> pd.DataFrame:
        adj = adjustment or Adjustment.SPLIT
        self.client.limiter.acquire("data")
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                feed=self.feed,
                adjustment=adj,
                limit=limit,
            )
            bars = self.client.data.get_stock_bars(request)
        except RateLimitError:
            raise
        except Exception as exc:
            log_caught(logger, "bars_fetch_failed", exc, symbol=symbol)
            raise RuntimeError("No se pudieron obtener barras de mercado.") from None
        df = bars.df
        if df is None or df.empty:
            logger.debug("Sin barras para %s (%s)", symbol, timeframe)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")

        df = df.sort_index()
        logger.debug("Barras %s: %s filas desde %s", symbol, len(df), df.index.min())
        return df

    def _fetch_crypto_bars(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
        adjustment: Adjustment | None = None,
        limit: int = 10_000,
    ) -> pd.DataFrame:
        del adjustment
        self.client.limiter.acquire("data")
        try:
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                limit=limit,
            )
            bars = self.client.crypto_data.get_crypto_bars(request)
        except RateLimitError:
            raise
        except Exception as exc:
            log_caught(logger, "crypto_bars_fetch_failed", exc, symbol=symbol)
            raise RuntimeError("No se pudieron obtener barras cripto.") from None
        df = bars.df
        if df is None or df.empty:
            logger.debug("Sin barras cripto para %s (%s)", symbol, timeframe)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")

        df = df.sort_index()
        logger.debug("Barras cripto %s: %s filas desde %s", symbol, len(df), df.index.min())
        return df

    @staticmethod
    def _start_for_lookback(timeframe: TimeFrame, lookback_bars: int, *, crypto: bool = False) -> datetime:
        """Margen extra para fines de semana y huecos de mercado."""
        unit = timeframe.unit
        amount = timeframe.amount * lookback_bars
        if unit == TimeFrameUnit.Minute:
            multiplier = 2 if crypto else 4
            delta = timedelta(minutes=amount * multiplier)
        elif unit == TimeFrameUnit.Hour:
            delta = timedelta(hours=amount * (2 if crypto else 4))
        else:
            delta = timedelta(days=max(amount * 3, 30))
        return datetime.now(timezone.utc) - delta
