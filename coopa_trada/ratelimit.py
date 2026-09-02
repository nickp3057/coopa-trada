"""Token-bucket rate limiter for schwab-py client calls.

Schwab's Trader API tolerates roughly 120 requests/minute. Every client call
in ``SchwabBroker``, ``ContractResolver`` and ``engine`` goes through one
shared :class:`RateLimiter`.
"""

from __future__ import annotations

import threading
import time

__all__ = ["RateLimiter"]


class RateLimiter:
    def __init__(self, max_per_minute: int = 120, *, sleep=time.sleep, monotonic=time.monotonic):
        if max_per_minute <= 0:
            raise ValueError("max_per_minute must be positive")
        self.capacity = float(max_per_minute)
        self.refill_per_sec = max_per_minute / 60.0
        self._tokens = float(max_per_minute)
        self._last = monotonic()
        self._lock = threading.Lock()
        self._sleep = sleep
        self._monotonic = monotonic

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_sec)
            self._last = now

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then consume them."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.refill_per_sec
            self._sleep(max(wait, 0.0))

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens
