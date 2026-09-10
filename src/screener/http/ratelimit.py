"""Global async token bucket.

One instance is shared by every request the client makes, so concurrency and rate
limiting are independent knobs: concurrency caps in-flight requests, the bucket caps
requests per minute regardless of how many workers want one.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Refills continuously at `rate_per_minute`, capped at `capacity` tokens."""

    def __init__(self, rate_per_minute: int, capacity: int | None = None) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_second = rate_per_minute / 60.0
        # A small burst allowance smooths batch starts without exceeding the average.
        self.capacity = float(capacity if capacity is not None else max(1, rate_per_minute // 4))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
            self._updated = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate_per_second
            await asyncio.sleep(sleep_for)
            waited += sleep_for
