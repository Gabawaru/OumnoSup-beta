"""Scheduled refresh of every enabled platform.

One job per country, on a cadence read from ``countries.scraping_frequency``, so
adding a country to the schedule is a database change rather than a deployment.

A country that fails never affects the others: each job is independent and its
failure is recorded in ``scrape_runs``.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from src.core.database import dispose_engine, session_scope
from src.core.models import Country, ScrapingFrequency
from src.utils.logger import configure_logging, logger

__all__ = ["SCRAPERS", "build_scheduler", "main", "refresh_platform"]

#: Platform slug -> (module path, class name). Imported lazily so the scheduler
#: process does not load every country's dependencies at startup.
SCRAPERS: Final[dict[str, tuple[str, str]]] = {
    "parcoursup": ("src.scrapers.france.parcoursup", "ParcoursupScraper"),
}

#: When each frequency fires. Times are staggered rather than all at midnight so
#: several platforms do not hit their sources — or our database — at once.
_TRIGGERS: Final[dict[ScrapingFrequency, CronTrigger]] = {
    ScrapingFrequency.DAILY: CronTrigger(hour=3, minute=15),
    ScrapingFrequency.WEEKLY: CronTrigger(day_of_week="sun", hour=4, minute=15),
    ScrapingFrequency.MONTHLY: CronTrigger(day=1, hour=5, minute=15),
    # Admission data changes in bursts around the national calendar, so a
    # seasonal platform is refreshed at the start of each quarter.
    ScrapingFrequency.SEASONAL: CronTrigger(month="1,4,7,10", day=1, hour=6, minute=15),
}


async def refresh_platform(platform: str) -> None:
    """Run one platform's scraper through the pipeline.

    Args:
        platform: Platform slug, e.g. ``parcoursup``.
    """
    entry = SCRAPERS.get(platform)
    if entry is None:
        logger.error("no scraper registered for platform {}", platform)
        return

    import importlib

    from src.pipeline.runner import run_pipeline

    module_path, class_name = entry
    scraper_cls = getattr(importlib.import_module(module_path), class_name)

    logger.info("scheduled refresh of {} starting", platform)
    try:
        result = await run_pipeline(scraper_cls(persist_run=True))
    except Exception as exc:
        # Contained on purpose: one platform failing must not stop the others,
        # and the failure is already recorded in scrape_runs by BaseScraper.
        logger.error("scheduled refresh of {} failed: {}", platform, exc)
        return
    logger.info(
        "scheduled refresh of {} finished: {} new, {} updated",
        platform, result.stored.programs_inserted, result.stored.programs_updated,
    )

    # Clear the API's cached responses. The scheduler is a separate process, but
    # the cache lives in Redis, so dropping the keys here is what makes the API
    # serve the data this run just wrote.
    from src.api.cache import get_cache

    await get_cache().invalidate()


async def _enabled_countries() -> list[tuple[str, str | None, ScrapingFrequency]]:
    """Read the countries whose scraping is switched on.

    Returns:
        ``(country_code, platform_slug, frequency)`` for each enabled country.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Country.code_iso2,
                    Country.admission_platform_name,
                    Country.scraping_frequency,
                ).where(Country.scraping_enabled.is_(True))
            )
        ).all()
    return [(code, (name or "").lower() or None, freq) for code, name, freq in rows]


async def build_scheduler() -> AsyncIOScheduler:
    """Create a scheduler with one job per enabled country.

    Returns:
        The configured, not-yet-started scheduler.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    for code, platform, frequency in await _enabled_countries():
        if platform not in SCRAPERS:
            logger.warning(
                "country {} is enabled but has no scraper for platform {!r}; skipping",
                code, platform,
            )
            continue
        trigger = _TRIGGERS[frequency]
        scheduler.add_job(
            refresh_platform,
            trigger=trigger,
            args=[platform],
            id=f"refresh-{platform}",
            name=f"Refresh {platform} ({code})",
            replace_existing=True,
            # A national run takes minutes; overlapping runs would fight over the
            # same rows and double the load on the source.
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("scheduled {} ({}) on the {} cadence", platform, code, frequency.value)

    if not scheduler.get_jobs():
        logger.warning(
            "no scheduled jobs: enable a country with scraping_enabled = true "
            "and a platform name matching a registered scraper"
        )
    return scheduler


async def main() -> None:
    """Run the scheduler until the process is asked to stop."""
    configure_logging()
    scheduler = await build_scheduler()
    scheduler.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Containers are stopped with SIGTERM; shutting down cleanly lets an
        # in-flight run finish writing its audit row.
        loop.add_signal_handler(sig, stop.set)

    logger.info("scheduler running with {} job(s)", len(scheduler.get_jobs()))
    await stop.wait()

    logger.info("scheduler stopping")
    scheduler.shutdown(wait=True)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
