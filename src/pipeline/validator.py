"""Plausibility checks applied between parsing and storage.

The ``Scraped*`` models in :mod:`src.core.schemas` already enforce types, bounds
and cross-field rules at the scraper boundary, and the database enforces the same
invariants as constraints. This stage sits between the two and catches what
neither can: values that are individually well-formed and structurally legal but
implausible — a programme with 500,000 places, an acceptance rate of exactly zero
against tens of thousands of applicants, a duration of fourteen years.

Rejecting a record is preferable to storing a wrong one: a missing programme is
visible in the counters, whereas a plausible-looking wrong number silently
misinforms a student choosing where to apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from src.core.schemas import ScrapedProgram
from src.utils.logger import logger

__all__ = ["ValidationReport", "validate_many", "validate_program"]

#: No real programme admits this many students in one intake. A larger figure
#: means the source aggregated several programmes into one row.
MAX_PLAUSIBLE_CAPACITY: Final[int] = 50_000
#: Likewise for applications.
MAX_PLAUSIBLE_APPLICANTS: Final[int] = 500_000
#: Longest credible programme, covering integrated medical degrees.
MAX_PLAUSIBLE_DURATION_YEARS: Final[Decimal] = Decimal("12")
#: Beyond this, a "tuition fee" is almost certainly a total programme cost in
#: minor units (cents) or a mis-parsed identifier.
MAX_PLAUSIBLE_TUITION_EUR: Final[Decimal] = Decimal("200000")


@dataclass(slots=True)
class ValidationReport:
    """Outcome of validating a batch.

    Attributes:
        accepted: Records that passed.
        rejected: ``(record, reason)`` pairs that did not.
        warnings: Non-fatal observations worth surfacing.
    """

    accepted: list[ScrapedProgram] = field(default_factory=list)
    rejected: list[tuple[ScrapedProgram, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rejection_reasons(self) -> list[str]:
        """Reasons for every rejection, in order."""
        return [reason for _, reason in self.rejected]


def validate_program(program: ScrapedProgram) -> str | None:
    """Check one programme for implausible values.

    Args:
        program: The parsed programme.

    Returns:
        ``None`` when the record is acceptable, otherwise the reason to reject it.
    """
    if not program.name_local.strip():
        return "programme has no local name"
    if not program.university.name.strip():
        return "programme has no institution name"

    if program.capacity is not None and program.capacity > MAX_PLAUSIBLE_CAPACITY:
        return f"capacity {program.capacity} exceeds {MAX_PLAUSIBLE_CAPACITY}"

    if (
        program.duration_years is not None
        and program.duration_years > MAX_PLAUSIBLE_DURATION_YEARS
    ):
        return f"duration {program.duration_years} years is implausible"

    if program.tuition_fee is not None and program.tuition_fee > MAX_PLAUSIBLE_TUITION_EUR:
        return f"tuition fee {program.tuition_fee} is implausible"

    for stat in program.statistics:
        if (
            stat.applicants_count is not None
            and stat.applicants_count > MAX_PLAUSIBLE_APPLICANTS
        ):
            return f"applicants {stat.applicants_count} exceeds {MAX_PLAUSIBLE_APPLICANTS}"
        if (
            stat.admitted_count is not None
            and stat.applicants_count is not None
            and stat.admitted_count > stat.applicants_count
        ):
            # The database constraint would reject this too; catching it here
            # gives a readable reason instead of an integrity error mid-batch.
            return (
                f"admitted {stat.admitted_count} exceeds applicants {stat.applicants_count}"
            )
        if stat.academic_year != program.academic_year:
            return (
                f"statistics year {stat.academic_year} does not match programme year "
                f"{program.academic_year}"
            )

    total_weight = sum(
        (c.weight for c in program.criteria if c.weight is not None), Decimal(0)
    )
    if total_weight > Decimal("1.001"):
        return f"admission criteria weights sum to {total_weight}, above 1"

    return None


def validate_many(programs: list[ScrapedProgram]) -> ValidationReport:
    """Validate a batch of programmes.

    Args:
        programs: Parsed programmes.

    Returns:
        A report separating accepted from rejected records.
    """
    report = ValidationReport()
    for program in programs:
        reason = validate_program(program)
        if reason is None:
            report.accepted.append(program)
        else:
            report.rejected.append((program, reason))

    if report.rejected:
        logger.warning(
            "validation rejected {} of {} records", len(report.rejected), len(programs)
        )
        # Surface the shape of the problem, not every instance: a systematic
        # parser fault produces thousands of identical reasons.
        seen: dict[str, int] = {}
        for reason in report.rejection_reasons:
            key = reason.split(" ")[0]
            seen[key] = seen.get(key, 0) + 1
        for key, count in sorted(seen.items(), key=lambda kv: -kv[1])[:5]:
            report.warnings.append(f"{count}x rejection starting with {key!r}")
    return report
