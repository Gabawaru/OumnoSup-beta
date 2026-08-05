"""Export stored data to files under ``data/exports``.

Used for bulk redistribution and for offline inspection of what a run produced.
Exports stream rather than materialising a full list, so a national dataset does
not have to fit in memory twice.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from src.core.schemas import ScrapedProgram
from src.utils.logger import logger

__all__ = ["EXPORT_DIR", "export_csv", "export_json"]

EXPORT_DIR = Path("data/exports")

#: Flat column set for the CSV export. Nested statistics are folded in, since a
#: spreadsheet consumer cannot navigate nesting.
_CSV_COLUMNS: Sequence[str] = (
    "platform", "academic_year", "institution", "institution_external_id", "city",
    "region", "programme", "programme_local", "degree_level", "field_of_study",
    "capacity", "applicants", "admitted", "acceptance_rate", "tuition_fee",
    "currency", "source_url",
)


def _jsonable(value: Any) -> Any:
    """Convert values the JSON encoder cannot handle on its own.

    Args:
        value: Any value from a model dump.

    Returns:
        A JSON-serialisable equivalent.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _flatten(program: ScrapedProgram) -> dict[str, Any]:
    """Flatten a programme into the CSV column set.

    Args:
        program: The programme to flatten.

    Returns:
        One row keyed by :data:`_CSV_COLUMNS`.
    """
    stat = program.statistics[0] if program.statistics else None
    return {
        "platform": program.source_platform,
        "academic_year": program.academic_year,
        "institution": program.university.name,
        "institution_external_id": program.university.external_id or "",
        "city": program.university.city or "",
        "region": program.university.region or "",
        "programme": program.name,
        "programme_local": program.name_local,
        "degree_level": program.degree_level.value,
        "field_of_study": program.field_of_study or "",
        "capacity": program.capacity if program.capacity is not None else "",
        "applicants": stat.applicants_count if stat and stat.applicants_count is not None else "",
        "admitted": stat.admitted_count if stat and stat.admitted_count is not None else "",
        "acceptance_rate": str(stat.acceptance_rate) if stat and stat.acceptance_rate is not None else "",
        "tuition_fee": str(program.tuition_fee) if program.tuition_fee is not None else "",
        "currency": program.currency or "",
        "source_url": program.source_url or "",
    }


def export_json(
    programs: Iterable[ScrapedProgram], *, platform: str, directory: Path | None = None
) -> Path:
    """Write programmes to a JSON file.

    Args:
        programs: The programmes to export.
        platform: Slug used in the filename.
        directory: Destination directory. Defaults to :data:`EXPORT_DIR`.

    Returns:
        The path written.
    """
    target_dir = directory or EXPORT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{platform}_{date.today().isoformat()}.json"

    count = 0
    with path.open("w", encoding="utf-8") as handle:
        handle.write("[\n")
        for program in programs:
            if count:
                handle.write(",\n")
            payload = program.model_dump(mode="json", exclude={"raw_data"})
            handle.write(json.dumps(payload, ensure_ascii=False, default=_jsonable))
            count += 1
        handle.write("\n]\n")

    logger.info("exported {} records to {}", count, path)
    return path


def export_csv(
    programs: Iterable[ScrapedProgram], *, platform: str, directory: Path | None = None
) -> Path:
    """Write programmes to a CSV file.

    Args:
        programs: The programmes to export.
        platform: Slug used in the filename.
        directory: Destination directory. Defaults to :data:`EXPORT_DIR`.

    Returns:
        The path written.
    """
    target_dir = directory or EXPORT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{platform}_{date.today().isoformat()}.csv"

    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for program in programs:
            writer.writerow(_flatten(program))
            count += 1

    logger.info("exported {} records to {}", count, path)
    return path
