"""Storage tests against a real PostgreSQL database.

These cover the property the whole refresh cycle depends on: running the same
scrape twice must update rows rather than duplicate or reject them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.core.models import AdmissionStatistic, Country, Program, University
from src.core.schemas import ScrapedProgram
from src.pipeline.store import store_programs
from tests.conftest import requires_db

pytestmark = requires_db


async def _count(session, model) -> int:
    """Return the number of rows of a model.

    Args:
        session: Database session.
        model: The ORM class to count.

    Returns:
        The row count.
    """
    return await session.scalar(select(func.count()).select_from(model))


class TestStore:
    async def test_stores_a_batch(self, session, parsed_programs) -> None:
        result = await store_programs(session, parsed_programs, country_code="FR")
        await session.commit()

        assert result.programs_inserted == len(parsed_programs)
        assert result.programs_updated == 0
        assert await _count(session, Program) == len(parsed_programs)

    async def test_creates_the_country_when_absent(self, session, parsed_programs) -> None:
        """A scraper must run before anyone curates the reference data."""
        await store_programs(session, parsed_programs, country_code="FR")
        await session.commit()

        country = (
            await session.execute(select(Country).where(Country.code_iso2 == "FR"))
        ).scalar_one()
        assert country.name == "France"

    async def test_rerun_updates_and_never_duplicates(self, session, parsed_programs) -> None:
        """The property that makes a scheduled daily refresh safe."""
        await store_programs(session, parsed_programs, country_code="FR")
        await session.commit()
        first = await _count(session, Program)

        second_result = await store_programs(session, parsed_programs, country_code="FR")
        await session.commit()

        assert await _count(session, Program) == first
        assert second_result.programs_inserted == 0
        assert second_result.programs_updated == len(parsed_programs)

    async def test_keeps_programmes_sharing_a_name(self, session, parsed_programs) -> None:
        """Distinct programmes with one display name must all survive.

        Several universities publish multiple PASS programmes under a single
        name, differing only by their minor and each with its own capacity.
        """
        shared = [p for p in parsed_programs if "PASS" in p.name_local]
        if len(shared) < 2:
            pytest.skip("sample carries no name collision")

        await store_programs(session, shared, country_code="FR")
        await session.commit()

        assert await _count(session, Program) == len(shared)
        capacities = (await session.execute(select(Program.capacity))).scalars().all()
        assert sum(c for c in capacities if c) == sum(p.capacity or 0 for p in shared)

    async def test_deduplicates_institutions(self, session, parsed_programs) -> None:
        """One row per institution, however many programmes reference it."""
        await store_programs(session, parsed_programs, country_code="FR")
        await session.commit()

        stored = await _count(session, University)
        distinct = len({p.university.external_id or p.university.name for p in parsed_programs})
        assert stored == distinct

    async def test_stores_statistics_alongside(self, session, parsed_programs) -> None:
        await store_programs(session, parsed_programs, country_code="FR")
        await session.commit()

        expected = sum(len(p.statistics) for p in parsed_programs)
        assert await _count(session, AdmissionStatistic) == expected

    async def test_updated_values_are_written_back(self, session, parsed_programs) -> None:
        await store_programs(session, parsed_programs, country_code="FR")
        await session.commit()

        changed = ScrapedProgram.model_validate(
            {**parsed_programs[0].model_dump(), "capacity": 4242}
        )
        await store_programs(session, [changed], country_code="FR")
        await session.commit()

        stored = (
            await session.execute(
                select(Program.capacity).where(Program.external_id == changed.external_id)
            )
        ).scalar_one()
        assert stored == 4242

    async def test_empty_batch_is_a_no_op(self, session) -> None:
        result = await store_programs(session, [], country_code="FR")
        assert result.programs_inserted == 0
        assert await _count(session, Program) == 0
