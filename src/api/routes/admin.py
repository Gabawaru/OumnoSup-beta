"""Administration endpoints. Every route here requires the admin token."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from src.api.dependencies import PaginationDep, SessionDep, require_admin
from src.core.exceptions import ResourceNotFoundError
from src.core.models import ScrapeRun
from src.core.schemas import Page, ScrapeRunRead
from src.utils.logger import logger

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

#: Scrapers that can be triggered through the API, by platform slug.
#:
#: Imported lazily inside the handler: importing every scraper at module load
#: would pull each country's dependencies into the API process.
_AVAILABLE_PLATFORMS: dict[str, tuple[str, str]] = {
    "parcoursup": ("src.scrapers.france.parcoursup", "ParcoursupScraper"),
}


@router.get("/scrape-runs", response_model=Page[ScrapeRunRead], summary="List scrape runs")
async def list_scrape_runs(
    session: SessionDep,
    pagination: PaginationDep,
    platform: str | None = None,
) -> Page[ScrapeRunRead]:
    """List scraper executions, most recent first.

    Args:
        session: Database session.
        pagination: Page window.
        platform: Restrict to one platform slug.

    Returns:
        A page of run records.
    """
    stmt = select(ScrapeRun)
    if platform:
        stmt = stmt.where(ScrapeRun.platform == platform)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.execute(
            stmt.order_by(ScrapeRun.started_at.desc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    ).scalars().all()

    return Page.build(
        [ScrapeRunRead.model_validate(row) for row in rows],
        total=total or 0,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("/scrape/{platform}", status_code=202, summary="Trigger a scrape")
async def trigger_scrape(platform: str, limit: int | None = None) -> dict[str, Any]:
    """Start a scrape and return immediately.

    A national run takes minutes, far longer than any sane HTTP timeout, so the
    work is scheduled on the event loop and the caller is handed a 202. Progress
    is observable through ``GET /admin/scrape-runs``.

    Args:
        platform: Platform slug, e.g. ``parcoursup``.
        limit: Optional cap on records processed, for smoke runs.

    Returns:
        An acknowledgement naming the platform that was started.

    Raises:
        ResourceNotFoundError: If no scraper is registered for that slug.
    """
    entry = _AVAILABLE_PLATFORMS.get(platform)
    if entry is None:
        raise ResourceNotFoundError("scraper", platform)

    module_path, class_name = entry

    async def _run() -> None:
        """Execute the pipeline for the requested platform."""
        import importlib

        from src.pipeline.runner import run_pipeline

        scraper_cls = getattr(importlib.import_module(module_path), class_name)
        try:
            await run_pipeline(scraper_cls(persist_run=True, limit=limit))
        except Exception as exc:
            logger.error("triggered scrape of {} failed: {}", platform, exc)

    asyncio.create_task(_run())
    return {
        "status": "accepted",
        "platform": platform,
        "limit": limit,
        "follow": "/api/v1/admin/scrape-runs",
    }


@router.get("/platforms", summary="List triggerable platforms")
async def list_platforms() -> dict[str, list[str]]:
    """List the platform slugs that can be scraped through the API.

    Returns:
        The available slugs.
    """
    return {"platforms": sorted(_AVAILABLE_PLATFORMS)}
