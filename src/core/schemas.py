"""Pydantic schemas.

Two distinct families live here, kept in separate sections:

**API read schemas** (``*Read``, ``*Detail``) serialise ORM objects into
responses. They are built with ``from_attributes=True`` and never accept input.

**Scraped item schemas** (``Scraped*``) are the contract between a scraper and
the pipeline. A scraper never returns a raw ``dict``: it returns validated
instances, so a field that a platform renamed fails at the scraper boundary
rather than three stages later as a mysterious ``None``.

Enumerations are imported from :mod:`src.core.models` rather than redeclared, so
there is exactly one definition of what ``degree_level`` may contain.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Generic, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from src.core.models import (
    ApplicationStatus,
    CriterionType,
    DegreeLevel,
    EducationLevel,
    ScrapeStatus,
    ScrapingFrequency,
    UniversityType,
)

__all__ = [
    "AdmissionCriterionRead",
    "AdmissionStatisticRead",
    "CountryRead",
    "Page",
    "ProgramDetail",
    "ProgramRead",
    "ScrapeRunRead",
    "ScrapedCriterion",
    "ScrapedProgram",
    "ScrapedStatistic",
    "ScrapedUniversity",
    "UniversityRead",
    "UniversitySummary",
]

#: ISO 639-1 language code.
LanguageCode = Annotated[str, Field(min_length=2, max_length=2, pattern=r"^[a-z]{2}$")]
#: ISO 4217 currency code.
CurrencyCode = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
#: Academic year in ``YYYY-YYYY`` form.
AcademicYear = Annotated[str, Field(min_length=9, max_length=9, pattern=r"^\d{4}-\d{4}$")]


# ===========================================================================
# API read schemas
# ===========================================================================


class _ReadModel(BaseModel):
    """Base for response schemas built from ORM instances."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class CountryRead(_ReadModel):
    """A country and its national admission platform."""

    id: UUID
    code_iso2: str
    code_iso3: str
    name: str
    name_local: str | None = None
    admission_platform_name: str | None = None
    admission_platform_url: str | None = None
    language: str
    academic_year_format: str | None = None
    scraping_enabled: bool
    scraping_frequency: ScrapingFrequency
    last_scrape_at: datetime | None = None


class UniversitySummary(_ReadModel):
    """Compact institution reference, embedded inside programme responses."""

    id: UUID
    name: str
    name_local: str | None = None
    city: str | None = None
    country_id: UUID


class UniversityRead(_ReadModel):
    """An institution."""

    id: UUID
    country_id: UUID
    external_id: str | None = None
    name: str
    name_local: str | None = None
    city: str | None = None
    region: str | None = None
    website_url: str | None = None
    type: UniversityType | None = None
    accreditation_body: str | None = None
    founding_year: int | None = None
    student_count: int | None = None
    international_student_count: int | None = None
    oumno_partner: bool


class AdmissionCriterionRead(_ReadModel):
    """One admission requirement."""

    id: UUID
    program_id: UUID
    criterion_type: CriterionType
    description: str | None = None
    minimum_score: str | None = None
    maximum_score: str | None = None
    weight: Decimal | None = None
    is_mandatory: bool


class AdmissionStatisticRead(_ReadModel):
    """Published admission outcomes for one academic year."""

    id: UUID
    program_id: UUID
    academic_year: str
    applicants_count: int | None = None
    admitted_count: int | None = None
    waitlist_count: int | None = None
    rejected_count: int | None = None
    min_score_admitted: Decimal | None = None
    max_score_admitted: Decimal | None = None
    avg_score_admitted: Decimal | None = None
    acceptance_rate: Decimal | None = None


class ProgramRead(_ReadModel):
    """A programme, as returned by list endpoints.

    ``raw_data`` is deliberately absent: it is a provenance artefact for the
    pipeline, sometimes large, and of no use to an API consumer.
    """

    id: UUID
    university_id: UUID
    external_id: str | None = None
    name: str
    name_local: str
    degree_level: DegreeLevel
    field_of_study: str | None = None
    field_code: str | None = None
    language_of_instruction: list[str]
    duration_years: Decimal | None = None
    duration_semesters: int | None = None
    tuition_fee: Decimal | None = None
    currency: str | None = None
    application_fee: Decimal | None = None
    capacity: int | None = None
    application_deadline: date | None = None
    application_open_date: date | None = None
    academic_year: str
    is_international_program: bool
    requires_visa: bool
    source_platform: str
    source_url: str | None = None


class ProgramDetail(ProgramRead):
    """A programme with its criteria, statistics and institution.

    Returned by ``GET /api/v1/programs/{id}``.
    """

    university: UniversitySummary | None = None
    criteria: list[AdmissionCriterionRead] = Field(default_factory=list)
    statistics: list[AdmissionStatisticRead] = Field(default_factory=list)


class ScrapeRunRead(_ReadModel):
    """Audit record of one scraper execution."""

    id: UUID
    platform: str
    country_code: str
    started_at: datetime
    finished_at: datetime | None = None
    status: ScrapeStatus
    records_scraped: int
    records_inserted: int
    records_updated: int
    records_failed: int
    error_log: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock duration of the run, or ``None`` while still running."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Paginated envelope shared by every list endpoint.

    Attributes:
        items: The page of results.
        total: Total matching rows across all pages.
        page: 1-based page number.
        page_size: Requested page size.
    """

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pages(self) -> int:
        """Total number of pages, at least 1 even when there are no results."""
        return max(1, math.ceil(self.total / self.page_size))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_next(self) -> bool:
        """Whether a further page exists."""
        return self.page < self.pages

    @classmethod
    def build(cls, items: list[T], *, total: int, page: int, page_size: int) -> Page[T]:
        """Construct a page from a result slice.

        Args:
            items: Rows for this page.
            total: Total matching rows.
            page: 1-based page number.
            page_size: Requested page size.

        Returns:
            The populated envelope.
        """
        return cls(items=items, total=total, page=page, page_size=page_size)


# ===========================================================================
# Scraped item schemas — the scraper/pipeline contract
# ===========================================================================


class _ScrapedModel(BaseModel):
    """Base for items produced by scrapers.

    ``extra="forbid"`` is intentional. A scraper that starts emitting a field the
    pipeline does not know about is a bug worth surfacing immediately, not
    something to drop silently.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScrapedUniversity(_ScrapedModel):
    """An institution as produced by a scraper, before normalisation."""

    external_id: str | None = None
    name: str = Field(min_length=1, max_length=300)
    name_local: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    website_url: str | None = Field(default=None, max_length=500)
    type: UniversityType | None = None
    accreditation_body: str | None = Field(default=None, max_length=200)
    founding_year: int | None = Field(default=None, ge=800, le=2200)
    student_count: int | None = Field(default=None, ge=0)
    international_student_count: int | None = Field(default=None, ge=0)


class ScrapedCriterion(_ScrapedModel):
    """An admission requirement as produced by a scraper."""

    criterion_type: CriterionType
    description: str | None = None
    minimum_score: str | None = Field(default=None, max_length=50)
    maximum_score: str | None = Field(default=None, max_length=50)
    weight: Decimal | None = Field(default=None, ge=0, le=1)
    is_mandatory: bool = True


class ScrapedStatistic(_ScrapedModel):
    """Admission outcomes as produced by a scraper.

    The bounds here mirror the database check constraints, so an implausible
    record is rejected at the scraper boundary with a readable Pydantic error
    rather than at ``INSERT`` time with an integrity violation.
    """

    academic_year: AcademicYear
    applicants_count: int | None = Field(default=None, ge=0)
    admitted_count: int | None = Field(default=None, ge=0)
    waitlist_count: int | None = Field(default=None, ge=0)
    rejected_count: int | None = Field(default=None, ge=0)
    min_score_admitted: Decimal | None = None
    max_score_admitted: Decimal | None = None
    avg_score_admitted: Decimal | None = None
    acceptance_rate: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator("acceptance_rate")
    @classmethod
    def _quantize_rate(cls, value: Decimal | None) -> Decimal | None:
        """Round the rate to the four decimals the column stores.

        Args:
            value: The computed acceptance rate.

        Returns:
            The value rounded to 4 decimal places, or ``None``.
        """
        return None if value is None else value.quantize(Decimal("0.0001"))


class ScrapedProgram(_ScrapedModel):
    """A programme as produced by a scraper.

    This is the unit the pipeline consumes. It carries its institution inline
    because most platforms publish one flat record per programme, and splitting
    them is the normaliser's job, not the scraper's.
    """

    #: The source platform's own identifier for this programme, when it publishes
    #: one. Programme names are *not* unique within an institution, so this is
    #: what deduplication keys on; see :attr:`source_key`.
    external_id: str | None = Field(default=None, max_length=64)

    name: str = Field(min_length=1, max_length=500)
    name_local: str = Field(min_length=1, max_length=500)
    degree_level: DegreeLevel = DegreeLevel.OTHER
    field_of_study: str | None = Field(default=None, max_length=200)
    field_code: str | None = Field(default=None, max_length=50)
    language_of_instruction: list[LanguageCode] = Field(default_factory=list)
    duration_years: Decimal | None = Field(default=None, gt=0, le=15)
    duration_semesters: int | None = Field(default=None, gt=0)
    tuition_fee: Decimal | None = Field(default=None, ge=0)
    currency: CurrencyCode | None = None
    application_fee: Decimal | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, ge=0)
    application_deadline: date | None = None
    application_open_date: date | None = None
    academic_year: AcademicYear
    is_international_program: bool = False
    requires_visa: bool = False

    source_platform: str = Field(min_length=1, max_length=50)
    source_url: str | None = Field(default=None, max_length=1000)

    university: ScrapedUniversity
    criteria: list[ScrapedCriterion] = Field(default_factory=list)
    statistics: list[ScrapedStatistic] = Field(default_factory=list)

    #: The source record exactly as retrieved, stored for replay and drift
    #: diagnosis. Not validated: it is evidence, not data.
    raw_data: dict | None = None

    @property
    def source_key(self) -> str:
        """The value deduplication and upserts key on.

        Returns:
            :attr:`external_id` when the platform published one, otherwise the
            local name.
        """
        return self.external_id or self.name_local

    @field_validator("currency", mode="before")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        """Upper-case the currency code before the ISO 4217 pattern is applied.

        Args:
            value: The raw currency code.

        Returns:
            The upper-cased code, or ``None``.
        """
        return value.upper() if isinstance(value, str) and value else None

    @model_validator(mode="after")
    def _check_cross_field_rules(self) -> ScrapedProgram:
        """Apply the rules the database also enforces, but earlier.

        Mirrors ``ck_programs_fee_requires_currency`` and
        ``ck_programs_application_window_ordered`` so a bad record is rejected at
        the scraper boundary with a readable message instead of surfacing as an
        integrity violation at insert time.

        Returns:
            The validated instance.

        Raises:
            ValueError: If a fee carries no currency, or the application window
                is inverted.
        """
        if self.tuition_fee is not None and self.currency is None:
            raise ValueError("currency is required when tuition_fee is set")
        if self.application_fee is not None and self.currency is None:
            raise ValueError("currency is required when application_fee is set")
        if (
            self.application_open_date is not None
            and self.application_deadline is not None
            and self.application_open_date > self.application_deadline
        ):
            raise ValueError("application_open_date cannot be after application_deadline")
        return self
