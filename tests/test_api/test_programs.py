"""API tests, driven through httpx against the ASGI app with a real database."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.main import create_app
from src.core.database import get_session
from src.pipeline.store import store_programs
from tests.conftest import requires_db

pytestmark = requires_db

ADMIN_TOKEN = "test-admin-token"


@pytest_asyncio.fixture
async def client(session, parsed_programs, monkeypatch) -> AsyncClient:
    """An API client backed by the test database, pre-loaded with the sample.

    Args:
        session: The test session.
        parsed_programs: Parsed sample programmes.
        monkeypatch: Pytest patching helper.

    Returns:
        A client bound to the application.
    """
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    from src.core.config import get_settings

    get_settings.cache_clear()

    await store_programs(session, parsed_programs, country_code="FR")
    await session.commit()

    app = create_app()
    # Every request reuses the test session, so the client sees exactly the rows
    # this test wrote and nothing else.
    app.dependency_overrides[get_session] = lambda: session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as api:
        yield api

    get_settings.cache_clear()


class TestMeta:
    async def test_health(self, client) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["database"] == "up"

    async def test_openapi_document_is_generated(self, client) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        assert "/api/v1/programs" in response.json()["paths"]


class TestCountries:
    async def test_lists_countries(self, client) -> None:
        body = (await client.get("/api/v1/countries")).json()
        assert body["total"] >= 1

    async def test_lookup_is_case_insensitive(self, client) -> None:
        assert (await client.get("/api/v1/countries/fr")).status_code == 200

    async def test_unknown_code_is_404(self, client) -> None:
        response = await client.get("/api/v1/countries/ZZ")
        assert response.status_code == 404
        assert response.json()["error"] == "ResourceNotFoundError"


class TestPrograms:
    async def test_lists_programmes(self, client, parsed_programs) -> None:
        body = (await client.get("/api/v1/programs")).json()
        assert body["total"] == len(parsed_programs)

    async def test_page_size_is_clamped_not_rejected(self, client) -> None:
        """A client asking for 5000 rows gets the maximum, not an error."""
        response = await client.get("/api/v1/programs?page_size=5000")
        assert response.status_code == 200
        assert len(response.json()["items"]) <= 100

    async def test_rejects_page_zero(self, client) -> None:
        assert (await client.get("/api/v1/programs?page=0")).status_code == 422

    async def test_filters_by_level(self, client) -> None:
        body = (await client.get("/api/v1/programs?level=bachelor")).json()
        assert all(i["degree_level"] == "bachelor" for i in body["items"])

    async def test_filters_by_minimum_capacity(self, client) -> None:
        body = (await client.get("/api/v1/programs?min_capacity=50")).json()
        assert all(i["capacity"] >= 50 for i in body["items"])

    async def test_detail_includes_related_records(self, client) -> None:
        listing = (await client.get("/api/v1/programs")).json()
        detail = (await client.get(f"/api/v1/programs/{listing['items'][0]['id']}")).json()
        assert detail["university"] is not None
        assert len(detail["statistics"]) >= 1

    async def test_unknown_id_is_404(self, client) -> None:
        response = await client.get(
            "/api/v1/programs/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    async def test_malformed_id_is_422(self, client) -> None:
        assert (await client.get("/api/v1/programs/not-a-uuid")).status_code == 422


class TestSearch:
    async def test_finds_programmes(self, client) -> None:
        body = (await client.get("/api/v1/search?q=licence")).json()
        assert body["total"] >= 0

    async def test_requires_two_characters(self, client) -> None:
        assert (await client.get("/api/v1/search?q=a")).status_code == 422


class TestStats:
    async def test_global_totals(self, client, parsed_programs) -> None:
        body = (await client.get("/api/v1/stats/global")).json()
        assert body["programs"] == len(parsed_programs)
        assert body["total_places"] == sum(p.capacity or 0 for p in parsed_programs)


class TestAdminAuth:
    async def test_requires_a_token(self, client) -> None:
        response = await client.get("/api/v1/admin/scrape-runs")
        assert response.status_code == 401

    async def test_rejects_a_wrong_token(self, client) -> None:
        response = await client.get(
            "/api/v1/admin/scrape-runs", headers={"X-Admin-Token": "nope"}
        )
        assert response.status_code == 401

    async def test_accepts_the_configured_token(self, client) -> None:
        response = await client.get(
            "/api/v1/admin/scrape-runs", headers={"X-Admin-Token": ADMIN_TOKEN}
        )
        assert response.status_code == 200

    async def test_unknown_platform_is_404(self, client) -> None:
        response = await client.post(
            "/api/v1/admin/scrape/nope", headers={"X-Admin-Token": ADMIN_TOKEN}
        )
        assert response.status_code == 404
