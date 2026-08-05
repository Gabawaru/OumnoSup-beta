"""Duplicate detection and merging.

Two kinds of duplicate arrive from a scrape:

*Exact* — the same programme published twice under the natural key
``(institution, local name, academic year)``. The database rejects these outright,
so they must be collapsed before storage or the whole batch fails.

*Near* — the same programme worded slightly differently between runs or between
campuses ("Licence Droit" vs "Licence - Droit"). These are reported rather than
merged automatically: silently collapsing two genuinely distinct programmes would
destroy data, and the two are indistinguishable without a human look.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Final

from src.core.schemas import ScrapedProgram
from src.pipeline.normalizer import match_key
from src.utils.logger import logger

__all__ = ["DedupeReport", "NearDuplicate", "deduplicate", "similarity"]

#: Names at or above this ratio are reported as near-duplicates.
NEAR_DUPLICATE_THRESHOLD: Final[float] = 0.92


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    """Two programmes that look like the same thing under different wording.

    Attributes:
        left: One programme's local name.
        right: The other programme's local name.
        institution: The institution both belong to.
        ratio: Similarity between the two names, from 0 to 1.
    """

    left: str
    right: str
    institution: str
    ratio: float


@dataclass(slots=True)
class DedupeReport:
    """Outcome of deduplicating a batch.

    Attributes:
        unique: One programme per natural key.
        exact_merged: How many exact duplicates were folded away.
        near_duplicates: Suspected duplicates left in place for review.
    """

    unique: list[ScrapedProgram] = field(default_factory=list)
    exact_merged: int = 0
    near_duplicates: list[NearDuplicate] = field(default_factory=list)


def similarity(left: str, right: str) -> float:
    """Return how similar two names are, from 0 to 1.

    Args:
        left: First name.
        right: Second name.

    Returns:
        The similarity ratio of their canonical match keys.
    """
    return SequenceMatcher(None, match_key(left), match_key(right)).ratio()


def _richness(program: ScrapedProgram) -> int:
    """Score how much usable information a record carries.

    Used to pick the survivor when two records share a natural key: the one that
    actually has capacity, statistics and a source link is the better record to
    keep.

    Args:
        program: The programme to score.

    Returns:
        A count of populated fields of interest.
    """
    score = 0
    for value in (
        program.capacity, program.source_url, program.field_of_study,
        program.duration_years, program.tuition_fee,
    ):
        if value is not None:
            score += 1
    score += len(program.criteria) + len(program.statistics)
    for stat in program.statistics:
        score += sum(
            1 for v in (stat.applicants_count, stat.admitted_count, stat.acceptance_rate)
            if v is not None
        )
    if program.university.external_id:
        score += 2
    return score


def _merge(keep: ScrapedProgram, drop: ScrapedProgram) -> ScrapedProgram:
    """Fold any field the surviving record lacks in from the discarded one.

    Args:
        keep: The record being kept.
        drop: The duplicate being discarded.

    Returns:
        The enriched record.
    """
    data = keep.model_dump()
    other = drop.model_dump()
    for key, value in data.items():
        if value in (None, [], "") and other.get(key) not in (None, [], ""):
            data[key] = other[key]
    # The institution is a nested record; fill its gaps too.
    uni = data["university"]
    for key, value in uni.items():
        if value in (None, "") and other["university"].get(key) not in (None, ""):
            uni[key] = other["university"][key]
    if not data["statistics"] and other["statistics"]:
        data["statistics"] = other["statistics"]
    if not data["criteria"] and other["criteria"]:
        data["criteria"] = other["criteria"]
    return ScrapedProgram.model_validate(data)


def deduplicate(
    programs: list[ScrapedProgram], *, detect_near: bool = True
) -> DedupeReport:
    """Collapse exact duplicates and report near-duplicates.

    Args:
        programs: Validated programmes.
        detect_near: Whether to look for near-duplicates. The search is
            quadratic within an institution, so it can be disabled for very large
            batches.

    Returns:
        The deduplication report.
    """
    report = DedupeReport()
    by_key: dict[tuple[str, str, str], ScrapedProgram] = {}

    for program in programs:
        # Key on the platform's own programme code where it exists. Names are not
        # unique within an institution, so folding on the name destroys genuinely
        # distinct programmes — see Program.source_key in src/core/models.py.
        key = (
            match_key(program.university.external_id or program.university.name),
            program.external_id or match_key(program.name_local),
            program.academic_year,
        )
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = program
            continue
        report.exact_merged += 1
        # Keep the richer record and fold the other's extra fields into it.
        if _richness(program) > _richness(existing):
            by_key[key] = _merge(program, existing)
        else:
            by_key[key] = _merge(existing, program)

    report.unique = list(by_key.values())

    if detect_near:
        grouped: dict[str, list[ScrapedProgram]] = defaultdict(list)
        for program in report.unique:
            grouped[
                match_key(program.university.external_id or program.university.name)
            ].append(program)
        for institution, group in grouped.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if a.academic_year != b.academic_year:
                        continue
                    ratio = similarity(a.name_local, b.name_local)
                    if ratio >= NEAR_DUPLICATE_THRESHOLD:
                        report.near_duplicates.append(
                            NearDuplicate(
                                left=a.name_local, right=b.name_local,
                                institution=a.university.name, ratio=round(ratio, 3),
                            )
                        )

    if report.exact_merged:
        logger.info("deduplicator merged {} exact duplicates", report.exact_merged)
    if report.near_duplicates:
        logger.info(
            "deduplicator flagged {} near-duplicates for review (not merged)",
            len(report.near_duplicates),
        )
    return report
