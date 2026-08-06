"""Exception hierarchy.

Every error the project raises derives from :class:`OumnoSupError`, so callers
can catch the whole family, a branch, or a single condition. The project never
raises bare ``Exception``.

Exceptions carry structured context as attributes rather than folding everything
into a message string. That is what lets :class:`src.scrapers.base.BaseScraper`
write a useful ``scrape_runs.error_log`` and the API map a failure to the right
HTTP status without re-parsing text.
"""

from __future__ import annotations

from typing import Any


class OumnoSupError(Exception):
    """Base class for every OumnoSup error.

    Args:
        message: Human-readable description.
        **context: Arbitrary structured detail attached to the error and
            included in :meth:`to_dict`.

    Attributes:
        message: The description passed in.
        context: Structured detail, safe to serialise into logs.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def to_dict(self) -> dict[str, Any]:
        """Return a serialisable representation of the error.

        Returns:
            A mapping with the error class name, message and context.
        """
        return {"error": type(self).__name__, "message": self.message, **self.context}

    def __str__(self) -> str:
        """Return the message followed by any context, sorted for stability."""
        if not self.context:
            return self.message
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


# ---------------------------------------------------------------------------
# Configuration and infrastructure
# ---------------------------------------------------------------------------


class ConfigurationError(OumnoSupError):
    """Raised when settings are missing, malformed or unusable."""


class DatabaseError(OumnoSupError):
    """Raised when a database operation fails for a non-business reason."""


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


class ScraperError(OumnoSupError):
    """Base class for every scraping failure.

    Args:
        message: Human-readable description.
        platform: Slug of the scraper that failed, e.g. ``parcoursup``.
        **context: Additional structured detail.
    """

    def __init__(self, message: str, *, platform: str | None = None, **context: Any) -> None:
        super().__init__(message, platform=platform, **context)
        self.platform = platform


class RobotsDisallowedError(ScraperError):
    """Raised when ``robots.txt`` forbids the URL the scraper was about to fetch.

    This is a hard stop, never a warning: the run is abandoned rather than
    downgraded, so a policy change upstream cannot be silently ignored.

    Args:
        url: The disallowed URL.
        user_agent: The user agent the rule was matched against.
        platform: Slug of the scraper.
    """

    def __init__(self, url: str, *, user_agent: str, platform: str | None = None) -> None:
        super().__init__(
            f"robots.txt disallows fetching {url}",
            platform=platform,
            url=url,
            user_agent=user_agent,
        )
        self.url = url
        self.user_agent = user_agent


class RateLimitedError(ScraperError):
    """Raised when the remote platform signals the client is going too fast.

    Args:
        url: The URL that was throttled.
        retry_after: Seconds the platform asked the client to wait, when given.
        platform: Slug of the scraper.
    """

    def __init__(
        self, url: str, *, retry_after: float | None = None, platform: str | None = None
    ) -> None:
        super().__init__(
            f"rate limited by the platform while fetching {url}",
            platform=platform,
            url=url,
            retry_after=retry_after,
        )
        self.url = url
        self.retry_after = retry_after


class ScrapeTimeoutError(ScraperError):
    """Raised when a request exceeds the configured timeout.

    Args:
        url: The URL being fetched.
        timeout: The timeout in seconds that elapsed.
        platform: Slug of the scraper.
    """

    def __init__(self, url: str, *, timeout: float, platform: str | None = None) -> None:
        super().__init__(
            f"request to {url} timed out after {timeout}s",
            platform=platform,
            url=url,
            timeout=timeout,
        )
        self.url = url
        self.timeout = timeout


class ScrapeHTTPError(ScraperError):
    """Raised for an unsuccessful HTTP response.

    Args:
        url: The URL being fetched.
        status_code: The HTTP status returned.
        platform: Slug of the scraper.
        body_excerpt: A short excerpt of the response body, for diagnosis.
    """

    def __init__(
        self,
        url: str,
        *,
        status_code: int,
        platform: str | None = None,
        body_excerpt: str | None = None,
    ) -> None:
        super().__init__(
            f"HTTP {status_code} while fetching {url}",
            platform=platform,
            url=url,
            status_code=status_code,
            body_excerpt=body_excerpt,
        )
        self.url = url
        self.status_code = status_code
        self.body_excerpt = body_excerpt


class StructureChangedError(ScraperError):
    """Raised when the source no longer has the shape the scraper expects.

    Upstream platforms rename and remove fields without notice. Detecting that
    and stopping is the whole point: a parser that silently maps missing fields
    to ``None`` would quietly replace good data with empty records.

    Args:
        platform: Slug of the scraper.
        missing: Expected fields that were absent.
        unexpected: Fields present that the scraper does not know about.
    """

    def __init__(
        self,
        *,
        platform: str | None = None,
        missing: frozenset[str] | set[str] | None = None,
        unexpected: frozenset[str] | set[str] | None = None,
    ) -> None:
        missing = set(missing or ())
        unexpected = set(unexpected or ())
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if unexpected:
            parts.append(f"unexpected {sorted(unexpected)}")
        super().__init__(
            "source structure changed: " + "; ".join(parts or ["shape differs from reference"]),
            platform=platform,
            missing=sorted(missing),
            unexpected=sorted(unexpected),
        )
        self.missing = missing
        self.unexpected = unexpected


class ParserError(ScraperError):
    """Raised when a record cannot be turned into a scraped item.

    Args:
        message: Human-readable description.
        platform: Slug of the scraper.
        field: The field that could not be parsed, when known.
        value: The offending value, when known.
    """

    def __init__(
        self,
        message: str,
        *,
        platform: str | None = None,
        field: str | None = None,
        value: Any = None,
    ) -> None:
        super().__init__(message, platform=platform, field=field, value=value)
        self.field = field
        self.value = value


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class PipelineError(OumnoSupError):
    """Base class for failures in the normalise/validate/deduplicate pipeline."""


class NormalizationError(PipelineError):
    """Raised when a value cannot be converted to its canonical form.

    Args:
        message: Human-readable description.
        field: The field being normalised.
        value: The value that could not be converted.
    """

    def __init__(self, message: str, *, field: str | None = None, value: Any = None) -> None:
        super().__init__(message, field=field, value=value)
        self.field = field
        self.value = value


class RecordValidationError(PipelineError):
    """Raised when a record fails a plausibility or completeness rule.

    Named ``RecordValidationError`` rather than ``ValidationError`` so it is
    never confused with :class:`pydantic.ValidationError` at a call site.

    Args:
        message: Human-readable description.
        field: The field that failed, when the failure is field-specific.
        value: The offending value.
    """

    def __init__(self, message: str, *, field: str | None = None, value: Any = None) -> None:
        super().__init__(message, field=field, value=value)
        self.field = field
        self.value = value


class DeduplicationError(PipelineError):
    """Raised when conflicting duplicates cannot be merged automatically."""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class APIError(OumnoSupError):
    """Base class for errors surfaced through the public API.

    Attributes:
        status_code: HTTP status the error maps to.
    """

    status_code: int = 500


class ResourceNotFoundError(APIError):
    """Raised when a requested resource does not exist.

    Args:
        resource: The resource type, e.g. ``program``.
        identifier: The identifier that was looked up.
    """

    status_code = 404

    def __init__(self, resource: str, identifier: Any) -> None:
        super().__init__(f"{resource} {identifier!r} not found", resource=resource,
                         identifier=str(identifier))
        self.resource = resource
        self.identifier = identifier


class AuthenticationError(APIError):
    """Raised when a protected endpoint is called without valid credentials."""

    status_code = 401


class ScrapeAlreadyRunningError(APIError):
    """Raised when a scrape is triggered for a platform already being scraped.

    409 rather than 429: the request is not rate limited, it conflicts with
    work already in progress.

    Args:
        platform: The platform slug with a run in flight.
    """

    status_code = 409

    def __init__(self, platform: str) -> None:
        super().__init__(
            f"a scrape of {platform} is already running; follow it at "
            f"/api/v1/admin/scrape-runs",
            platform=platform,
        )
        self.platform = platform


__all__ = [
    "APIError",
    "AuthenticationError",
    "ConfigurationError",
    "DatabaseError",
    "DeduplicationError",
    "NormalizationError",
    "OumnoSupError",
    "ParserError",
    "PipelineError",
    "RateLimitedError",
    "RecordValidationError",
    "ResourceNotFoundError",
    "RobotsDisallowedError",
    "ScrapeAlreadyRunningError",
    "ScrapeHTTPError",
    "ScrapeTimeoutError",
    "ScraperError",
    "StructureChangedError",
]
