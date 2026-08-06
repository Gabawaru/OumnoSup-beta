"""Compact payload for the public map view.

Builds a single JSON document from what is already stored, so the map is a
product of the pipeline rather than a hand-assembled artefact:

    python -m src.pipeline.map_export > data/exports/map.json

The structure is columnar — string tables plus integer rows referencing them —
because the map ships to a browser. The obvious shape, one object per programme
with names repeated, is roughly six times larger for the same information, and
that difference is felt on a phone.

Coordinates come out of ``Program.raw_data``. Keeping the source record is what
makes a view like this possible without going back to the platform, which is
exactly the case that column was added for.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.database import dispose_engine, session_scope
from src.core.models import AdmissionStatistic, DegreeLevel, Program
from src.utils.logger import configure_logging, logger

__all__ = ["build_map_payload", "main"]

#: Degree levels, in the order the front end indexes them.
_LEVEL_ORDER: list[DegreeLevel] = [
    DegreeLevel.BACHELOR,
    DegreeLevel.MASTER,
    DegreeLevel.DOCTORATE,
    DegreeLevel.OTHER,
]


class _Table:
    """Interns strings and hands back their index."""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}

    def add(self, value: str | None) -> int:
        """Return the index of a value, adding it on first sight.

        Args:
            value: The string to intern. Blank and ``None`` map to ``-1``.

        Returns:
            The index, or ``-1`` when there is nothing to store.
        """
        text = (value or "").strip()
        if not text:
            return -1
        if text not in self._index:
            self._index[text] = len(self._index)
        return self._index[text]

    def values(self) -> list[str]:
        """Return the interned strings in index order."""
        return list(self._index)

    def __len__(self) -> int:
        """Return how many distinct strings are interned."""
        return len(self._index)


def _coordinates(raw: dict[str, Any] | None) -> tuple[float, float] | None:
    """Pull a latitude/longitude pair out of a stored source record.

    Args:
        raw: The programme's ``raw_data``.

    Returns:
        ``(lat, lon)``, or ``None`` when the record carries no usable point.
    """
    if not raw:
        return None
    point = raw.get("g_olocalisation_des_formations")
    if not isinstance(point, dict):
        return None
    lat, lon = point.get("lat"), point.get("lon")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    return float(lat), float(lon)


async def build_map_payload(platform: str = "parcoursup") -> dict[str, Any]:
    """Assemble the map payload from stored programmes.

    Args:
        platform: Restrict to one source platform.

    Returns:
        The columnar payload the map front end consumes.
    """
    names, unis, cities, regions, fields = _Table(), _Table(), _Table(), _Table(), _Table()
    points: dict[int, list[tuple[float, float]]] = defaultdict(list)
    rows: list[list[int]] = []

    async with session_scope() as session:
        programs = (
            await session.execute(
                select(Program)
                .where(Program.source_platform == platform)
                .options(selectinload(Program.university), selectinload(Program.statistics))
            )
        ).scalars().all()

        for program in programs:
            university = program.university
            city_idx = cities.add(university.city)

            coords = _coordinates(program.raw_data)
            if city_idx >= 0 and coords is not None:
                points[city_idx].append(coords)

            stat: AdmissionStatistic | None = next(
                (s for s in program.statistics if s.academic_year == program.academic_year),
                program.statistics[0] if program.statistics else None,
            )
            applicants = stat.applicants_count if stat else None
            admitted = stat.admitted_count if stat else None
            # Rate is carried as an integer in ten-thousandths: it keeps the
            # payload free of floats without losing the precision the display
            # needs (one decimal place on a percentage).
            rate = round(float(stat.acceptance_rate) * 10_000) if (
                stat and stat.acceptance_rate is not None
            ) else -1

            level = (
                _LEVEL_ORDER.index(program.degree_level)
                if program.degree_level in _LEVEL_ORDER
                else len(_LEVEL_ORDER) - 1
            )

            rows.append(
                [
                    names.add(program.name_local),
                    unis.add(university.name),
                    city_idx,
                    regions.add(university.region),
                    fields.add(program.field_of_study),
                    level,
                    program.capacity if program.capacity is not None else -1,
                    applicants if applicants is not None else -1,
                    admitted if admitted is not None else -1,
                    rate,
                    int(program.external_id)
                    if program.external_id is not None and program.external_id.isdigit()
                    else -1,
                ]
            )

    # A city's point is the median of its programmes', which ignores a single
    # mis-geocoded record instead of dragging the marker across the country.
    latitudes = [0.0] * len(cities)
    longitudes = [0.0] * len(cities)
    for city_idx, pts in points.items():
        latitudes[city_idx] = round(statistics.median(p[0] for p in pts), 4)
        longitudes[city_idx] = round(statistics.median(p[1] for p in pts), 4)

    academic_year = programs[0].academic_year if programs else ""
    logger.info(
        "map payload: {} programmes, {} cities, {} regions",
        len(rows), len(cities), len(regions),
    )
    return {
        "n": names.values(), "u": unis.values(), "c": cities.values(),
        "r": regions.values(), "f": fields.values(),
        "lat": latitudes, "lon": longitudes, "y": academic_year, "d": rows,
    }


async def main() -> None:
    """Write the payload to standard output."""
    configure_logging()
    payload = await build_map_payload()
    json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
