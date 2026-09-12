"""WebSocket Alpaca (trades/quotes) con reconexión, watchdog y fallback REST."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from alpaca.data.enums import CryptoFeed, DataFeed
from alpaca.data.live import CryptoDataStream, StockDataStream

logger = logging.getLogger(__name__)

FALLBACK_STALE_SECONDS = 120.0
TICK_EVAL_MIN_SECONDS = 0.15
SUPERVISOR_RECONNECT_SECONDS = 2.0
SUPERVISOR_RECONNECT_MAX_SECONDS = 60.0
NOTIFY_DEBOUNCE_SECONDS = 300.0
WS_PING_INTERVAL = 10.0
WS_PING_TIMEOUT = 180.0
QUIET_LOG_SECONDS = 60.0
SHUTDOWN_WAIT_SECONDS = 5.0


class StreamHealth:
    """Estado del stream: socket por feed vs fallback REST.

    Un feed conectado pero sin ticks (cinta IEX de noche, cripto en calma)
    NO es una caída. Fallback solo si el socket de un feed esperado está
    abajo más de ``stale_after`` segundos.
    """

    def __init__(self, stale_after: float = FALLBACK_STALE_SECONDS) -> None:
        self.stale_after = float(stale_after)
        self.started_at = time.monotonic()
        self.last_tick_at = 0.0
        self.last_tick_feed: dict[str, float] = {}
        self.last_disconnect_at: dict[str, float] = {}
        self.connected = False
        self.fallback = False
        self._expected_feeds: set[str] = set()
        self._connected_feeds: set[str] = set()
        self._lock = threading.Lock()

    def set_expected_feeds(self, feeds: list[str]) -> None:
        with self._lock:
            self._expected_feeds = {f for f in feeds if f}

    def note_connected(self, feed: str = "any") -> None:
        with self._lock:
            if feed:
                self._connected_feeds.add(feed)
            self.connected = bool(self._connected_feeds)

    def note_disconnected(self, feed: str | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            if feed:
                self._connected_feeds.discard(feed)
                self.last_disconnect_at[feed] = now
            else:
                self._connected_feeds.clear()
                for name in list(self._expected_feeds) or ["any"]:
                    self.last_disconnect_at[name] = now
            self.connected = bool(self._connected_feeds)

    def force_stale(self) -> None:
        """Marca el stream como mudo (pruebas / corte simulado)."""
        past = time.monotonic() - self.stale_after - 1.0
        with self._lock:
            self.started_at = past
            self.last_tick_at = past
            self._connected_feeds.clear()
            self.connected = False
            for feed in list(self.last_tick_feed) or ["any"]:
                self.last_tick_feed[feed] = past
                self.last_disconnect_at[feed] = past

    def note_tick(self, feed: str = "any") -> bool:
        """Registra un tick. Devuelve True si salimos del fallback REST."""
        now = time.monotonic()
        with self._lock:
            self.last_tick_at = now
            self.last_tick_feed[feed] = now
            if feed:
                self._connected_feeds.add(feed)
            self.connected = bool(self._connected_feeds)
            if not self.fallback:
                return False
            if self._any_expected_socket_down_unlocked(now):
                return False
            self.fallback = False
            return True

    def enter_fallback_if_stale(self) -> bool:
        """True la primera vez que un feed esperado tiene el socket abajo > stale_after."""
        now = time.monotonic()
        with self._lock:
            if now - self.started_at < self.stale_after:
                return False
            if not self._any_expected_socket_down_unlocked(now):
                return False
            if self.fallback:
                return False
            self.fallback = True
            return True

    def try_restore_if_healthy(self) -> bool:
        """True si estábamos en fallback y todos los feeds esperados ya tienen socket."""
        now = time.monotonic()
        with self._lock:
            if not self.fallback:
                return False
            if self._any_expected_socket_down_unlocked(now):
                return False
            self.fallback = False
            return True

    def in_fallback(self) -> bool:
        with self._lock:
            return self.fallback

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            quiet: dict[str, float] = {}
            for feed in self._expected_feeds or {"any"}:
                last = self.last_tick_feed.get(feed, 0.0)
                if last <= 0.0:
                    last = self.last_tick_at if self.last_tick_at > 0 else self.started_at
                quiet[feed] = now - last
            return {
                "connected_feeds": sorted(self._connected_feeds),
                "expected_feeds": sorted(self._expected_feeds),
                "fallback": self.fallback,
                "quiet_seconds": quiet,
            }

    def _any_expected_socket_down_unlocked(self, now: float) -> bool:
        feeds = self._expected_feeds or {"any"}
        for feed in feeds:
            if feed in self._connected_feeds:
                continue
            last = self.last_disconnect_at.get(feed, 0.0)
            if last <= 0.0:
                last = self.started_at
            if (now - last) >= self.stale_after:
                return True
        return False


class StreamAlertGate:
    """Agrupa caídas/recuperaciones cercanas para no spamear Telegram."""

    def __init__(
        self,
        debounce_seconds: float = NOTIFY_DEBOUNCE_SECONDS,
        on_fallback: Callable[[str], None] | None = None,
        on_restored: Callable[[], None] | None = None,
        on_unstable: Callable[[int, float, bool], None] | None = None,
    ) -> None:
        self.window = float(debounce_seconds)
        self._on_fallback = on_fallback
        self._on_restored = on_restored
        self._on_unstable = on_unstable
        self._lock = threading.Lock()
        self._drops = 0
        self._window_start = 0.0
        self._last_reason = ""
        self._sent_fallback = False
        self._pending_restore = False

    def notify_fallback(self, reason: str) -> None:
        emit_first = False
        with self._lock:
            now = time.monotonic()
            if self._window_start <= 0.0 or now - self._window_start >= self.window:
                self._window_start = now
                self._drops = 1
                self._last_reason = reason
                self._sent_fallback = True
                self._pending_restore = False
                emit_first = True
            else:
                self._drops += 1
                self._last_reason = reason
                self._pending_restore = False
        if emit_first:
            logger.warning("stream | alerta Telegram | caída inicial | %s", reason)
            if self._on_fallback:
                self._on_fallback(reason)
        else:
            logger.warning(
                "stream | alerta Telegram suprimida | caída #%s en ventana %.0fs | %s",
                self._drops,
                self.window,
                reason,
            )

    def notify_restored(self) -> None:
        emit_now = False
        with self._lock:
            now = time.monotonic()
            in_window = self._window_start > 0.0 and now - self._window_start < self.window
            if in_window or self._drops > 1:
                self._pending_restore = True
            elif self._sent_fallback:
                emit_now = True
                self._sent_fallback = False
                self._drops = 0
                self._window_start = 0.0
                self._pending_restore = False
            else:
                self._pending_restore = False
        if emit_now:
            if self._on_restored:
                self._on_restored()
        else:
            logger.info("stream | alerta Telegram suprimida | recuperado (debounce)")

    def poll(self, currently_healthy: bool) -> None:
        unstable_n = 0
        window = self.window
        recovered = currently_healthy
        emit_restore = False
        with self._lock:
            now = time.monotonic()
            if self._window_start <= 0.0 or now - self._window_start < self.window:
                return
            unstable_n = self._drops
            recovered = currently_healthy
            emit_restore = currently_healthy and self._sent_fallback and unstable_n <= 1
            self._drops = 0
            self._window_start = 0.0
            self._sent_fallback = False
            self._pending_restore = False
        if unstable_n > 1:
            logger.warning(
                "stream | conexion inestable | %s caidas en %.0fs | recuperado=%s",
                unstable_n,
                window,
                recovered,
            )
            if self._on_unstable:
                self._on_unstable(unstable_n, window, recovered)
            return
        if emit_restore and self._on_restored:
            self._on_restored()


class LiveMarketStream:
    """
    Dos sockets (stock IEX + crypto US). El SDK mantiene ping/pong nativo;
    el watchdog solo declara caída si el socket de un feed esperado está abajo.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        stock_symbols: list[str],
        crypto_symbols: list[str],
        on_tick: Callable[[str, float], None] | None = None,
        on_fallback: Callable[[str], None] | None = None,
        on_restored: Callable[[], None] | None = None,
        on_unstable: Callable[[int, float, bool], None] | None = None,
        stale_after: float = FALLBACK_STALE_SECONDS,
        data_timeout: float | None = None,
        ping_interval: float = WS_PING_INTERVAL,
        ping_timeout: float = WS_PING_TIMEOUT,
        notify_debounce: float = NOTIFY_DEBOUNCE_SECONDS,
        reconnect_min: float = SUPERVISOR_RECONNECT_SECONDS,
        reconnect_max: float = SUPERVISOR_RECONNECT_MAX_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._stock_symbols = [s.upper() for s in stock_symbols if s and "/" not in s]
        self._crypto_symbols = [s.upper() for s in crypto_symbols if s and "/" in s]
        self._on_tick = on_tick
        self._data_timeout = float(data_timeout) if data_timeout and data_timeout > 0 else None
        self._ping_interval = float(ping_interval)
        self._ping_timeout = float(ping_timeout)
        self._reconnect_min = max(1.0, float(reconnect_min))
        self._reconnect_max = max(self._reconnect_min, float(reconnect_max))
        self._alerts = StreamAlertGate(
            debounce_seconds=notify_debounce,
            on_fallback=on_fallback,
            on_restored=on_restored,
            on_unstable=on_unstable,
        )
        self.health = StreamHealth(stale_after=stale_after)
        self._should_run = False
        self._expect_stock = bool(self._stock_symbols)
        self._prices: dict[str, float] = {}
        self._quotes: dict[str, tuple[float, float]] = {}
        self._peaks: dict[str, float] = {}
        self._last_eval: dict[str, float] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._streams: list[Any] = []
        self._watchdog: threading.Thread | None = None
        self._eval_queue: queue.Queue[tuple[str, float]] = queue.Queue(maxsize=64)
        self._eval_thread: threading.Thread | None = None
        self._last_quiet_log = 0.0
        self._shutdown_done = False
        self._shutdown_lock = threading.Lock()
        self._sync_expected_feeds()

    def set_stock_expected(self, expected: bool) -> None:
        """Tras el cierre NYSE no exigimos ticks IEX (el cripto sigue 24/7)."""
        self._expect_stock = bool(expected) and bool(self._stock_symbols)
        self._sync_expected_feeds()

    def _sync_expected_feeds(self) -> None:
        feeds: list[str] = []
        if self._expect_stock and self._stock_symbols:
            feeds.append("stock")
        if self._crypto_symbols:
            feeds.append("crypto")
        self.health.set_expected_feeds(feeds)

    def last_price(self, symbol: str) -> float | None:
        key = str(symbol).upper()
        with self._lock:
            px = self._prices.get(key)
        return float(px) if px and px > 0 else None

    def last_quote(self, symbol: str) -> tuple[float | None, float | None]:
        key = str(symbol).upper()
        with self._lock:
            pair = self._quotes.get(key)
        if not pair:
            return None, None
        bid, ask = pair
        if bid and ask and bid > 0 and ask > 0:
            return float(bid), float(ask)
        return None, None

    def peak_price(self, symbol: str, tick: float) -> float:
        key = str(symbol).upper()
        with self._lock:
            prev = self._peaks.get(key, 0.0)
            peak = max(prev, float(tick) if tick > 0 else 0.0)
            self._peaks[key] = peak
            return peak

    def start(self) -> None:
        if self._should_run:
            return
        self._should_run = True
        self.health.started_at = time.monotonic()
        self._sync_expected_feeds()
        timeout_label = f"{self._data_timeout:.0f}s" if self._data_timeout else "off"
        logger.info(
            "stream | arranque | stocks=%s | crypto=%s | fallback_si_socket_abajo=%.0fs | "
            "data_timeout=%s | ping=%.0fs | debounce_telegram=%.0fs",
            ",".join(self._stock_symbols) or "—",
            ",".join(self._crypto_symbols) or "—",
            self.health.stale_after,
            timeout_label,
            self._ping_interval,
            self._alerts.window,
        )
        self._eval_thread = threading.Thread(
            target=self._eval_loop, name="alpaca-ws-eval", daemon=True
        )
        self._eval_thread.start()
        if self._stock_symbols:
            t = threading.Thread(target=self._stock_loop, name="alpaca-ws-stock", daemon=True)
            self._threads.append(t)
            t.start()
        if self._crypto_symbols:
            t = threading.Thread(target=self._crypto_loop, name="alpaca-ws-crypto", daemon=True)
            self._threads.append(t)
            t.start()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="alpaca-ws-watch", daemon=True)
        self._watchdog.start()

    def stop(self, timeout: float = SHUTDOWN_WAIT_SECONDS) -> bool:
        """Cierre limpio: señal al SDK, espera threads y confirma antes de salir."""
        with self._shutdown_lock:
            if self._shutdown_done:
                return True
            self._should_run = False
            wait = max(1.0, float(timeout))
            logger.info("stream | iniciando cierre limpio WS (timeout=%.0fs)", wait)
            self._close_active_streams(wait)
            self._join_worker_threads(wait)
            self._streams.clear()
            self.health.note_disconnected()
            alive = [t.name for t in self._threads if t.is_alive()]
            self._threads.clear()
            if alive:
                logger.warning(
                    "stream | WS shutdown incompleto — threads vivos: %s",
                    ",".join(alive),
                )
                return False
            self._shutdown_done = True
            logger.info("stream | WS cerrado limpiamente antes de shutdown")
            for handler in logging.root.handlers:
                try:
                    handler.flush()
                except Exception:
                    pass
            return True

    def _close_active_streams(self, timeout: float) -> None:
        per_stream = max(1.0, timeout / max(1, len(self._streams)))
        for stream in list(self._streams):
            try:
                stream._should_run = False
                loop = getattr(stream, "_loop", None)
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(stream.stop_ws(), loop).result(
                        timeout=per_stream
                    )
                else:
                    stream.stop()
            except Exception as exc:
                logger.warning(
                    "stream | error cerrando socket Alpaca: %s",
                    type(exc).__name__,
                )

    def _join_worker_threads(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for thread in list(self._threads):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        if self._watchdog is not None and self._watchdog.is_alive():
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                self._watchdog.join(timeout=remaining)
        if self._eval_thread is not None and self._eval_thread.is_alive():
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                self._eval_thread.join(timeout=remaining)

    def simulate_disconnect(self, reason: str = "simulated_cut") -> None:
        logger.warning("stream | corte simulado | %s", reason)
        for stream in list(self._streams):
            try:
                stream.stop()
            except Exception:
                pass
        self.health.force_stale()
        if self.health.enter_fallback_if_stale():
            logger.error("stream | fallback REST | %s", reason)
            self._emit_fallback(reason)

    def _emit_fallback(self, reason: str) -> None:
        self._alerts.notify_fallback(reason)

    def _emit_restored(self) -> None:
        self._alerts.notify_restored()

    def _eval_loop(self) -> None:
        while self._should_run:
            try:
                symbol, price = self._eval_queue.get(timeout=0.4)
            except queue.Empty:
                continue
            if not self._on_tick:
                continue
            try:
                self._on_tick(symbol, price)
            except Exception as exc:
                logger.warning("stream | on_tick %s: %s", symbol, type(exc).__name__)

    def _watchdog_loop(self) -> None:
        while self._should_run:
            try:
                if self.health.enter_fallback_if_stale():
                    snap = self.health.snapshot()
                    logger.error(
                        "stream | socket abajo %.0fs | feeds=%s | fallback REST activo",
                        self.health.stale_after,
                        snap.get("expected_feeds"),
                    )
                    self._emit_fallback(f"socket abajo {self.health.stale_after:.0f}s")
                elif self.health.try_restore_if_healthy():
                    logger.info("stream | sockets vivos | salida de fallback REST")
                    self._emit_restored()
                else:
                    self._log_quiet_if_needed()
                self._alerts.poll(currently_healthy=not self.health.in_fallback())
            except Exception as exc:
                logger.warning("stream | watchdog: %s", type(exc).__name__)
            time.sleep(2.0)

    def _log_quiet_if_needed(self) -> None:
        snap = self.health.snapshot()
        quiet = snap.get("quiet_seconds") or {}
        connected = set(snap.get("connected_feeds") or [])
        expected = set(snap.get("expected_feeds") or [])
        silent = [
            f"{feed}:{seconds:.0f}s"
            for feed, seconds in quiet.items()
            if feed in expected and feed in connected and seconds >= 30.0
        ]
        if not silent:
            return
        now = time.monotonic()
        if now - self._last_quiet_log < QUIET_LOG_SECONDS:
            return
        self._last_quiet_log = now
        logger.info(
            "stream | cinta en silencio %s | sockets vivos=%s — no es caida",
            ",".join(silent),
            ",".join(sorted(connected)) or "—",
        )

    def _websocket_params(self) -> dict[str, Any]:
        return {
            "ping_interval": self._ping_interval,
            "ping_timeout": self._ping_timeout,
            "max_queue": 1024,
        }

    def _stock_loop(self) -> None:
        failures = 0
        while self._should_run:
            stream = None
            try:
                logger.info("stream | conectando stock IEX | %s", ",".join(self._stock_symbols))
                kwargs: dict[str, Any] = {
                    "feed": DataFeed.IEX,
                    "websocket_params": self._websocket_params(),
                }
                if self._data_timeout is not None:
                    kwargs["data_timeout"] = self._data_timeout
                stream = StockDataStream(self._api_key, self._secret_key, **kwargs)
                self._hook_lifecycle(stream, "stock")
                stream.subscribe_trades(self._on_trade, *self._stock_symbols)
                stream.subscribe_quotes(self._on_quote, *self._stock_symbols)
                self._streams.append(stream)
                stream.run()
                failures = 0
            except Exception as exc:
                failures += 1
                logger.warning("stream | stock error | %s: %s", type(exc).__name__, exc)
            finally:
                if stream in self._streams:
                    self._streams.remove(stream)
                self.health.note_disconnected("stock")
                logger.warning("stream | stock desconectado")
            if self._should_run:
                delay = min(self._reconnect_max, self._reconnect_min * (2 ** min(failures, 6)))
                logger.info("stream | reconectando stock en %.0fs (backoff)", delay)
                time.sleep(delay)

    def _crypto_loop(self) -> None:
        failures = 0
        while self._should_run:
            stream = None
            try:
                logger.info("stream | conectando crypto US | %s", ",".join(self._crypto_symbols))
                kwargs: dict[str, Any] = {
                    "feed": CryptoFeed.US,
                    "websocket_params": self._websocket_params(),
                }
                if self._data_timeout is not None:
                    kwargs["data_timeout"] = self._data_timeout
                stream = CryptoDataStream(self._api_key, self._secret_key, **kwargs)
                self._hook_lifecycle(stream, "crypto")
                stream.subscribe_trades(self._on_trade, *self._crypto_symbols)
                stream.subscribe_quotes(self._on_quote, *self._crypto_symbols)
                self._streams.append(stream)
                stream.run()
                failures = 0
            except Exception as exc:
                failures += 1
                logger.warning("stream | crypto error | %s: %s", type(exc).__name__, exc)
            finally:
                if stream in self._streams:
                    self._streams.remove(stream)
                self.health.note_disconnected("crypto")
                logger.warning("stream | crypto desconectado")
            if self._should_run:
                delay = min(self._reconnect_max, self._reconnect_min * (2 ** min(failures, 6)))
                logger.info("stream | reconectando crypto en %.0fs (backoff)", delay)
                time.sleep(delay)

    def _hook_lifecycle(self, stream: Any, name: str) -> None:
        orig_start = stream._start_ws
        orig_close = stream.close

        async def _wrapped_start() -> None:
            logger.info("stream | handshake %s ...", name)
            await orig_start()
            self.health.note_connected(name)
            logger.info("stream | conectado %s", name)

        async def _wrapped_close() -> None:
            self.health.note_disconnected(name)
            await orig_close()

        stream._start_ws = _wrapped_start
        stream.close = _wrapped_close

    async def _on_trade(self, trade: Any) -> None:
        symbol, price = _extract_symbol_price(trade)
        if symbol and price > 0:
            self._ingest(symbol, price, _feed_for(symbol))

    async def _on_quote(self, quote: Any) -> None:
        symbol = str(getattr(quote, "symbol", "") or "")
        if not symbol and isinstance(quote, dict):
            symbol = str(quote.get("S") or quote.get("symbol") or "")
        bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
        ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
        if (bid <= 0 or ask <= 0) and isinstance(quote, dict):
            bid = float(quote.get("bp") or quote.get("bid_price") or 0.0)
            ask = float(quote.get("ap") or quote.get("ask_price") or 0.0)
        if symbol and bid > 0 and ask > 0:
            key = symbol.upper()
            with self._lock:
                self._quotes[key] = (bid, ask)
            self._ingest(symbol, (bid + ask) / 2.0, _feed_for(symbol))

    def _ingest(self, symbol: str, price: float, feed: str) -> None:
        key = symbol.upper()
        restored = self.health.note_tick(feed)
        if restored:
            logger.info("stream | ticks de nuevo | salida de fallback REST")
            self._emit_restored()
        with self._lock:
            self._prices[key] = price
            self._peaks[key] = max(self._peaks.get(key, 0.0), price)
            now = time.monotonic()
            last = self._last_eval.get(key, 0.0)
            if now - last < TICK_EVAL_MIN_SECONDS:
                return
            self._last_eval[key] = now
        try:
            self._eval_queue.put_nowait((key, price))
        except queue.Full:
            try:
                self._eval_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._eval_queue.put_nowait((key, price))
            except queue.Full:
                logger.debug("stream | cola de ticks llena | %s", key)


def _feed_for(symbol: str) -> str:
    return "crypto" if "/" in str(symbol).upper() else "stock"


def _extract_symbol_price(item: Any) -> tuple[str, float]:
    symbol = str(getattr(item, "symbol", "") or "")
    price = float(getattr(item, "price", 0.0) or 0.0)
    if (not symbol or price <= 0) and isinstance(item, dict):
        symbol = symbol or str(item.get("S") or item.get("symbol") or "")
        price = price or float(item.get("p") or item.get("price") or 0.0)
    return symbol, price
