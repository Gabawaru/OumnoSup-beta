"""SQLAlchemy ORM models.

The schema is organised in three groups:

* **Reference and scraped data** — :class:`Country`, :class:`University`,
  :class:`Program`, :class:`AdmissionCriterion`, :class:`AdmissionStatistic`.
* **Operational data** — :class:`ScrapeRun`, the audit trail of every scraper run.
* **Student-facing data** — :class:`OumnoUser`, :class:`OumnoApplication`
  (tables reserved for the accounts feature; nothing writes to them yet).

Two conventions run through the whole module:

Natural keys are enforced in the database.
    Deduplication rules live as ``UniqueConstraint`` objects, not only in
    :mod:`src.pipeline.deduplicator`. A scraper re-run, a concurrent job or a
    manual import therefore cannot create the duplicates the pipeline is
    supposed to prevent.

Plausibility rules live as ``CheckConstraint`` objects.
    The validator stage rejects implausible records with a useful message; the
    constraints make it impossible for such a row to exist regardless of which
    code path wrote it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ScrapingFrequency(str, PyEnum):
    """How often a country's platform is refreshed."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    SEASONAL = "seasonal"


class UniversityType(str, PyEnum):
    """Ownership model of an institution."""

    PUBLIC = "public"
    PRIVATE = "private"
    SEMI_PUBLIC = "semi_public"


class DegreeLevel(str, PyEnum):
    """Level of the qualification a programme awards."""

    BACHELOR = "bachelor"
    MASTER = "master"
    DOCTORATE = "doctorate"
    OTHER = "other"


class CriterionType(str, PyEnum):
    """Kind of requirement an admission criterion expresses."""

    GRADE = "grade"
    TEST = "test"
    INTERVIEW = "interview"
    PORTFOLIO = "portfolio"
    ESSAY = "essay"
    LETTERS_OF_RECOMMENDATION = "letters_of_recommendation"
    LANGUAGE_TEST = "language_test"
    EXTRACURRICULAR = "extracurricular"
    OTHER = "other"


class ScrapeStatus(str, PyEnum):
    """Outcome of a scraper run."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class EducationLevel(str, PyEnum):
    """Highest education level reached by a student user."""

    HIGH_SCHOOL = "high_school"
    BACHELOR = "bachelor"
    MASTER = "master"


class ApplicationStatus(str, PyEnum):
    """Lifecycle state of a student application."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WAITLISTED = "waitlisted"


def _pg_enum(enum_cls: type[PyEnum], name: str) -> SAEnum:
    """Build a native PostgreSQL enum type from a Python enum.

    ``values_callable`` is what makes the database store the member *values*
    (``bachelor``) rather than the member *names* (``BACHELOR``). Without it,
    rows come back upper-cased and every API filter comparing against the
    lower-case value silently returns nothing.

    Args:
        enum_cls: The Python enum to mirror.
        name: Name of the PostgreSQL type to create.

    Returns:
        A configured :class:`sqlalchemy.Enum` column type.
    """
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda enum: [member.value for member in enum],
        native_enum=True,
    )


# ---------------------------------------------------------------------------
# Shared column helpers
# ---------------------------------------------------------------------------


def _uuid_pk() -> Mapped[uuid.UUID]:
    """Return a UUID primary-key column.

    The default is generated both client-side (``uuid4``) and server-side
    (``gen_random_uuid()``), so rows inserted by raw SQL or by ``COPY`` get an
    identifier too. ``gen_random_uuid()`` is built into PostgreSQL 13+.

    Returns:
        A configured primary-key column.
    """
    return mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` to a model.

    Note:
        ``updated_at`` refreshes through SQLAlchemy's ``onupdate`` hook, which
        fires for ORM and Core updates issued by this application. A statement
        run directly in ``psql`` will not bump it; add a database trigger if that
        ever becomes a requirement.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Reference and scraped data
# ---------------------------------------------------------------------------


class Country(TimestampMixin, Base):
    """A country and the national admission platform OumnoSup scrapes for it."""

    __tablename__ = "countries"
    __table_args__ = (
        CheckConstraint("char_length(code_iso2) = 2", name="code_iso2_length"),
        CheckConstraint("char_length(code_iso3) = 3", name="code_iso3_length"),
        CheckConstraint("char_length(language) = 2", name="language_length"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()

    code_iso2: Mapped[str] = mapped_column(String(2), nullable=False, unique=True)
    code_iso3: Mapped[str] = mapped_column(String(3), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_local: Mapped[str | None] = mapped_column(String(120))

    admission_platform_name: Mapped[str | None] = mapped_column(String(120))
    admission_platform_url: Mapped[str | None] = mapped_column(String(500))

    #: ISO 639-1 code of the platform's primary language.
    language: Mapped[str] = mapped_column(String(2), nullable=False)
    #: How the country writes an academic year, e.g. ``2025-2026``.
    academic_year_format: Mapped[str | None] = mapped_column(String(20))

    #: Scraping is opt-in per country: a country row can exist long before its
    #: scraper does, and a misbehaving platform can be paused without a deploy.
    scraping_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    scraping_frequency: Mapped[ScrapingFrequency] = mapped_column(
        _pg_enum(ScrapingFrequency, "scraping_frequency"),
        nullable=False,
        default=ScrapingFrequency.SEASONAL,
        server_default=ScrapingFrequency.SEASONAL.value,
    )
    last_scrape_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    universities: Mapped[list[University]] = relationship(
        back_populates="country", cascade="all, delete-orphan", passive_deletes=True
    )
    users: Mapped[list[OumnoUser]] = relationship(back_populates="country")

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return f"<Country {self.code_iso2} {self.name!r}>"


class University(TimestampMixin, Base):
    """An institution offering programmes within a country."""

    __tablename__ = "universities"
    __table_args__ = (
        # A country's own identifier for the institution (UAI in France) is the
        # only stable key across scrape runs: names get reworded upstream.
        # Left unnamed so NAMING_CONVENTION supplies the ``uq_`` prefix; an
        # explicit name= would be used verbatim and break the convention.
        UniqueConstraint("country_id", "external_id"),
        Index("ix_universities_country_id_name", "country_id", "name"),
        CheckConstraint(
            "founding_year IS NULL OR founding_year BETWEEN 800 AND 2200",
            name="founding_year_plausible",
        ),
        CheckConstraint(
            "student_count IS NULL OR student_count >= 0", name="student_count_non_negative"
        ),
        CheckConstraint(
            "international_student_count IS NULL OR international_student_count >= 0",
            name="international_student_count_non_negative",
        ),
        CheckConstraint(
            "international_student_count IS NULL OR student_count IS NULL "
            "OR international_student_count <= student_count",
            name="international_students_within_total",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # No index=True: the (country_id, external_id) unique constraint and the
    # (country_id, name) index both lead with this column, so a standalone index
    # would only add write cost.
    country_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("countries.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Source-platform identifier, e.g. the French ``cod_uai``. Nullable because
    #: not every platform publishes one.
    external_id: Mapped[str | None] = mapped_column(String(64))

    #: Normalised English name.
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Name as published in the country's own language.
    name_local: Mapped[str | None] = mapped_column(String(300))

    city: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    website_url: Mapped[str | None] = mapped_column(String(500))

    type: Mapped[UniversityType | None] = mapped_column(
        _pg_enum(UniversityType, "university_type")
    )
    accreditation_body: Mapped[str | None] = mapped_column(String(200))
    founding_year: Mapped[int | None] = mapped_column(Integer)
    student_count: Mapped[int | None] = mapped_column(Integer)
    international_student_count: Mapped[int | None] = mapped_column(Integer)

    oumno_partner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    country: Mapped[Country] = relationship(back_populates="universities")
    programs: Mapped[list[Program]] = relationship(
        back_populates="university", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return f"<University {self.name!r}>"


class Program(TimestampMixin, Base):
    """A course of study offered by a university for one academic year."""

    __tablename__ = "programs"
    __table_args__ = (
        # The natural key is (institution, source_key, year) — NOT the programme
        # name. Names are not unique within an institution: Parcoursup publishes
        # several distinct PASS programmes per university sharing one display
        # name and differing only by their minor, each with its own capacity and
        # applicant counts. Keying on the name silently merged 1404 of 14252
        # records in a real run. See ``source_key`` below.
        #
        # Its backing index also serves lookups by ``university_id`` alone, so
        # that column carries no separate index.
        UniqueConstraint("university_id", "source_key", "academic_year"),
        Index("ix_programs_degree_level_field_of_study", "degree_level", "field_of_study"),
        Index("ix_programs_source_platform_academic_year", "source_platform", "academic_year"),
        CheckConstraint("capacity IS NULL OR capacity >= 0", name="capacity_non_negative"),
        CheckConstraint(
            "duration_years IS NULL OR duration_years > 0", name="duration_years_positive"
        ),
        CheckConstraint(
            "duration_semesters IS NULL OR duration_semesters > 0",
            name="duration_semesters_positive",
        ),
        CheckConstraint("tuition_fee IS NULL OR tuition_fee >= 0", name="tuition_fee_non_negative"),
        CheckConstraint(
            "application_fee IS NULL OR application_fee >= 0",
            name="application_fee_non_negative",
        ),
        CheckConstraint(
            "application_open_date IS NULL OR application_deadline IS NULL "
            "OR application_open_date <= application_deadline",
            name="application_window_ordered",
        ),
        CheckConstraint(
            "tuition_fee IS NULL OR currency IS NOT NULL", name="fee_requires_currency"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # No index=True: the natural-key unique constraint leads with this column.
    university_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("universities.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: The source platform's own identifier for this programme — Parcoursup's
    #: ``cod_aff_form``, for instance. Null when the platform publishes none.
    external_id: Mapped[str | None] = mapped_column(String(64))

    #: What deduplication and upserts key on, never null.
    #:
    #: Holds ``external_id`` when the platform provides one and falls back to the
    #: local name otherwise. The redundancy with ``external_id`` is deliberate:
    #: a nullable column cannot carry a unique constraint usefully in PostgreSQL
    #: (nulls compare distinct, so codeless rows would duplicate freely), and a
    #: functional index over ``coalesce()`` would defeat both ``ON CONFLICT``
    #: inference and reliable Alembic autogeneration.
    source_key: Mapped[str] = mapped_column(String(500), nullable=False)

    #: Normalised English name, produced by the translator stage.
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Name exactly as published by the source platform.
    name_local: Mapped[str] = mapped_column(String(500), nullable=False)

    degree_level: Mapped[DegreeLevel] = mapped_column(
        _pg_enum(DegreeLevel, "degree_level"),
        nullable=False,
        default=DegreeLevel.OTHER,
        server_default=DegreeLevel.OTHER.value,
    )
    field_of_study: Mapped[str | None] = mapped_column(String(200))
    #: The source platform's own subject classification code.
    field_code: Mapped[str | None] = mapped_column(String(50))

    #: ISO 639-1 codes, e.g. ``["fr", "en"]``.
    language_of_instruction: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)), nullable=False, default=list, server_default=text("'{}'")
    )

    duration_years: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    duration_semesters: Mapped[int | None] = mapped_column(Integer)

    # Money is Numeric, never Float: binary floats cannot represent decimal
    # currency amounts exactly and the rounding error compounds on conversion.
    tuition_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    #: ISO 4217 code the fees are expressed in.
    currency: Mapped[str | None] = mapped_column(String(3))
    application_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    #: Number of seats. Nullable: many platforms publish programmes without one.
    capacity: Mapped[int | None] = mapped_column(Integer)
    application_deadline: Mapped[date | None] = mapped_column(Date)
    application_open_date: Mapped[date | None] = mapped_column(Date)
    academic_year: Mapped[str] = mapped_column(String(9), nullable=False)

    is_international_program: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    requires_visa: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    #: Slug of the scraper that produced the row, e.g. ``parcoursup``.
    #: No index=True: the (source_platform, academic_year) index leads with it.
    source_platform: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))

    #: The source record as retrieved, before normalisation. Kept so the pipeline
    #: can be re-run over stored payloads after a mapping fix without hitting the
    #: platform again, and so structural drift can be diagnosed after the fact.
    raw_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    university: Mapped[University] = relationship(back_populates="programs")
    criteria: Mapped[list[AdmissionCriterion]] = relationship(
        back_populates="program", cascade="all, delete-orphan", passive_deletes=True
    )
    statistics: Mapped[list[AdmissionStatistic]] = relationship(
        back_populates="program", cascade="all, delete-orphan", passive_deletes=True
    )
    applications: Mapped[list[OumnoApplication]] = relationship(
        back_populates="program", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return f"<Program {self.name_local!r} {self.academic_year}>"


class AdmissionCriterion(TimestampMixin, Base):
    """One requirement a candidate must meet to enter a programme."""

    __tablename__ = "admission_criteria"
    __table_args__ = (
        CheckConstraint(
            "weight IS NULL OR (weight >= 0 AND weight <= 1)", name="weight_is_a_ratio"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    program_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    criterion_type: Mapped[CriterionType] = mapped_column(
        _pg_enum(CriterionType, "criterion_type"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)

    # Scores stay textual: grading scales are not comparable across countries
    # ("14/20", "100/120", "A*"). The normalizer derives comparable values
    # separately rather than forcing everything into one numeric scale here.
    minimum_score: Mapped[str | None] = mapped_column(String(50))
    maximum_score: Mapped[str | None] = mapped_column(String(50))

    #: Share of the overall assessment, as a ratio: ``0.4`` means 40%.
    weight: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    program: Mapped[Program] = relationship(back_populates="criteria")

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return f"<AdmissionCriterion {self.criterion_type.value}>"


class AdmissionStatistic(TimestampMixin, Base):
    """Published admission outcomes for a programme in a given year."""

    __tablename__ = "admission_statistics"
    __table_args__ = (
        UniqueConstraint("program_id", "academic_year"),
        CheckConstraint(
            "applicants_count IS NULL OR applicants_count >= 0",
            name="applicants_count_non_negative",
        ),
        CheckConstraint(
            "admitted_count IS NULL OR admitted_count >= 0",
            name="admitted_count_non_negative",
        ),
        CheckConstraint(
            "waitlist_count IS NULL OR waitlist_count >= 0",
            name="waitlist_count_non_negative",
        ),
        CheckConstraint(
            "rejected_count IS NULL OR rejected_count >= 0",
            name="rejected_count_non_negative",
        ),
        CheckConstraint(
            "admitted_count IS NULL OR applicants_count IS NULL "
            "OR admitted_count <= applicants_count",
            name="admitted_within_applicants",
        ),
        CheckConstraint(
            "acceptance_rate IS NULL OR (acceptance_rate >= 0 AND acceptance_rate <= 1)",
            name="acceptance_rate_is_a_ratio",
        ),
        CheckConstraint(
            "min_score_admitted IS NULL OR max_score_admitted IS NULL "
            "OR min_score_admitted <= max_score_admitted",
            name="score_range_ordered",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # No index=True: the (program_id, academic_year) unique constraint leads
    # with this column.
    program_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("programs.id", ondelete="CASCADE"),
        nullable=False,
    )

    academic_year: Mapped[str] = mapped_column(String(9), nullable=False)

    applicants_count: Mapped[int | None] = mapped_column(Integer)
    admitted_count: Mapped[int | None] = mapped_column(Integer)
    waitlist_count: Mapped[int | None] = mapped_column(Integer)
    rejected_count: Mapped[int | None] = mapped_column(Integer)

    min_score_admitted: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    max_score_admitted: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    avg_score_admitted: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))

    #: ``admitted_count / applicants_count``, computed by the pipeline and stored
    #: rather than generated: a generated column would divide by zero for a
    #: programme that received no applications.
    acceptance_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))

    program: Mapped[Program] = relationship(back_populates="statistics")

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return f"<AdmissionStatistic {self.academic_year} rate={self.acceptance_rate}>"


# ---------------------------------------------------------------------------
# Operational data
# ---------------------------------------------------------------------------


class ScrapeRun(Base):
    """Audit record for a single execution of one platform's scraper.

    Written by :class:`src.scrapers.base.BaseScraper` for every run, successful
    or not, so that a country failing never goes unnoticed and so that the admin
    dashboard can show per-platform freshness.

    Note:
        Deliberately has ``created_at`` only. A run record describes an event
        that already happened; it is appended, never edited.
    """

    __tablename__ = "scrape_runs"
    __table_args__ = (
        Index("ix_scrape_runs_platform_started_at", "platform", "started_at"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finished_after_started"
        ),
        CheckConstraint("records_scraped >= 0", name="records_scraped_non_negative"),
        CheckConstraint("records_inserted >= 0", name="records_inserted_non_negative"),
        CheckConstraint("records_updated >= 0", name="records_updated_non_negative"),
        CheckConstraint("records_failed >= 0", name="records_failed_non_negative"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()

    # platform carries no standalone index: the (platform, started_at) index
    # below leads with it and also serves the dashboard's "latest run per
    # platform" query.
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[ScrapeStatus] = mapped_column(
        _pg_enum(ScrapeStatus, "scrape_status"),
        nullable=False,
        default=ScrapeStatus.RUNNING,
        server_default=ScrapeStatus.RUNNING.value,
    )

    records_scraped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    records_inserted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    records_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    records_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    error_log: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return f"<ScrapeRun {self.platform} {self.status.value}>"


# ---------------------------------------------------------------------------
# Student-facing data (reserved for the accounts feature)
# ---------------------------------------------------------------------------


class OumnoUser(TimestampMixin, Base):
    """A student account.

    Note:
        Reserved for a later phase. ``password_hash`` stores a hash produced by a
        password-hashing function; no plaintext credential is ever persisted.
    """

    __tablename__ = "oumno_users"
    __table_args__ = (
        CheckConstraint(
            "char_length(preferred_language) = 2", name="preferred_language_length"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))

    country_code: Mapped[str | None] = mapped_column(
        String(2), ForeignKey("countries.code_iso2", ondelete="SET NULL"), index=True
    )
    preferred_language: Mapped[str] = mapped_column(
        String(2), nullable=False, default="fr", server_default="fr"
    )
    education_level: Mapped[EducationLevel | None] = mapped_column(
        _pg_enum(EducationLevel, "education_level")
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    country: Mapped[Country | None] = relationship(back_populates="users")
    applications: Mapped[list[OumnoApplication]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        """Return a concise debugging representation, without personal data."""
        return f"<OumnoUser {self.id}>"


class OumnoApplication(TimestampMixin, Base):
    """A student's application to a programme.

    Note:
        Reserved for a later phase.
    """

    __tablename__ = "oumno_applications"
    __table_args__ = (
        UniqueConstraint("user_id", "program_id"),
        CheckConstraint(
            "status = 'draft' OR submitted_at IS NOT NULL",
            name="submitted_at_set_once_left_draft",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()

    # No index=True: the (user_id, program_id) unique constraint leads with it.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("oumno_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[ApplicationStatus] = mapped_column(
        _pg_enum(ApplicationStatus, "application_status"),
        nullable=False,
        default=ApplicationStatus.DRAFT,
        server_default=ApplicationStatus.DRAFT.value,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[OumnoUser] = relationship(back_populates="applications")
    program: Mapped[Program] = relationship(back_populates="applications")

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        return f"<OumnoApplication {self.id} {self.status.value}>"


__all__ = [
    "AdmissionCriterion",
    "AdmissionStatistic",
    "ApplicationStatus",
    "Base",
    "Country",
    "CriterionType",
    "DegreeLevel",
    "EducationLevel",
    "OumnoApplication",
    "OumnoUser",
    "Program",
    "ScrapeRun",
    "ScrapeStatus",
    "ScrapingFrequency",
    "TimestampMixin",
    "University",
    "UniversityType",
]
