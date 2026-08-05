"""Structured logging built on Loguru.

Development gets a colourised, human-readable sink. Production emits one JSON
object per line so a log shipper can index fields without regex parsing.

Call :func:`configure_logging` once at process start (API lifespan, scheduler
boot, scraper ``main``); everywhere else just ``from src.utils.logger import
logger``.
"""

from __future__ import annotations

import sys
from typing import Any

from loguru import logger

from src.core.config import get_settings

__all__ = ["configure_logging", "logger", "log_request"]

_HUMAN_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[platform]}</cyan> | "
    "<level>{message}</level>"
)

_configured = False


def configure_logging(*, force: bool = False) -> None:
    """Install the project's log sinks.

    Idempotent: repeated calls are ignored unless ``force`` is set, so importing
    a scraper from a test does not stack duplicate sinks.

    Args:
        force: Reinstall the sinks even if logging was already configured.
    """
    global _configured
    if _configured and not force:
        return

    settings = get_settings()
    logger.remove()

    # `platform` is bound globally so the human format can always reference it;
    # scrapers rebind it to their own slug via logger.bind(platform=...).
    logger.configure(extra={"platform": "oumnosup"})

    if settings.is_production:
        # serialize=True emits the record as JSON, including bound extras.
        logger.add(sys.stdout, level=settings.log_level, serialize=True, backtrace=False,
                   diagnose=False)
    else:
        # diagnose=False even in development: tracebacks would otherwise render
        # local variables, and those can hold credentials read from settings.
        logger.add(sys.stderr, level=settings.log_level, format=_HUMAN_FORMAT,
                   colorize=True, backtrace=True, diagnose=False)

    _configured = True


def log_request(
    *,
    url: str,
    method: str = "GET",
    status_code: int | None = None,
    duration_ms: float | None = None,
    size_bytes: int | None = None,
    **extra: Any,
) -> None:
    """Emit one structured record for an outbound HTTP request.

    Requirement 4 of the BaseScraper contract: every request is logged with its
    URL, status, duration and size.

    Args:
        url: The URL requested.
        method: HTTP method.
        status_code: Response status, or ``None`` if the request never completed.
        duration_ms: Wall-clock duration in milliseconds.
        size_bytes: Response body size in bytes.
        **extra: Further fields bound onto the record.
    """
    logger.bind(
        url=url,
        method=method,
        status_code=status_code,
        duration_ms=round(duration_ms, 1) if duration_ms is not None else None,
        size_bytes=size_bytes,
        **extra,
    ).info(
        "{method} {url} -> {status} in {ms}ms ({size})",
        method=method,
        url=url,
        status=status_code if status_code is not None else "ERR",
        ms=round(duration_ms, 1) if duration_ms is not None else "?",
        size=f"{size_bytes:,}B" if size_bytes is not None else "?",
    )
