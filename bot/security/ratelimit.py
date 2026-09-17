"""Rate limiting (token bucket) para API de datos y envio de ordenes."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from bot.security.exceptions import RateLimitError


@dataclass
class RateLimitConfig:
    data_per_minute: int = 120
    order_per_minute: int = 10
    order_per_day: int = 40
    # Un ciclo 3m+6m con 2 símbolos puede pedir >20 tokens si no hay cache WS.
    burst_data: int = 80
    burst_orders: int = 3


class _TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int) -> None:
        self.rate = max(rate_per_minute, 1) / 60.0
        self.capacity = float(max(burst, 1))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> None:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.updated
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated = now
            if self.tokens < n:
                raise RateLimitError("Limite de tasa excedido; reintente mas tarde")
            self.tokens -= n


class RateLimiter:
    """Limites por categoria: data, trading_read, order."""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self.config = config or RateLimitConfig()
        self._buckets = {
            "data": _TokenBucket(self.config.data_per_minute, self.config.burst_data),
            "trading_read": _TokenBucket(self.config.data_per_minute, self.config.burst_data),
            "order": _TokenBucket(self.config.order_per_minute, self.config.burst_orders),
        }
        self._daily_orders = 0
        self._day_key = self._today()
        self._lock = threading.Lock()
        self._hits: dict[str, int] = defaultdict(int)

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def acquire(self, category: str) -> None:
        bucket = self._buckets.get(category)
        if bucket is None:
            raise RateLimitError("Categoria de rate limit desconocida")
        bucket.acquire()
        with self._lock:
            self._hits[category] += 1
            if category == "order":
                today = self._today()
                if today != self._day_key:
                    self._day_key = today
                    self._daily_orders = 0
                if self._daily_orders >= self.config.order_per_day:
                    raise RateLimitError("Limite diario de ordenes alcanzado")
                self._daily_orders += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits_data": self._hits["data"],
                "hits_trading_read": self._hits["trading_read"],
                "hits_order": self._hits["order"],
                "orders_today": self._daily_orders,
            }
