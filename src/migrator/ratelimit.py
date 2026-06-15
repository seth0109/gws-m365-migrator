from __future__ import annotations

import threading
import time
from collections import defaultdict


class TokenBucket:
    """Thread-safe token-bucket rate limiter."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self._rate = rate  # tokens per second
        self._capacity = capacity or rate
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        with self._lock:
            self._refill()
            wait = 0.0
            if self._tokens < tokens:
                wait = (tokens - self._tokens) / self._rate
            self._tokens = max(0.0, self._tokens - tokens)
        if wait > 0:
            time.sleep(wait)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now


class RateLimiterRegistry:
    """Central registry of named token-bucket limiters."""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def register(self, name: str, rate: float, capacity: float | None = None) -> None:
        with self._lock:
            self._buckets[name] = TokenBucket(rate, capacity)

    def acquire(self, name: str, tokens: float = 1.0) -> None:
        with self._lock:
            bucket = self._buckets.get(name)
        if bucket is None:
            raise KeyError(f"No rate limiter registered for '{name}'")
        bucket.acquire(tokens)


class PerUserRateLimiter:
    """Per-key (e.g. per-mailbox) rate limiter backed by individual token buckets."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self._rate = rate
        self._capacity = capacity
        self._buckets: dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(self._rate, self._capacity)
        )
        self._lock = threading.Lock()

    def acquire(self, key: str, tokens: float = 1.0) -> None:
        with self._lock:
            bucket = self._buckets[key]
        bucket.acquire(tokens)


# Singleton registry — populated by the orchestrator from config
registry = RateLimiterRegistry()
