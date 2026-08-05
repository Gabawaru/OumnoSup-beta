"""University endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from src.api.dependencies import PaginationDep, SessionDep
from src.core.exceptions import ResourceNotFoundError
from src.core.models import Country, University, UniversityType
from src.core.schemas import Page, UniversityRead

router = APIRouter(prefix="/universities", tags=["universities"])


@router.get("", response_model=Page[UniversityRead], summary="List universities")
async def list_universities(
    session: SessionDep,
    pagination: PaginationDep,
    country: str | None = Query(default=None, description="ISO 3166-1 alpha-2 code"),
    city: str | None = None,
    region: str | None = None,
    type: UniversityType | None = None,
    q: str | None = Query(default=None, description="Match against the name"),
) -> Page[UniversityRead]:
    """List institutions, optionally filtered.

    Args:
        session: Database session.
        pagination: Page window.
        country: Restrict to one country.
        city: Restrict to one city.
        region: Restrict to one region.
        type: Restrict to an ownership type.
        q: Case-insensitive substring match on the institution name.

    Returns:
        A page of institutions, ordered by name.
    """
    stmt = select(University)
    if country:
        stmt = stmt.join(Country).where(Country.code_iso2 == country.upper())
    if city:
        stmt = stmt.where(University.city == city)
    if region:
        stmt = stmt.where(University.region == region)
    if type is not None:
        stmt = stmt.where(University.type == type)
    if q:
        stmt = stmt.where(University.name.ilike(f"%{q}%"))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.execute(
            stmt.order_by(University.name).offset(pagination.offset).limit(pagination.limit)
        )
    ).scalars().all()

    return Page.build(
        [UniversityRead.model_validate(row) for row in rows],
        total=total or 0,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{university_id}", response_model=UniversityRead, summary="Get one university")
async def get_university(university_id: UUID, session: SessionDep) -> UniversityRead:
    """Fetch an institution by identifier.

    Args:
        university_id: The institution's UUID.
        session: Database session.

    Returns:
        The institution.

    Raises:
        ResourceNotFoundError: If no institution has that identifier.
    """
    university = await session.get(University, university_id)
    if university is None:
        raise ResourceNotFoundError("university", university_id)
    return UniversityRead.model_validate(university)
