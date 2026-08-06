"""Programme endpoints, including its criteria and statistics."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from src.api.dependencies import PaginationDep, SessionDep
from src.core.exceptions import ResourceNotFoundError
from src.core.models import (
    AdmissionCriterion,
    AdmissionStatistic,
    Country,
    DegreeLevel,
    Program,
    University,
)
from src.core.schemas import (
    AdmissionCriterionRead,
    AdmissionStatisticRead,
    Page,
    ProgramDetail,
    ProgramRead,
)

router = APIRouter(prefix="/programs", tags=["programs"])


@router.get("", response_model=Page[ProgramRead], summary="List programmes")
async def list_programs(
    session: SessionDep,
    pagination: PaginationDep,
    country: str | None = Query(default=None, description="ISO 3166-1 alpha-2 code"),
    level: DegreeLevel | None = None,
    field: str | None = Query(default=None, description="Field of study"),
    region: str | None = None,
    city: str | None = None,
    academic_year: str | None = None,
    min_capacity: int | None = Query(default=None, ge=0),
    min_acceptance_rate: Decimal | None = Query(default=None, ge=0, le=1),
    q: str | None = Query(default=None, description="Match against the programme name"),
) -> Page[ProgramRead]:
    """List programmes, filtered on the criteria students actually search by.

    Args:
        session: Database session.
        pagination: Page window.
        country: Restrict to one country.
        level: Restrict to a degree level.
        field: Restrict to a field of study.
        region: Restrict to an institution region.
        city: Restrict to an institution city.
        academic_year: Restrict to one academic year.
        min_capacity: Only programmes offering at least this many places.
        min_acceptance_rate: Only programmes admitting at least this share of
            applicants, as a ratio between 0 and 1.
        q: Case-insensitive substring match on either the English or local name.

    Returns:
        A page of programmes, largest intake first.
    """
    stmt = select(Program).join(University)
    if country:
        stmt = stmt.join(Country, University.country_id == Country.id).where(
            Country.code_iso2 == country.upper()
        )
    if level is not None:
        stmt = stmt.where(Program.degree_level == level)
    if field:
        stmt = stmt.where(Program.field_of_study.ilike(f"%{field}%"))
    if region:
        stmt = stmt.where(University.region == region)
    if city:
        stmt = stmt.where(University.city == city)
    if academic_year:
        stmt = stmt.where(Program.academic_year == academic_year)
    if min_capacity is not None:
        stmt = stmt.where(Program.capacity >= min_capacity)
    if q:
        stmt = stmt.where(Program.name.ilike(f"%{q}%") | Program.name_local.ilike(f"%{q}%"))
    if min_acceptance_rate is not None:
        # EXISTS, not a join. Joining admission_statistics multiplies a programme
        # by its number of published years, which inflates `total` and repeats
        # the same programme across pages -- invisible today because each
        # programme has one year, and guaranteed to appear the moment a second
        # session is scraped.
        #
        # The correlation is on the programme's own academic year, so the filter
        # judges a programme by its current intake rather than matching one that
        # happened to be easy to enter several years ago.
        stmt = stmt.where(
            select(AdmissionStatistic.id)
            .where(
                AdmissionStatistic.program_id == Program.id,
                AdmissionStatistic.academic_year == Program.academic_year,
                AdmissionStatistic.acceptance_rate >= min_acceptance_rate,
            )
            .exists()
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.execute(
            stmt.order_by(Program.capacity.desc().nullslast(), Program.name)
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


@router.get("/{program_id}", response_model=ProgramDetail, summary="Get one programme")
async def get_program(program_id: UUID, session: SessionDep) -> ProgramDetail:
    """Fetch a programme with its institution, criteria and statistics.

    Args:
        program_id: The programme's UUID.
        session: Database session.

    Returns:
        The programme and its related records.

    Raises:
        ResourceNotFoundError: If no programme has that identifier.
    """
    program = (
        await session.execute(
            select(Program)
            .where(Program.id == program_id)
            .options(
                selectinload(Program.university),
                selectinload(Program.criteria),
                selectinload(Program.statistics),
            )
        )
    ).scalar_one_or_none()
    if program is None:
        raise ResourceNotFoundError("program", program_id)
    return ProgramDetail.model_validate(program)


@router.get(
    "/{program_id}/criteria",
    response_model=list[AdmissionCriterionRead],
    summary="Admission criteria for a programme",
)
async def get_program_criteria(
    program_id: UUID, session: SessionDep
) -> list[AdmissionCriterionRead]:
    """List a programme's admission criteria.

    Args:
        program_id: The programme's UUID.
        session: Database session.

    Returns:
        The criteria, mandatory ones first.

    Raises:
        ResourceNotFoundError: If no programme has that identifier.
    """
    if await session.get(Program, program_id) is None:
        raise ResourceNotFoundError("program", program_id)
    rows = (
        await session.execute(
            select(AdmissionCriterion)
            .where(AdmissionCriterion.program_id == program_id)
            .order_by(AdmissionCriterion.is_mandatory.desc())
        )
    ).scalars().all()
    return [AdmissionCriterionRead.model_validate(row) for row in rows]


@router.get(
    "/{program_id}/stats",
    response_model=list[AdmissionStatisticRead],
    summary="Admission statistics for a programme",
)
async def get_program_stats(
    program_id: UUID, session: SessionDep
) -> list[AdmissionStatisticRead]:
    """List a programme's published admission outcomes by year.

    Args:
        program_id: The programme's UUID.
        session: Database session.

    Returns:
        The statistics, most recent year first.

    Raises:
        ResourceNotFoundError: If no programme has that identifier.
    """
    if await session.get(Program, program_id) is None:
        raise ResourceNotFoundError("program", program_id)
    rows = (
        await session.execute(
            select(AdmissionStatistic)
            .where(AdmissionStatistic.program_id == program_id)
            .order_by(AdmissionStatistic.academic_year.desc())
        )
    ).scalars().all()
    return [AdmissionStatisticRead.model_validate(row) for row in rows]
