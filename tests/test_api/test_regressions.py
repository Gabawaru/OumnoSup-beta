"""Regressions for defects found after the API was first written.

Each test here corresponds to a bug that shipped and was fixed. They exist so
the same mistake cannot return quietly.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.main import _InProcessRateLimiter, create_app
from src.core.database import get_session
from src.core.models import AdmissionStatistic, Program
from src.pipeline.store import store_programs
from tests.conftest import requires_db

ADMIN_TOKEN = "test-admin-token"


class TestRateLimiterMemory:
    """The table grew without bound: every address ever seen was kept forever.

    A public API meets scanners and one-off clients constantly, so the process
    would eventually run out of memory. No database needed for these.
    """

    def test_table_is_bounded_under_high_cardinality(self) -> None:
        limiter = _InProcessRateLimiter(100)
        for i in range(25_000):
            limiter.allow(f"203.0.113.{i}")
        assert len(limiter._hits) <= _InProcessRateLimiter._MAX_TRACKED + 1

    def test_an_active_client_is_still_limited(self) -> None:
        """Eviction must not hand a busy caller a free pass."""
        limiter = _InProcessRateLimiter(3)
        for _ in range(3):
            assert limiter.allow("client") is True
        assert limiter.allow("client") is False

    def test_a_normal_client_is_unaffected(self) -> None:
        limiter = _InProcessRateLimiter(5)
        assert all(limiter.allow("client") for _ in range(5))
        assert limiter.allow("client") is False


@requires_db
class TestStatisticsJoinDuplication:
    """Filtering on acceptance rate joined admission_statistics, so a programme
    with more than one published year was counted once per year: `total` was
    inflated and the programme repeated across pages. Invisible while each
    programme had a single year, and certain to appear on the second session.
    """

    @pytest_asyncio.fixture
    async def client(self, session, parsed_programs, monkeypatch) -> AsyncClient:
        """An API client over data where one programme has two years of stats.

        Args:
            session: The test session.
            parsed_programs: Parsed sample programmes.
            monkeypatch: Pytest patching helper.

        Returns:
            A client bound to the application.
        """
        monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
        monkeypatch.setenv("API_CACHE_TTL_SECONDS", "0")
        from src.core.config import get_settings

        get_settings.cache_clear()

        await store_programs(session, parsed_programs, country_code="FR")
        first = (await session.execute(Program.__table__.select().limit(1))).first()
        session.add(
            AdmissionStatistic(
                program_id=first.id,
                academic_year="2024-2025",
                applicants_count=900,
                admitted_count=450,
                acceptance_rate=Decimal("0.5000"),
            )
        )
        await session.commit()

        app = create_app()
        app.dependency_overrides[get_session] = lambda: session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            yield api

        get_settings.cache_clear()

    async def test_total_is_not_inflated(self, client, parsed_programs) -> None:
        body = (await client.get("/api/v1/programs?min_acceptance_rate=0.0")).json()
        assert body["total"] == len(parsed_programs)

    async def test_no_programme_appears_twice(self, client) -> None:
        body = (await client.get("/api/v1/programs?min_acceptance_rate=0.0")).json()
        ids = [item["id"] for item in body["items"]]
        assert len(ids) == len(set(ids))

    async def test_the_filter_still_discriminates(self, client, parsed_programs) -> None:
        """Fixing the duplication must not turn the filter into a no-op."""
        body = (await client.get("/api/v1/programs?min_acceptance_rate=0.99")).json()
        assert body["total"] < len(parsed_programs)


@requires_db
class TestConcurrentScrapeTrigger:
    """Two triggers for one platform used to start two runs, which would upsert
    the same rows against each other and double the load on the source.

    The pipeline is stubbed out here. Firing a real scrape from a test would
    download the whole national dataset over the network, and asserting on the
    second response would then race the first task's completion — which is
    exactly how the first version of this test failed intermittently.
    """

    @pytest_asyncio.fixture
    async def client(self, session, monkeypatch) -> AsyncClient:
        """An API client whose scrape trigger does no real work.

        Args:
            session: The test session.
            monkeypatch: Pytest patching helper.

        Returns:
            A client bound to the application.
        """
        monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
        from src.core.config import get_settings

        get_settings.cache_clear()

        from src.pipeline import runner

        async def _never_finishes(*_args, **_kwargs) -> None:
            """Stand in for a run that is still in progress."""
            await asyncio.sleep(30)

        # admin.py imports run_pipeline inside the handler, so it resolves from
        # the module at call time and this patch takes effect.
        monkeypatch.setattr(runner, "run_pipeline", _never_finishes)

        app = create_app()
        app.dependency_overrides[get_session] = lambda: session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            yield api

        from src.api.routes.admin import _IN_FLIGHT, _RUNNING_SCRAPES

        for task in list(_RUNNING_SCRAPES):
            task.cancel()
        _RUNNING_SCRAPES.clear()
        _IN_FLIGHT.clear()
        get_settings.cache_clear()

    async def test_first_trigger_is_accepted(self, client) -> None:
        response = await client.post(
            "/api/v1/admin/scrape/parcoursup", headers={"X-Admin-Token": ADMIN_TOKEN}
        )
        assert response.status_code == 202

    async def test_second_trigger_is_refused_while_one_runs(self, client) -> None:
        headers = {"X-Admin-Token": ADMIN_TOKEN}
        assert (
            await client.post("/api/v1/admin/scrape/parcoursup", headers=headers)
        ).status_code == 202

        second = await client.post("/api/v1/admin/scrape/parcoursup", headers=headers)
        # 409, not 429: this conflicts with work in progress, it is not throttling.
        assert second.status_code == 409
        assert second.json()["error"] == "ScrapeAlreadyRunningError"

    async def test_an_unregistered_platform_is_still_404(self, client) -> None:
        """The in-flight guard must not shadow the unknown-platform check."""
        response = await client.post(
            "/api/v1/admin/scrape/nope", headers={"X-Admin-Token": ADMIN_TOKEN}
        )
        assert response.status_code == 404
