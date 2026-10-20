"""Async token bucket shared by the HTTP fetcher and the browser (one request budget per domain)."""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_second: float, burst: int = 1):
        self.rate = max(rate_per_second, 0.01)
        self.capacity = max(burst, 1)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.penalty_until = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self.penalty_until:
                    await asyncio.sleep(self.penalty_until - now)
                    continue
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)

    def back_off(self, seconds: float) -> None:
        """Server asked us to slow down (429 / Retry-After): pause the whole domain budget."""
        self.penalty_until = max(self.penalty_until, time.monotonic() + seconds)
