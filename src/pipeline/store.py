"""Persistence of pipeline output.

Writes through PostgreSQL's ``INSERT ... ON CONFLICT DO UPDATE`` against the
natural keys declared in :mod:`src.core.models`, in batches. Two reasons this is
not done with per-row ORM operations:

*Volume* — one Parcoursup run is 14,252 programmes plus their institutions and
statistics. Row-at-a-time flushing turns that into tens of thousands of
round trips.

*Idempotence* — the upsert targets the same unique constraints the deduplicator
uses, so re-running a scrape updates existing rows instead of failing or
duplicating. That property is what makes a scheduled refresh safe to run daily.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import DatabaseError
from src.core.models import AdmissionCriterion, AdmissionStatistic, Country, Program, University
from src.core.schemas import ScrapedProgram
from src.utils.logger import logger

__all__ = ["StoreResult", "store_programs"]

#: Rows per statement. Large enough to amortise round trips, small enough to keep
#: parameter counts and memory within PostgreSQL's limits.
BATCH_SIZE: Final[int] = 500


@dataclass(slots=True)
class StoreResult:
    """Counts from a storage run.

    Attributes:
        universities_upserted: Institutions inserted or updated.
        programs_inserted: Programmes newly created.
        programs_updated: Programmes that already existed and were refreshed.
        statistics_upserted: Statistics rows written.
        criteria_upserted: Criteria rows written.
    """

    universities_upserted: int = 0
    programs_inserted: int = 0
    programs_updated: int = 0
    statistics_upserted: int = 0
    criteria_upserted: int = 0


def _chunks(items: list[Any], size: int = BATCH_SIZE):
    """Yield successive slices of a list.

    Args:
        items: The list to slice.
        size: Maximum slice length.

    Yields:
        Successive sublists.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _resolve_country(session: AsyncSession, code: str) -> Country:
    """Fetch the country row for a scrape, creating a stub if absent.

    A scraper must be able to run before anyone has curated the country table,
    so a minimal row is created rather than failing the run. Curated fields are
    left untouched if the row already exists.

    Args:
        session: Open database session.
        code: ISO 3166-1 alpha-2 code.

    Returns:
        The persisted country.

    Raises:
        DatabaseError: If the country can neither be found nor created.
    """
    country = (
        await session.execute(select(Country).where(Country.code_iso2 == code))
    ).scalar_one_or_none()
    if country is not None:
        return country

    # Minimal placeholder; the ISO3 and names are filled in by whoever curates
    # the reference data. Only FR is known to this build.
    known = {"FR": ("FRA", "France", "France", "fr", "Parcoursup",
                    "https://www.parcoursup.gouv.fr")}
    iso3, name, local, lang, platform, url = known.get(
        code, (code + "X", code, code, "en", None, None)
    )
    country = Country(
        code_iso2=code, code_iso3=iso3, name=name, name_local=local, language=lang,
        admission_platform_name=platform, admission_platform_url=url,
        scraping_enabled=True,
    )
    session.add(country)
    try:
        await session.flush()
    except Exception as exc:
        raise DatabaseError(f"could not create country {code}", code=code) from exc
    logger.info("created placeholder country row for {}", code)
    return country


async def _upsert_universities(
    session: AsyncSession, programs: list[ScrapedProgram], country_id: UUID
) -> tuple[dict[str, UUID], int]:
    """Insert or update every institution referenced by the batch.

    Args:
        session: Open database session.
        programs: The programmes being stored.
        country_id: Owning country.

    Returns:
        A mapping from institution match key to identifier, and the number of
        rows written.
    """
    # One row per distinct institution. external_id is the stable key where the
    # platform publishes one; otherwise fall back to the name.
    distinct: dict[str, dict[str, Any]] = {}
    for program in programs:
        uni = program.university
        key = uni.external_id or uni.name
        if key not in distinct:
            distinct[key] = {
                "country_id": country_id,
                "external_id": uni.external_id,
                "name": uni.name,
                "name_local": uni.name_local,
                "city": uni.city,
                "region": uni.region,
                "website_url": uni.website_url,
                "type": uni.type,
                "accreditation_body": uni.accreditation_body,
                "founding_year": uni.founding_year,
                "student_count": uni.student_count,
                "international_student_count": uni.international_student_count,
            }

    rows = list(distinct.values())
    written = 0
    for batch in _chunks(rows):
        stmt = pg_insert(University).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=[University.country_id, University.external_id],
            set_={
                "name": stmt.excluded.name,
                "name_local": stmt.excluded.name_local,
                "city": stmt.excluded.city,
                "region": stmt.excluded.region,
                "type": stmt.excluded.type,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        await session.execute(stmt)
        written += len(batch)

    # Read identifiers back for the whole country in one query.
    result = await session.execute(
        select(University.id, University.external_id, University.name).where(
            University.country_id == country_id
        )
    )
    mapping: dict[str, UUID] = {}
    for uid, external_id, name in result:
        if external_id:
            mapping[external_id] = uid
        mapping.setdefault(name, uid)
    return mapping, written


async def store_programs(
    session: AsyncSession, programs: list[ScrapedProgram], *, country_code: str
) -> StoreResult:
    """Persist a batch of programmes and everything hanging off them.

    Safe to call repeatedly with the same input: every write targets a unique
    constraint and updates in place on conflict.

    Args:
        session: Open database session. The caller owns the transaction.
        programs: Validated, deduplicated programmes.
        country_code: ISO 3166-1 alpha-2 code the batch belongs to.

    Returns:
        Counts describing what was written.

    Raises:
        DatabaseError: If the country cannot be resolved.
    """
    result = StoreResult()
    if not programs:
        return result

    country = await _resolve_country(session, country_code)
    uni_ids, result.universities_upserted = await _upsert_universities(
        session, programs, country.id
    )

    # Which natural keys already exist? Needed to report inserted vs updated
    # honestly — ON CONFLICT alone cannot tell the two apart in a batch.
    existing = {
        (uid, key, year)
        for uid, key, year in (
            await session.execute(
                select(Program.university_id, Program.source_key, Program.academic_year).where(
                    Program.source_platform == programs[0].source_platform
                )
            )
        )
    }

    program_rows: list[dict[str, Any]] = []
    for program in programs:
        uni = program.university
        university_id = uni_ids.get(uni.external_id or "") or uni_ids.get(uni.name)
        if university_id is None:
            continue
        key = (university_id, program.source_key, program.academic_year)
        if key in existing:
            result.programs_updated += 1
        else:
            result.programs_inserted += 1
        program_rows.append(
            {
                "id": uuid4(),
                "university_id": university_id,
                "external_id": program.external_id,
                "source_key": program.source_key,
                "name": program.name,
                "name_local": program.name_local,
                "degree_level": program.degree_level,
                "field_of_study": program.field_of_study,
                "field_code": program.field_code,
                "language_of_instruction": program.language_of_instruction,
                "duration_years": program.duration_years,
                "duration_semesters": program.duration_semesters,
                "tuition_fee": program.tuition_fee,
                "currency": program.currency,
                "application_fee": program.application_fee,
                "capacity": program.capacity,
                "application_deadline": program.application_deadline,
                "application_open_date": program.application_open_date,
                "academic_year": program.academic_year,
                "is_international_program": program.is_international_program,
                "requires_visa": program.requires_visa,
                "source_platform": program.source_platform,
                "source_url": program.source_url,
                "raw_data": program.raw_data,
                "_stats": program.statistics,
                "_criteria": program.criteria,
            }
        )

    # Write programmes, then read back the identifiers the natural keys resolved
    # to, so children attach to the right parent whether it was new or existing.
    for batch in _chunks(program_rows):
        payload = [{k: v for k, v in row.items() if not k.startswith("_")} for row in batch]
        stmt = pg_insert(Program).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Program.university_id, Program.source_key, Program.academic_year],
            set_={
                "name": stmt.excluded.name,
                "name_local": stmt.excluded.name_local,
                "external_id": stmt.excluded.external_id,
                "degree_level": stmt.excluded.degree_level,
                "field_of_study": stmt.excluded.field_of_study,
                "field_code": stmt.excluded.field_code,
                "capacity": stmt.excluded.capacity,
                "duration_years": stmt.excluded.duration_years,
                "source_url": stmt.excluded.source_url,
                "raw_data": stmt.excluded.raw_data,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        await session.execute(stmt)

    keys = {(r["university_id"], r["source_key"], r["academic_year"]): r for r in program_rows}
    resolved = await session.execute(
        select(Program.id, Program.university_id, Program.source_key, Program.academic_year)
        .where(Program.source_platform == programs[0].source_platform)
    )
    program_ids: dict[tuple[UUID, str, str], UUID] = {
        (uid, key, year): pid for pid, uid, key, year in resolved
    }

    stat_rows: list[dict[str, Any]] = []
    crit_rows: list[dict[str, Any]] = []
    for key, row in keys.items():
        program_id = program_ids.get(key)
        if program_id is None:
            continue
        for stat in row["_stats"]:
            stat_rows.append(
                {
                    "id": uuid4(), "program_id": program_id,
                    "academic_year": stat.academic_year,
                    "applicants_count": stat.applicants_count,
                    "admitted_count": stat.admitted_count,
                    "waitlist_count": stat.waitlist_count,
                    "rejected_count": stat.rejected_count,
                    "min_score_admitted": stat.min_score_admitted,
                    "max_score_admitted": stat.max_score_admitted,
                    "avg_score_admitted": stat.avg_score_admitted,
                    "acceptance_rate": stat.acceptance_rate,
                }
            )
        for crit in row["_criteria"]:
            crit_rows.append(
                {
                    "id": uuid4(), "program_id": program_id,
                    "criterion_type": crit.criterion_type,
                    "description": crit.description,
                    "minimum_score": crit.minimum_score,
                    "maximum_score": crit.maximum_score,
                    "weight": crit.weight,
                    "is_mandatory": crit.is_mandatory,
                }
            )

    for batch in _chunks(stat_rows):
        stmt = pg_insert(AdmissionStatistic).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AdmissionStatistic.program_id, AdmissionStatistic.academic_year],
            set_={
                "applicants_count": stmt.excluded.applicants_count,
                "admitted_count": stmt.excluded.admitted_count,
                "acceptance_rate": stmt.excluded.acceptance_rate,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        await session.execute(stmt)
        result.statistics_upserted += len(batch)

    # Criteria carry no natural key, so a re-run replaces the set for the
    # programmes in this batch rather than accumulating duplicates.
    if crit_rows:
        touched = {row["program_id"] for row in crit_rows}
        for batch in _chunks(sorted(touched, key=str)):
            await session.execute(
                AdmissionCriterion.__table__.delete().where(
                    AdmissionCriterion.program_id.in_(batch)
                )
            )
        for batch in _chunks(crit_rows):
            await session.execute(pg_insert(AdmissionCriterion).values(batch))
            result.criteria_upserted += len(batch)

    logger.info(
        "stored {} universities, {} programmes ({} new, {} updated), {} statistics",
        result.universities_upserted, len(program_rows),
        result.programs_inserted, result.programs_updated, result.statistics_upserted,
    )
    return result
