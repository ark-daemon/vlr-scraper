"""Token bucket rate limiter with per-host support and jitter."""

from __future__ import annotations

import asyncio
import random
import time

from loguru import logger


class TokenBucketRateLimiter:
    """
    Token bucket algorithm.
    Allows up to `rate` requests per second with a burst of `capacity` tokens.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, rate: float = 0.67, capacity: float = 3.0) -> None:
        self.rate = rate  # tokens added per second
        self.capacity = capacity  # max tokens in bucket
        self.tokens = capacity  # start full
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        added = elapsed * self.rate
        self.tokens = min(self.capacity, self.tokens + added)
        self._last_refill = now

    async def acquire(self, jitter: float = 0.5) -> None:
        """
        Block until a token is available, then consume one.
        Adds random jitter of up to `jitter` seconds.
        """
        while True:
            sleep_jitter = 0.0
            async with self._lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    # Add jitter outside the lock
                    sleep_jitter = random.uniform(0, jitter)
                else:
                    # Calculate wait time until next token available
                    wait = (1.0 - self.tokens) / self.rate
                    sleep_jitter = -1.0
            if sleep_jitter >= 0.0:
                if sleep_jitter > 0:
                    await asyncio.sleep(sleep_jitter)
                return
            # Wait outside the lock
            logger.debug(f"Rate limiter: waiting {wait:.2f}s for token")
            await asyncio.sleep(wait)

    async def wait_for_retry_after(self, retry_after: int) -> None:
        """Honour a 429 Retry-After header exactly."""
        logger.warning(f"Rate limited (429). Sleeping {retry_after}s per Retry-After header.")
        await asyncio.sleep(retry_after)
        # Drain any stale tokens to prevent burst after long sleep
        async with self._lock:
            self.tokens = 0.0
            self._last_refill = time.monotonic()


# Module-level singleton used by all scrapers
_global_limiter: TokenBucketRateLimiter | None = None


def get_limiter(rate: float = 0.67, capacity: float = 3.0) -> TokenBucketRateLimiter:
    global _global_limiter
    if _global_limiter is None or _global_limiter.rate != rate or _global_limiter.capacity != capacity:
        _global_limiter = TokenBucketRateLimiter(rate=rate, capacity=capacity)
    return _global_limiter


def reset_limiter() -> None:
    global _global_limiter
    _global_limiter = None
