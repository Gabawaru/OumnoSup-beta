"""Maps a Parcoursup open data record onto the project's item schema.

The source is one flat record per programme with well over a hundred columns,
documented at
https://data.education.gouv.fr/explore/dataset/fr-esr-parcoursup/information/

Only the columns that map onto the OumnoSup schema are read; the rest are kept
verbatim in ``raw_data`` so nothing is lost and the mapping can be revised
without refetching.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Final

from src.core.exceptions import ParserError
from src.core.models import DegreeLevel, UniversityType
from src.core.schemas import ScrapedProgram, ScrapedStatistic, ScrapedUniversity

__all__ = [
    "PARCOURSUP_REQUIRED_FIELDS",
    "coerce_decimal",
    "coerce_int",
    "degree_level_for",
    "parse_record",
]

PLATFORM: Final[str] = "parcoursup"

#: Columns the parser cannot work without. Absence signals the dataset changed
#: shape, which :meth:`BaseScraper.check_structure` turns into a hard failure.
PARCOURSUP_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {"session", "cod_uai", "g_ea_lib_vx", "fili", "lib_for_voe_ins", "capa_fin", "voe_tot",
     "acc_tot"}
)

#: ``fili`` values seen in the dataset, mapped to the project's degree levels and
#: the nominal length of the qualification in years.
#:
#: The durations are a property of the French diploma itself, not of the
#: individual programme — the dataset does not publish a duration column. They
#: are recorded here rather than inferred ad hoc so the assumption is reviewable.
_FILIERE_MAP: Final[dict[str, tuple[DegreeLevel, Decimal | None]]] = {
    # Three-year first cycle awards.
    "Licence": (DegreeLevel.BACHELOR, Decimal("3")),
    "Licence_Las": (DegreeLevel.BACHELOR, Decimal("3")),
    "BUT": (DegreeLevel.BACHELOR, Decimal("3")),
    "IFSI": (DegreeLevel.BACHELOR, Decimal("3")),
    "EFTS": (DegreeLevel.BACHELOR, Decimal("3")),
    # PASS is the single health-sciences entry year, not a full award.
    "PASS": (DegreeLevel.BACHELOR, Decimal("1")),
    # Five-year integrated engineering programmes carry the master's grade.
    "Ecole d'Ingénieur": (DegreeLevel.MASTER, Decimal("5")),
    # Two-year awards below bachelor level, and preparatory classes which confer
    # no degree of their own — both map to `other` rather than being flattened
    # into `bachelor`.
    "BTS": (DegreeLevel.OTHER, Decimal("2")),
    "CPGE": (DegreeLevel.OTHER, Decimal("2")),
    # Post-secondary business schools on Parcoursup range from three-year
    # bachelors to five-year integrated programmes. The dataset does not say
    # which, so the level is left unclaimed rather than guessed.
    "Ecole de Commerce": (DegreeLevel.OTHER, None),
    "Autre formation": (DegreeLevel.OTHER, None),
}

#: ``contrat_etab`` values mapped to institution ownership.
_INSTITUTION_TYPE_MAP: Final[dict[str, UniversityType]] = {
    "Public": UniversityType.PUBLIC,
    # State-contracted private institutions are publicly funded and regulated,
    # which is what `semi_public` denotes.
    "Privé sous contrat d'association": UniversityType.SEMI_PUBLIC,
    "Privé enseignement supérieur": UniversityType.PRIVATE,
    "Privé hors contrat": UniversityType.PRIVATE,
}


def coerce_int(value: Any) -> int | None:
    """Convert a source value to an integer.

    The two ODS endpoints disagree on typing: ``/exports/json`` returns numbers
    while ``/records`` returns the same columns as strings. Both are handled, as
    are empty strings and thousands separators.

    Args:
        value: The raw value.

    Returns:
        The integer, or ``None`` when the value is absent or blank.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(" ", "").replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def coerce_decimal(value: Any) -> Decimal | None:
    """Convert a source value to a :class:`~decimal.Decimal`.

    Args:
        value: The raw value.

    Returns:
        The decimal, or ``None`` when the value is absent or unparsable.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def degree_level_for(filiere: str | None) -> tuple[DegreeLevel, Decimal | None]:
    """Map a ``fili`` value to a degree level and nominal duration.

    Args:
        filiere: The raw ``fili`` value.

    Returns:
        The degree level and duration in years, the latter ``None`` when the
        qualification's length is not fixed.
    """
    if not filiere:
        return DegreeLevel.OTHER, None
    return _FILIERE_MAP.get(filiere.strip(), (DegreeLevel.OTHER, None))


def academic_year_from_session(session: Any) -> str:
    """Convert a Parcoursup session year to the project's academic-year format.

    The dataset publishes ``session`` as a single admission year (``2025``),
    which corresponds to entry for the ``2025-2026`` academic year.

    Args:
        session: The raw ``session`` value.

    Returns:
        The academic year as ``YYYY-YYYY``.

    Raises:
        ParserError: If the session year cannot be read.
    """
    year = coerce_int(session)
    if year is None or not (1990 <= year <= 2100):
        raise ParserError(
            "unusable Parcoursup session year", platform=PLATFORM, field="session", value=session
        )
    return f"{year}-{year + 1}"


def _acceptance_rate(applicants: int | None, admitted: int | None) -> Decimal | None:
    """Compute the acceptance rate from application and admission counts.

    Uses ``admitted / applicants`` to match the schema's definition. The
    dataset's own ``taux_acces_ens`` is a different measure — the share of
    candidates who received *an offer*, expressed as a whole-number percentage —
    and is preserved in ``raw_data`` rather than conflated with this column.

    Args:
        applicants: Total applications received.
        admitted: Total candidates admitted.

    Returns:
        The rate between 0 and 1, or ``None`` when it cannot be computed.
    """
    if applicants is None or admitted is None or applicants <= 0:
        return None
    rate = Decimal(admitted) / Decimal(applicants)
    # The source occasionally reports more admissions than applications for
    # programmes that filled through a secondary channel. Clamp rather than emit
    # a value the database check constraint would reject.
    return min(rate, Decimal(1))


def parse_record(record: dict[str, Any]) -> ScrapedProgram:
    """Turn one Parcoursup open data record into a validated item.

    Args:
        record: A single record from the dataset.

    Returns:
        The parsed programme, with its institution and statistics attached.

    Raises:
        ParserError: If a field the schema requires is missing or unusable.
    """
    academic_year = academic_year_from_session(record.get("session"))

    institution_name = (record.get("g_ea_lib_vx") or "").strip()
    if not institution_name:
        raise ParserError(
            "record has no institution name", platform=PLATFORM, field="g_ea_lib_vx"
        )

    programme_name = (
        record.get("lib_for_voe_ins") or record.get("form_lib_voe_acc") or ""
    ).strip()
    if not programme_name:
        raise ParserError(
            "record has no programme name", platform=PLATFORM, field="lib_for_voe_ins"
        )

    degree_level, duration_years = degree_level_for(record.get("fili"))

    university = ScrapedUniversity(
        external_id=(record.get("cod_uai") or "").strip() or None,
        # The source publishes only the French name; the translator stage fills
        # the English `name` later, so both start as the local name.
        name=institution_name[:300],
        name_local=institution_name[:300],
        city=(record.get("ville_etab") or "").strip()[:120] or None,
        region=(record.get("region_etab_aff") or "").strip()[:120] or None,
        type=_INSTITUTION_TYPE_MAP.get((record.get("contrat_etab") or "").strip()),
    )

    applicants = coerce_int(record.get("voe_tot"))
    admitted = coerce_int(record.get("acc_tot"))
    statistics = [
        ScrapedStatistic(
            academic_year=academic_year,
            applicants_count=applicants,
            admitted_count=min(admitted, applicants)
            if admitted is not None and applicants is not None
            else admitted,
            acceptance_rate=_acceptance_rate(applicants, admitted),
        )
    ]

    return ScrapedProgram(
        name=programme_name[:500],
        name_local=programme_name[:500],
        degree_level=degree_level,
        field_of_study=(record.get("form_lib_voe_acc") or "").strip()[:200] or None,
        field_code=(record.get("fili") or "").strip()[:50] or None,
        # Parcoursup programmes are taught in French unless a programme says
        # otherwise, and the dataset carries no language column.
        language_of_instruction=["fr"],
        duration_years=duration_years,
        capacity=coerce_int(record.get("capa_fin")),
        academic_year=academic_year,
        # Every Parcoursup programme is in France and open to French secondary
        # leavers; international status is not published per programme.
        is_international_program=False,
        requires_visa=False,
        source_platform=PLATFORM,
        source_url=(record.get("lien_form_psup") or "").strip()[:1000] or None,
        university=university,
        statistics=statistics,
        raw_data=record,
    )
