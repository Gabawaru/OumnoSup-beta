"""Redis response cache for read endpoints.

Admission data changes when a scrape runs — daily at the fastest — while the
same handful of queries are asked constantly. Caching GET responses for a few
minutes removes almost all of the repeated database work.

Redis is optional throughout the project, and it is optional here too: if the
server is unreachable the cache reports a miss and the request is served
normally. A cache going away must never take the API down, so every Redis call
is wrapped and failures are logged once rather than raised.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from src.core.config import get_settings
from src.utils.logger import logger

__all__ = ["ResponseCache", "get_cache"]

#: Prefix for every key this module writes, so a shared Redis stays tidy and
#: `invalidate()` can clear the API's keys without touching anyone else's.
_PREFIX: Final[str] = "oumnosup:api:"


class ResponseCache:
    """A best-effort JSON cache backed by Redis.

    Args:
        ttl_seconds: How long an entry stays valid.
        redis_url: Connection URL. Defaults to the configured ``redis_url``.
    """

    def __init__(self, ttl_seconds: int | None = None, redis_url: str | None = None) -> None:
        settings = get_settings()
        self.ttl = ttl_seconds if ttl_seconds is not None else settings.api_cache_ttl_seconds
        self._redis_url = redis_url or settings.redis_url
        self._redis: Any = None
        self._unavailable = False

    @property
    def enabled(self) -> bool:
        """Whether caching is switched on at all."""
        return self.ttl > 0

    @property
    def active(self) -> bool:
        """Whether a usable Redis connection has been established.

        ``False`` before the first request and whenever Redis is unreachable, so
        callers can avoid advertising an ``X-Cache`` header that would imply a
        cache exists when none does.
        """
        return self.enabled and not self._unavailable and self._redis is not None

    async def _client(self) -> Any:
        """Return a connected Redis client, or ``None`` if unreachable.

        The driver is imported lazily and a failure is remembered, so an absent
        Redis costs one connection attempt per process rather than one per
        request.

        Returns:
            The Redis client, or ``None``.
        """
        if self._unavailable or not self.enabled:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis

                client = aioredis.from_url(self._redis_url, decode_responses=True)
                await client.ping()
                self._redis = client
                logger.info("API response cache active (ttl {}s)", self.ttl)
            except Exception as exc:
                self._unavailable = True
                logger.info("API response cache disabled ({})", exc)
                return None
        return self._redis

    @staticmethod
    def key_for(path: str, query: str) -> str:
        """Build the cache key for a request.

        The full path and query string are hashed, so two requests differing by
        a single filter never share an entry.

        Args:
            path: Request path.
            query: Raw query string.

        Returns:
            The Redis key.
        """
        digest = hashlib.sha256(f"{path}?{query}".encode()).hexdigest()[:32]
        return f"{_PREFIX}{digest}"

    async def get(self, key: str) -> dict[str, Any] | None:
        """Read a cached response body.

        Args:
            key: Key from :meth:`key_for`.

        Returns:
            The decoded body, or ``None`` on a miss or any Redis failure.
        """
        client = await self._client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:
            logger.debug("cache read failed: {}", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # A corrupt entry is a miss, not an error the caller should see.
            return None

    async def set(self, key: str, payload: bytes) -> None:
        """Store a response body.

        Args:
            key: Key from :meth:`key_for`.
            payload: The raw JSON body to cache.
        """
        client = await self._client()
        if client is None:
            return
        try:
            await client.set(key, payload.decode("utf-8"), ex=self.ttl)
        except Exception as exc:
            logger.debug("cache write failed: {}", exc)

    async def invalidate(self) -> int:
        """Drop every entry this API wrote.

        Called after a scrape, since stored data has changed and a cached page
        of programmes may now be wrong.

        Returns:
            The number of keys removed.
        """
        client = await self._client()
        if client is None:
            return 0
        removed = 0
        try:
            async for key in client.scan_iter(match=f"{_PREFIX}*", count=500):
                await client.delete(key)
                removed += 1
        except Exception as exc:
            logger.debug("cache invalidation failed: {}", exc)
            return removed
        if removed:
            logger.info("invalidated {} cached API responses", removed)
        return removed

    async def close(self) -> None:
        """Close the Redis connection if one was opened."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


_cache: ResponseCache | None = None


def get_cache() -> ResponseCache:
    """Return the process-wide response cache.

    Returns:
        The shared :class:`ResponseCache`.
    """
    global _cache
    if _cache is None:
        _cache = ResponseCache()
    return _cache
