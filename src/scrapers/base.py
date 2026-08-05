"""The abstract scraper every country implementation builds on.

``BaseScraper`` owns everything that must be true of every scraper — robots.txt
compliance, rate limiting, retry with backoff, request logging, run auditing,
error isolation, optional proxies and structure-drift detection — so a country
implementation only supplies :meth:`BaseScraper.fetch` and
:meth:`BaseScraper.parse`.

Run one directly::

    python -m src.scrapers.france.parcoursup
"""

from __future__ import annotations

import asyncio
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID

import httpx

from src.core.config import get_settings
from src.core.database import session_scope
from src.core.exceptions import (
    RateLimitedError,
    RobotsDisallowedError,
    ScrapeHTTPError,
    ScraperError,
    ScrapeTimeoutError,
    StructureChangedError,
)
from src.core.models import ScrapeRun, ScrapeStatus
from src.core.schemas import ScrapedProgram
from src.utils.logger import configure_logging, log_request, logger
from src.utils.rate_limiter import RateLimiter, build_rate_limiter
from src.utils.robots_checker import RobotsChecker

__all__ = ["BaseScraper", "RobotsPolicyMode", "ScrapeResult"]


class RobotsPolicyMode(str, Enum):
    """How a scraper treats ``robots.txt``.

    Attributes:
        ENFORCE: robots.txt is a hard stop. The default, and correct for any
            scraper that walks a site's HTML.
        API_CLIENT: The scraper consumes a documented data endpoint published
            for reuse under an open licence, and is therefore an API client
            rather than a crawler.
    """

    ENFORCE = "enforce"
    API_CLIENT = "api_client"


class ScrapeResult:
    """Outcome of one scraper run.

    Attributes:
        run_id: Primary key of the ``scrape_runs`` row, when persisted.
        items: Successfully parsed programmes.
        scraped: Number of source records retrieved.
        failed: Number of source records that could not be parsed.
        status: Final run status.
        errors: Human-readable messages for the failures encountered.
    """

    def __init__(self) -> None:
        self.run_id: UUID | None = None
        self.items: list[ScrapedProgram] = []
        self.scraped: int = 0
        self.failed: int = 0
        self.status: ScrapeStatus = ScrapeStatus.RUNNING
        self.errors: list[str] = []

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return (
            f"<ScrapeResult {self.status.value} scraped={self.scraped} "
            f"parsed={len(self.items)} failed={self.failed}>"
        )


class BaseScraper(ABC):
    """Base class for every country scraper.

    Subclasses must set :attr:`platform`, :attr:`country_code` and
    :attr:`base_url`, and implement :meth:`fetch` and :meth:`parse`.

    Args:
        persist_run: Whether to write a ``scrape_runs`` audit row. Disabled in
            unit tests that have no database.
        limit: Optional cap on the number of source records processed, for
            smoke runs.
    """

    #: Slug identifying the scraper, e.g. ``parcoursup``.
    platform: ClassVar[str]
    #: ISO 3166-1 alpha-2 code of the country covered.
    country_code: ClassVar[str]
    #: Origin used for robots.txt lookups and relative URLs.
    base_url: ClassVar[str]

    #: How this scraper treats robots.txt. Overriding to ``API_CLIENT`` requires
    #: :attr:`robots_policy_justification` to be set — see :meth:`check_robots`.
    robots_policy: ClassVar[RobotsPolicyMode] = RobotsPolicyMode.ENFORCE
    #: Why an ``API_CLIENT`` policy is legitimate: the licence and the reasoning.
    #: Recorded in the run log so the decision is visible in review and in ops.
    robots_policy_justification: ClassVar[str | None] = None

    #: Fields every source record must carry. Used for drift detection.
    expected_fields: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, *, persist_run: bool = True, limit: int | None = None) -> None:
        if not getattr(self, "platform", None):
            raise ScraperError(f"{type(self).__name__} must define a platform slug")
        if not getattr(self, "country_code", None):
            raise ScraperError(f"{type(self).__name__} must define a country_code",
                               platform=self.platform)
        self.settings = get_settings()
        self.persist_run = persist_run
        self.limit = limit
        self.log = logger.bind(platform=self.platform)
        self.robots = RobotsChecker(self.settings.scrape_user_agent)
        self._limiter: RateLimiter | None = None
        self._client: httpx.AsyncClient | None = None
        self._proxy_index = 0

    # ------------------------------------------------------------------
    # Contract for subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch(self) -> Sequence[dict[str, Any]]:
        """Retrieve raw source records.

        Returns:
            One mapping per source record, exactly as published.
        """

    @abstractmethod
    def parse(self, record: dict[str, Any]) -> ScrapedProgram:
        """Turn one raw source record into a validated item.

        Args:
            record: A single mapping from :meth:`fetch`.

        Returns:
            The parsed programme.

        Raises:
            ParserError: If the record cannot be interpreted.
        """

    # ------------------------------------------------------------------
    # HTTP with politeness built in
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
        if self._client is None:
            proxies = self.settings.proxies
            proxy = None
            if proxies:
                proxy = proxies[self._proxy_index % len(proxies)]
                self._proxy_index += 1
                self.log.debug("routing through proxy {}", proxy)
            self._client = httpx.AsyncClient(
                timeout=self.settings.scrape_timeout,
                headers={"User-Agent": self.settings.scrape_user_agent},
                follow_redirects=True,
                proxy=proxy,
            )
        return self._client

    async def check_robots(self, url: str) -> None:
        """Enforce this scraper's robots.txt policy for a URL.

        Args:
            url: The absolute URL about to be fetched.

        Raises:
            RobotsDisallowedError: If the policy is ``ENFORCE`` and robots.txt
                forbids the URL, or if ``API_CLIENT`` was declared without a
                justification.
        """
        if self.robots_policy is RobotsPolicyMode.API_CLIENT:
            if not self.robots_policy_justification:
                raise RobotsDisallowedError(
                    url, user_agent=self.settings.scrape_user_agent, platform=self.platform
                )
            return
        if not await self.robots.can_fetch(url):
            raise RobotsDisallowedError(
                url, user_agent=self.settings.scrape_user_agent, platform=self.platform
            )

    async def _delay_for(self, url: str) -> float:
        """Return the polite delay for a host, honouring any ``Crawl-delay``."""
        return await self.robots.effective_delay(url, self.settings.scrape_delay_seconds)

    async def request(self, url: str, **kwargs: Any) -> httpx.Response:
        """Perform one rate-limited, retried, logged HTTP GET.

        Applies, in order: the robots policy, the per-platform rate limit, then
        the request itself with exponential backoff on transient failures.

        Args:
            url: Absolute URL to fetch.
            **kwargs: Extra arguments forwarded to ``httpx.AsyncClient.get``.

        Returns:
            The successful response.

        Raises:
            RobotsDisallowedError: If the robots policy forbids the URL.
            ScrapeTimeoutError: If every attempt timed out.
            ScrapeHTTPError: If the final attempt returned an error status.
            RateLimitedError: If the platform kept signalling throttling.
        """
        await self.check_robots(url)

        if self._limiter is None:
            self._limiter = await build_rate_limiter(await self._delay_for(url))
        await self._limiter.acquire(self.platform)

        client = await self._get_client()
        backoff = self.settings.scrape_retry_backoff
        attempts = self.settings.scrape_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = await client.get(url, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = ScrapeTimeoutError(
                    url, timeout=float(self.settings.scrape_timeout), platform=self.platform
                )
                log_request(url=url, duration_ms=(time.monotonic() - started) * 1000,
                            platform=self.platform, error=type(exc).__name__)
            except httpx.HTTPError as exc:
                last_error = ScraperError(f"transport error fetching {url}: {exc}",
                                          platform=self.platform, url=url)
                log_request(url=url, duration_ms=(time.monotonic() - started) * 1000,
                            platform=self.platform, error=type(exc).__name__)
            else:
                duration_ms = (time.monotonic() - started) * 1000
                log_request(
                    url=url, status_code=response.status_code, duration_ms=duration_ms,
                    size_bytes=len(response.content), platform=self.platform,
                )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    last_error = RateLimitedError(
                        url,
                        retry_after=float(retry_after) if retry_after and retry_after.isdigit()
                        else None,
                        platform=self.platform,
                    )
                elif response.status_code >= 500:
                    last_error = ScrapeHTTPError(
                        url, status_code=response.status_code, platform=self.platform,
                        body_excerpt=response.text[:200],
                    )
                elif response.status_code >= 400:
                    # 4xx other than 429 will not fix itself; fail immediately.
                    raise ScrapeHTTPError(
                        url, status_code=response.status_code, platform=self.platform,
                        body_excerpt=response.text[:200],
                    )
                else:
                    return response

            if attempt < attempts - 1:
                wait = backoff[min(attempt, len(backoff) - 1)]
                self.log.warning(
                    "attempt {}/{} failed for {} ({}); retrying in {}s",
                    attempt + 1, attempts, url, last_error, wait,
                )
                await asyncio.sleep(wait)

        assert last_error is not None  # loop always sets it before exhausting
        raise last_error

    # ------------------------------------------------------------------
    # Structure drift
    # ------------------------------------------------------------------

    def check_structure(self, records: Sequence[dict[str, Any]]) -> None:
        """Verify the source still has the shape this scraper expects.

        Requirement 8 of the scraper contract. A parser that silently maps a
        renamed field to ``None`` would replace good data with empty records, so
        a missing expected field aborts the run instead.

        Args:
            records: The raw records returned by :meth:`fetch`.

        Raises:
            StructureChangedError: If any expected field is absent from every
                sampled record.
        """
        if not self.expected_fields or not records:
            return
        # Sample rather than scan: optional fields are legitimately absent from
        # individual records, so a field only counts as missing when no record
        # in the sample carries it.
        sample = records[:50]
        seen: set[str] = set()
        for record in sample:
            seen.update(record.keys())
        missing = self.expected_fields - seen
        if missing:
            raise StructureChangedError(platform=self.platform, missing=missing)

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def _open_run(self) -> UUID | None:
        """Insert the ``scrape_runs`` row for this execution.

        Returns:
            The new row's identifier, or ``None`` when persistence is disabled.
        """
        if not self.persist_run:
            return None
        async with session_scope() as session:
            run = ScrapeRun(
                platform=self.platform,
                country_code=self.country_code,
                started_at=datetime.now(timezone.utc),
                status=ScrapeStatus.RUNNING,
            )
            session.add(run)
            await session.flush()
            return run.id

    async def _close_run(self, run_id: UUID | None, result: ScrapeResult) -> None:
        """Finalise the ``scrape_runs`` row with counters and status.

        Args:
            run_id: Identifier returned by :meth:`_open_run`.
            result: The run outcome.
        """
        if run_id is None:
            return
        async with session_scope() as session:
            run = await session.get(ScrapeRun, run_id)
            if run is None:
                self.log.error("scrape_runs row {} vanished before finalisation", run_id)
                return
            run.finished_at = datetime.now(timezone.utc)
            run.status = result.status
            run.records_scraped = result.scraped
            run.records_failed = result.failed
            run.error_log = "\n".join(result.errors[:100]) or None

    async def run(self) -> ScrapeResult:
        """Execute the scraper end to end.

        Opens an audit row, fetches, checks for structural drift, parses every
        record, and closes the audit row with the final status. A parse failure
        on one record downgrades the run to ``partial`` rather than aborting it;
        a failure of the fetch itself marks the run ``failed``.

        Errors are contained: they are recorded and re-raised to this scraper's
        caller only, so a runner iterating over countries can continue with the
        next one.

        Returns:
            The run outcome.

        Raises:
            ScraperError: If the fetch stage fails outright.
        """
        result = ScrapeResult()
        run_id = await self._open_run()
        result.run_id = run_id

        if self.robots_policy is RobotsPolicyMode.API_CLIENT:
            self.log.info(
                "robots policy: API_CLIENT — {}", self.robots_policy_justification
            )

        try:
            records = await self.fetch()
            result.scraped = len(records)
            self.log.info("fetched {} source records", result.scraped)

            self.check_structure(records)

            if self.limit is not None:
                records = records[: self.limit]
                self.log.info("limited to {} records for this run", len(records))

            for record in records:
                try:
                    result.items.append(self.parse(record))
                except Exception as exc:
                    result.failed += 1
                    if len(result.errors) < 100:
                        result.errors.append(f"{type(exc).__name__}: {exc}")

            result.status = ScrapeStatus.PARTIAL if result.failed else ScrapeStatus.SUCCESS
            self.log.info(
                "parsed {} items, {} failures -> {}",
                len(result.items), result.failed, result.status.value,
            )
        except Exception as exc:
            result.status = ScrapeStatus.FAILED
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.errors.append(traceback.format_exc(limit=5))
            self.log.error("run failed: {}", exc)
            await self._close_run(run_id, result)
            await self.aclose()
            raise
        else:
            await self._close_run(run_id, result)
            await self.aclose()
            return result

    async def aclose(self) -> None:
        """Release the HTTP client and rate limiter."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._limiter is not None:
            await self._limiter.close()
            self._limiter = None

    # ------------------------------------------------------------------
    # CLI entry point
    # ------------------------------------------------------------------

    @classmethod
    async def main(cls, *, limit: int | None = None, persist_run: bool = True) -> ScrapeResult:
        """Run the scraper from the command line.

        Args:
            limit: Optional cap on records processed.
            persist_run: Whether to record the run in the database.

        Returns:
            The run outcome.
        """
        configure_logging()
        scraper = cls(persist_run=persist_run, limit=limit)
        return await scraper.run()
