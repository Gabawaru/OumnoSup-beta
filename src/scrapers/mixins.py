"""Optional capabilities scrapers can mix into :class:`~src.scrapers.base.BaseScraper`.

Each mixin is independent and composes left of ``BaseScraper``::

    class MyScraper(JSRenderMixin, CacheMixin, BaseScraper):
        ...

Playwright and Redis are heavy, optional dependencies. They are imported inside
the methods that need them, so importing this module — and therefore any scraper
package — works without either installed. A scraper that never renders JavaScript
should not force a browser download on anyone running the test suite.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.core.config import get_settings
from src.core.exceptions import ScraperError
from src.utils.logger import logger
from src.utils.rate_limiter import RateLimiter, build_rate_limiter

__all__ = [
    "CacheMixin",
    "JSRenderMixin",
    "ProxyRotationMixin",
    "RateLimitMixin",
    "RetryMixin",
    "RobotsCheckerMixin",
]


class RetryMixin:
    """Exponential backoff for arbitrary awaitable operations.

    ``BaseScraper.request`` already retries HTTP calls. This mixin exposes the
    same policy for anything else a scraper needs to retry, such as a browser
    navigation or a flaky third-party client.
    """

    async def with_retry(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        """Call an awaitable, retrying with the configured backoff.

        Args:
            operation: The coroutine function to call.
            *args: Positional arguments for the operation.
            **kwargs: Keyword arguments for the operation.

        Returns:
            Whatever the operation returns.

        Raises:
            Exception: The last error, if every attempt failed.
        """
        import asyncio

        settings = get_settings()
        backoff = settings.scrape_retry_backoff
        attempts = settings.scrape_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                return await operation(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if attempt < attempts - 1:
                    wait = backoff[min(attempt, len(backoff) - 1)]
                    logger.warning("attempt {}/{} failed ({}); retrying in {}s",
                                   attempt + 1, attempts, exc, wait)
                    await asyncio.sleep(wait)

        assert last_error is not None
        raise last_error


class RateLimitMixin:
    """A rate limiter independent of the one ``BaseScraper.request`` installs.

    Useful when a scraper talks to a second host with its own budget.
    """

    _extra_limiter: RateLimiter | None = None

    async def limit(self, key: str, delay_seconds: float | None = None) -> float:
        """Wait until a request against ``key`` is permitted.

        Args:
            key: Bucket name, usually a host or platform slug.
            delay_seconds: Interval to enforce. Defaults to the configured delay.

        Returns:
            Seconds spent waiting.
        """
        if self._extra_limiter is None:
            delay = delay_seconds or get_settings().scrape_delay_seconds
            self._extra_limiter = await build_rate_limiter(delay)
        return await self._extra_limiter.acquire(key)


class RobotsCheckerMixin:
    """Ad-hoc robots.txt queries beyond the automatic check on every request."""

    async def robots_allows(self, url: str) -> bool:
        """Whether robots.txt permits fetching a URL.

        Args:
            url: Absolute URL to test.

        Returns:
            ``True`` if permitted.
        """
        from src.utils.robots_checker import RobotsChecker

        checker = getattr(self, "robots", None) or RobotsChecker()
        return await checker.can_fetch(url)


class ProxyRotationMixin:
    """Round-robin selection over the configured proxy pool."""

    _proxy_cursor: int = 0

    def next_proxy(self) -> str | None:
        """Return the next proxy URL, or ``None`` when none are configured.

        Returns:
            A proxy URL from ``PROXY_LIST``.
        """
        proxies = get_settings().proxies
        if not proxies:
            return None
        proxy = proxies[self._proxy_cursor % len(proxies)]
        type(self)._proxy_cursor = self._proxy_cursor + 1
        return proxy


class CacheMixin:
    """Redis-backed response cache, to avoid refetching during development.

    Silently degrades to no caching when Redis is unavailable: a cache is an
    optimisation, and losing it must never fail a run.
    """

    cache_ttl_seconds: int = 3600
    _cache_client: Any = None
    _cache_unavailable: bool = False

    async def _cache_connect(self) -> Any:
        """Return a Redis client, or ``None`` if Redis cannot be reached."""
        if self._cache_unavailable:
            return None
        if self._cache_client is None:
            try:
                import redis.asyncio as aioredis

                client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
                await client.ping()
                type(self)._cache_client = client
            except Exception as exc:
                logger.debug("response cache disabled ({})", exc)
                type(self)._cache_unavailable = True
                return None
        return self._cache_client

    @staticmethod
    def _cache_key(url: str) -> str:
        """Return the Redis key for a URL."""
        return f"oumnosup:cache:{hashlib.sha256(url.encode()).hexdigest()[:32]}"

    async def cache_get(self, url: str) -> Any | None:
        """Read a cached response body.

        Args:
            url: The URL used as the cache key.

        Returns:
            The decoded payload, or ``None`` on a miss.
        """
        client = await self._cache_connect()
        if client is None:
            return None
        raw = await client.get(self._cache_key(url))
        return json.loads(raw) if raw else None

    async def cache_set(self, url: str, payload: Any) -> None:
        """Store a response body in the cache.

        Args:
            url: The URL used as the cache key.
            payload: Any JSON-serialisable value.
        """
        client = await self._cache_connect()
        if client is None:
            return
        await client.set(self._cache_key(url), json.dumps(payload), ex=self.cache_ttl_seconds)


class JSRenderMixin:
    """Renders JavaScript-driven pages with Playwright.

    Only for platforms that build their content client-side. A scraper backed by
    a JSON API must not use this: launching a browser to read JSON is orders of
    magnitude more expensive and far more fragile.
    """

    browser_headless: bool = True
    #: Milliseconds to wait for the network to settle after navigation.
    render_wait_ms: int = 2000

    async def render(self, url: str, *, wait_for: str | None = None) -> str:
        """Load a URL in a headless browser and return the rendered HTML.

        Args:
            url: The page to load.
            wait_for: Optional CSS selector to await before capturing.

        Returns:
            The page's HTML after scripts have run.

        Raises:
            ScraperError: If Playwright is not installed.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ScraperError(
                "JSRenderMixin requires Playwright: pip install playwright "
                "&& playwright install chromium",
                platform=getattr(self, "platform", None),
            ) from exc

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.browser_headless)
            try:
                page = await browser.new_page(
                    user_agent=get_settings().scrape_user_agent
                )
                await page.goto(url, wait_until="networkidle")
                if wait_for:
                    await page.wait_for_selector(wait_for, timeout=self.render_wait_ms)
                return await page.content()
            finally:
                await browser.close()
