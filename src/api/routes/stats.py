"""Aggregate statistics and cross-resource search."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from src.api.dependencies import PaginationDep, SessionDep
from src.core.models import AdmissionStatistic, Country, Program, ScrapeRun, University
from src.core.schemas import Page, ProgramRead

router = APIRouter(tags=["stats"])


@router.get("/stats/global", summary="Platform-wide totals")
async def global_stats(session: SessionDep) -> dict[str, Any]:
    """Return headline totals across everything stored.

    Args:
        session: Database session.

    Returns:
        Counts by resource, total advertised places, and per-country coverage.
    """
    countries = await session.scalar(select(func.count()).select_from(Country))
    universities = await session.scalar(select(func.count()).select_from(University))
    programs = await session.scalar(select(func.count()).select_from(Program))
    places = await session.scalar(select(func.coalesce(func.sum(Program.capacity), 0)))
    runs = await session.scalar(select(func.count()).select_from(ScrapeRun))

    per_country = (
        await session.execute(
            select(
                Country.code_iso2,
                Country.name,
                Country.last_scrape_at,
                func.count(Program.id).label("programs"),
            )
            .select_from(Country)
            .outerjoin(University, University.country_id == Country.id)
            .outerjoin(Program, Program.university_id == University.id)
            .group_by(Country.code_iso2, Country.name, Country.last_scrape_at)
            .order_by(func.count(Program.id).desc())
        )
    ).all()

    return {
        "countries": countries or 0,
        "universities": universities or 0,
        "programs": programs or 0,
        "total_places": int(places or 0),
        "scrape_runs": runs or 0,
        "coverage": [
            {
                "country_code": code,
                "country": name,
                "programs": count,
                "last_scrape_at": last.isoformat() if last else None,
            }
            for code, name, last, count in per_country
        ],
    }


@router.get("/search", response_model=Page[ProgramRead], summary="Search programmes")
async def search(
    session: SessionDep,
    pagination: PaginationDep,
    q: str = Query(min_length=2, description="Search term"),
    country: str | None = Query(default=None, description="ISO 3166-1 alpha-2 code"),
) -> Page[ProgramRead]:
    """Search programmes by name, institution or city.

    Args:
        session: Database session.
        pagination: Page window.
        q: Search term, at least two characters.
        country: Restrict to one country.

    Returns:
        A page of matching programmes, largest intake first.
    """
    pattern = f"%{q}%"
    stmt = (
        select(Program)
        .join(University)
        .where(
            Program.name.ilike(pattern)
            | Program.name_local.ilike(pattern)
            | University.name.ilike(pattern)
            | University.city.ilike(pattern)
        )
    )
    if country:
        stmt = stmt.join(Country, University.country_id == Country.id).where(
            Country.code_iso2 == country.upper()
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.execute(
            stmt.order_by(Program.capacity.desc().nullslast())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    ).scalars().all()

    return Page.build(
        [ProgramRead.model_validate(row) for row in rows],
        total=total or 0,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/stats/selectivity", summary="Least and most selective programmes")
async def selectivity(
    session: SessionDep,
    country: str | None = Query(default=None, description="ISO 3166-1 alpha-2 code"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Return the programmes at each end of the acceptance-rate range.

    Only programmes with a meaningful applicant pool are considered: a course
    with three applicants and a 100% rate is noise, not an opportunity.

    Args:
        session: Database session.
        country: Restrict to one country.
        limit: How many programmes to return at each end.

    Returns:
        The least and most selective programmes.
    """
    base = (
        select(
            Program.name,
            University.name.label("university"),
            University.city,
            Program.capacity,
            AdmissionStatistic.applicants_count,
            AdmissionStatistic.acceptance_rate,
        )
        .join(University, University.id == Program.university_id)
        .join(AdmissionStatistic, AdmissionStatistic.program_id == Program.id)
        .where(
            AdmissionStatistic.acceptance_rate.is_not(None),
            AdmissionStatistic.applicants_count >= 100,
        )
    )
    if country:
        base = base.join(Country, University.country_id == Country.id).where(
            Country.code_iso2 == country.upper()
        )

    def _rows(result) -> list[dict[str, Any]]:
        return [
            {
                "program": name,
                "university": university,
                "city": city,
                "capacity": capacity,
                "applicants": applicants,
                "acceptance_rate": float(rate),
            }
            for name, university, city, capacity, applicants, rate in result
        ]

    least = await session.execute(
        base.order_by(AdmissionStatistic.acceptance_rate.desc()).limit(limit)
    )
    most = await session.execute(
        base.order_by(AdmissionStatistic.acceptance_rate.asc()).limit(limit)
    )
    return {"least_selective": _rows(least), "most_selective": _rows(most)}
