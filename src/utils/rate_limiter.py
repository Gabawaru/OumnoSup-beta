"""Per-platform rate limiting.

A token bucket keyed by platform slug, with two backends:

* :class:`InMemoryRateLimiter` — process-local, the default. Local development
  and single-process runs need no Redis.
* :class:`RedisRateLimiter` — shared across processes, so several workers
  scraping the same platform still respect one combined rate.

Both honour the same contract: :meth:`RateLimiter.acquire` returns only once the
caller is allowed to issue a request.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from src.core.config import get_settings
from src.utils.logger import logger

__all__ = ["InMemoryRateLimiter", "RateLimiter", "RedisRateLimiter", "build_rate_limiter"]


class RateLimiter(ABC):
    """Interface for a per-platform rate limiter.

    Args:
        delay_seconds: Minimum interval between two requests to a platform.
    """

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = max(0.0, delay_seconds)

    @abstractmethod
    async def acquire(self, key: str) -> float:
        """Block until a request against ``key`` may proceed.

        Args:
            key: Platform slug the limit applies to.

        Returns:
            The number of seconds spent waiting.
        """

    async def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any resources held by the limiter.

        A no-op by default: an in-memory limiter holds nothing to release, and
        forcing every implementation to define one would be noise.
        """


class InMemoryRateLimiter(RateLimiter):
    """Process-local rate limiter.

    Serialises callers per key with a lock, so concurrent tasks scraping the
    same platform queue up rather than all firing at once.
    """

    def __init__(self, delay_seconds: float) -> None:
        super().__init__(delay_seconds)
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Return the lock guarding a key, creating it on first use."""
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def acquire(self, key: str) -> float:
        """Block until the configured interval has elapsed since the last call.

        Args:
            key: Platform slug the limit applies to.

        Returns:
            The number of seconds spent waiting.
        """
        if self.delay_seconds <= 0:
            return 0.0
        async with self._lock_for(key):
            now = time.monotonic()
            last = self._last.get(key)
            waited = 0.0
            if last is not None:
                remaining = self.delay_seconds - (now - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    waited = remaining
            self._last[key] = time.monotonic()
            return waited


class RedisRateLimiter(RateLimiter):
    """Rate limiter shared across processes through Redis.

    The check-and-set runs as a Lua script so that reading the last-call
    timestamp and writing the new one is atomic. Doing it with separate GET and
    SET calls would let two workers both observe an expired window and fire
    simultaneously.

    Args:
        delay_seconds: Minimum interval between two requests to a platform.
        redis_url: Connection URL. Defaults to the configured ``redis_url``.
    """

    #: Returns 0 if the caller may proceed now, else the milliseconds to wait.
    _SCRIPT = """
    local last = redis.call('GET', KEYS[1])
    local now = tonumber(ARGV[1])
    local delay = tonumber(ARGV[2])
    if last then
        local wait = delay - (now - tonumber(last))
        if wait > 0 then return wait end
    end
    redis.call('SET', KEYS[1], ARGV[1], 'PX', math.ceil(delay * 2))
    return 0
    """

    def __init__(self, delay_seconds: float, redis_url: str | None = None) -> None:
        super().__init__(delay_seconds)
        self._redis_url = redis_url or get_settings().redis_url
        self._redis: Any = None
        self._script: Any = None

    async def _connect(self) -> Any:
        """Return a connected Redis client, importing the driver lazily.

        Redis is an optional dependency: importing it at module level would make
        every scraper require it even when running with the in-memory limiter.

        Returns:
            The Redis client.
        """
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            self._script = self._redis.register_script(self._SCRIPT)
        return self._redis

    async def acquire(self, key: str) -> float:
        """Block until the shared window allows a request against ``key``.

        Args:
            key: Platform slug the limit applies to.

        Returns:
            The number of seconds spent waiting.
        """
        if self.delay_seconds <= 0:
            return 0.0
        await self._connect()
        delay_ms = self.delay_seconds * 1000
        waited = 0.0
        while True:
            now_ms = time.time() * 1000
            wait_ms = float(await self._script(keys=[f"oumnosup:rl:{key}"],
                                               args=[now_ms, delay_ms]))
            if wait_ms <= 0:
                return waited
            await asyncio.sleep(wait_ms / 1000)
            waited += wait_ms / 1000

    async def close(self) -> None:
        """Close the Redis connection if one was opened."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


async def build_rate_limiter(delay_seconds: float, *, prefer_redis: bool = True) -> RateLimiter:
    """Build the best available rate limiter for the current environment.

    Tries Redis when asked, and falls back to the in-memory limiter if the
    driver is missing or the server is unreachable. A missing Redis degrades the
    limiter's scope, never the politeness of a single process.

    Args:
        delay_seconds: Minimum interval between two requests to a platform.
        prefer_redis: Whether to attempt the distributed backend first.

    Returns:
        A ready-to-use limiter.
    """
    if prefer_redis:
        limiter = RedisRateLimiter(delay_seconds)
        try:
            redis = await limiter._connect()
            await redis.ping()
            return limiter
        except Exception as exc:
            await limiter.close()
            logger.debug("Redis unavailable ({}), using in-process rate limiting", exc)
    return InMemoryRateLimiter(delay_seconds)
