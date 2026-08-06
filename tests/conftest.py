"""Shared test fixtures.

Database-backed tests run against a real PostgreSQL instance, never SQLite. The
schema relies on native enums, ``ARRAY``, ``JSONB`` and ``ON CONFLICT`` — none of
which SQLite has — so a substitute database would pass tests that production
would fail.

Point ``TEST_DATABASE_URL`` at a disposable database; those tests skip when it is
unset, so the offline suite still runs anywhere.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.core.schemas import ScrapedProgram
from src.scrapers.france.parsers.parcoursup_parser import parse_record

FIXTURE = Path(__file__).parent.parent / "data" / "samples" / "parcoursup_sample.json"

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "").replace("/oumnosup", "/oumnosup_test") or "",
)

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL (or DATABASE_URL) to run database-backed tests",
)


@pytest.fixture(scope="session")
def raw_records() -> list[dict]:
    """The committed Parcoursup sample, straight from disk.

    Returns:
        Raw source records.
    """
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def parsed_programs(raw_records: list[dict]) -> list[ScrapedProgram]:
    """The sample parsed into items.

    Args:
        raw_records: Raw source records.

    Returns:
        Parsed programmes.
    """
    return [parse_record(record) for record in raw_records]


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session against a freshly created schema.

    The schema is created and dropped per test, so no test can see another's
    rows or depend on their ordering.

    Yields:
        A database session.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
