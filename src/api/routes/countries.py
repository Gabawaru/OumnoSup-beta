"""Country endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from src.api.dependencies import PaginationDep, SessionDep
from src.core.exceptions import ResourceNotFoundError
from src.core.models import Country
from src.core.schemas import CountryRead, Page

router = APIRouter(prefix="/countries", tags=["countries"])


@router.get("", response_model=Page[CountryRead], summary="List countries")
async def list_countries(
    session: SessionDep,
    pagination: PaginationDep,
    scraping_enabled: bool | None = None,
) -> Page[CountryRead]:
    """List countries covered by the platform.

    Args:
        session: Database session.
        pagination: Page window.
        scraping_enabled: Restrict to countries whose scraping is on or off.

    Returns:
        A page of countries, ordered by name.
    """
    stmt = select(Country)
    if scraping_enabled is not None:
        stmt = stmt.where(Country.scraping_enabled == scraping_enabled)

    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    )
    rows = (
        await session.execute(
            stmt.order_by(Country.name).offset(pagination.offset).limit(pagination.limit)
        )
    ).scalars().all()

    return Page.build(
        [CountryRead.model_validate(row) for row in rows],
        total=total or 0,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{code}", response_model=CountryRead, summary="Get one country")
async def get_country(code: str, session: SessionDep) -> CountryRead:
    """Fetch a country by its ISO 3166-1 alpha-2 code.

    Args:
        code: Two-letter country code, case-insensitive.
        session: Database session.

    Returns:
        The country.

    Raises:
        ResourceNotFoundError: If no country carries that code.
    """
    country = (
        await session.execute(
            select(Country).where(Country.code_iso2 == code.upper())
        )
    ).scalar_one_or_none()
    if country is None:
        raise ResourceNotFoundError("country", code.upper())
    return CountryRead.model_validate(country)
