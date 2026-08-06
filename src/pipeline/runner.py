"""The pipeline itself: the one place the mandated flow is expressed.

    scrape -> parse -> normalize -> validate -> deduplicate -> store

Scrapers produce items; this module is the only thing that writes them. Keeping
storage out of the scrapers is what makes "no raw payload reaches the database"
enforceable rather than aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from src.core.database import session_scope
from src.core.models import Country, ScrapeRun, ScrapeStatus
from src.core.schemas import ScrapedProgram
from src.pipeline.deduplicator import deduplicate
from src.pipeline.normalizer import (
    convert_to_eur,
    normalize_academic_year,
    normalize_institution_name,
)
from src.pipeline.store import StoreResult, store_programs
from src.pipeline.translator import Translator, build_translator
from src.pipeline.validator import validate_many
from src.scrapers.base import BaseScraper
from src.utils.logger import logger

__all__ = ["PipelineResult", "process_items", "run_pipeline"]


@dataclass(slots=True)
class PipelineResult:
    """Outcome of one end-to-end pipeline execution.

    Attributes:
        scraped: Source records retrieved.
        parsed: Records successfully parsed into items.
        rejected: Items dropped by validation.
        duplicates_merged: Exact duplicates collapsed.
        near_duplicates: Suspected duplicates reported but not merged.
        stored: Counts from the storage stage.
        status: Final status recorded on the run.
    """

    scraped: int = 0
    parsed: int = 0
    rejected: int = 0
    duplicates_merged: int = 0
    near_duplicates: int = 0
    stored: StoreResult = field(default_factory=StoreResult)
    status: ScrapeStatus = ScrapeStatus.RUNNING


def _normalize(program: ScrapedProgram, translator: Translator) -> ScrapedProgram:
    """Apply canonicalisation and translation to one item.

    Args:
        program: The parsed programme.
        translator: Backend used to produce the English name.

    Returns:
        A new item with canonical values.
    """
    data = program.model_dump()
    data["academic_year"] = normalize_academic_year(program.academic_year)
    data["university"]["name"] = normalize_institution_name(program.university.name)

    # `name` is the English form; `name_local` always keeps the original.
    data["name"] = translator.translate(program.name_local, source_language="fr")
    data["name_local"] = program.name_local.strip()

    # Money is stored in euros so figures compare across countries.
    if program.tuition_fee is not None and program.currency:
        data["tuition_fee"] = convert_to_eur(program.tuition_fee, program.currency)
        data["currency"] = "EUR"
    if program.application_fee is not None and program.currency:
        data["application_fee"] = convert_to_eur(program.application_fee, program.currency)
        data["currency"] = "EUR"

    for stat in data["statistics"]:
        stat["academic_year"] = normalize_academic_year(stat["academic_year"])

    return ScrapedProgram.model_validate(data)


def process_items(
    programs: list[ScrapedProgram], *, translate: bool = True, detect_near: bool = True
) -> tuple[list[ScrapedProgram], PipelineResult]:
    """Run the in-memory stages: normalize, validate, deduplicate.

    Separated from :func:`run_pipeline` so the transformation can be tested
    without a database or a network.

    Args:
        programs: Items straight from a scraper.
        translate: Whether to produce English names.
        detect_near: Whether to look for near-duplicates.

    Returns:
        The items ready for storage, and a partially filled result.
    """
    result = PipelineResult(parsed=len(programs))
    translator = build_translator(enabled=translate)

    normalized: list[ScrapedProgram] = []
    for program in programs:
        try:
            normalized.append(_normalize(program, translator))
        except Exception as exc:
            result.rejected += 1
            logger.debug("normalisation dropped a record: {}", exc)

    report = validate_many(normalized)
    result.rejected += len(report.rejected)
    for warning in report.warnings:
        logger.warning("validation: {}", warning)

    deduped = deduplicate(report.accepted, detect_near=detect_near)
    result.duplicates_merged = deduped.exact_merged
    result.near_duplicates = len(deduped.near_duplicates)

    return deduped.unique, result


async def run_pipeline(
    scraper: BaseScraper, *, translate: bool = True, detect_near: bool = False
) -> PipelineResult:
    """Scrape a platform and store the result.

    Args:
        scraper: A configured scraper instance.
        translate: Whether to produce English names.
        detect_near: Whether to look for near-duplicates. Off by default here:
            the search is quadratic per institution and adds little on a full
            national run.

    Returns:
        The pipeline outcome.
    """
    scrape = await scraper.run()

    items, result = process_items(
        scrape.items, translate=translate, detect_near=detect_near
    )
    result.scraped = scrape.scraped

    async with session_scope() as session:
        result.stored = await store_programs(
            session, items, country_code=scraper.country_code
        )

        # Reflect what storage actually did onto the audit row the scraper opened,
        # and stamp the country's freshness.
        if scrape.run_id is not None:
            run = await session.get(ScrapeRun, scrape.run_id)
            if run is not None:
                run.records_inserted = result.stored.programs_inserted
                run.records_updated = result.stored.programs_updated
                run.records_failed = scrape.failed + result.rejected
                run.status = (
                    ScrapeStatus.PARTIAL
                    if (scrape.failed or result.rejected)
                    else ScrapeStatus.SUCCESS
                )
                result.status = run.status

        country = (
            await session.execute(
                select(Country).where(Country.code_iso2 == scraper.country_code)
            )
        ).scalar_one_or_none()
        if country is not None:
            country.last_scrape_at = datetime.now(UTC)

    logger.info(
        "pipeline complete: {} scraped, {} stored ({} new, {} updated), {} rejected",
        result.scraped, result.stored.programs_inserted + result.stored.programs_updated,
        result.stored.programs_inserted, result.stored.programs_updated, result.rejected,
    )
    return result
