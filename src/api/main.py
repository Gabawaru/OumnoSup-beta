"""The public OumnoSup API.

Served under ``/api/v1``, documented at ``/docs``.

Rate limiting and response caching both degrade rather than fail when Redis is
absent: limiting falls back to per-process counters and caching switches off
after one failed connection attempt. A missing cache must never take the API
down.

Cached responses carry an ``X-Cache`` header of ``HIT`` or ``MISS``, so a client
— or a bug report — can tell where an answer came from.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.cache import get_cache
from src.api.routes import admin, countries, programs, stats, universities
from src.core.config import get_settings
from src.core.database import dispose_engine, ping
from src.core.exceptions import APIError, OumnoSupError
from src.utils.logger import configure_logging, logger

__all__ = ["app", "create_app"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown.

    Args:
        app: The application being started.

    Yields:
        Control while the application serves requests.
    """
    configure_logging()
    settings = get_settings()
    logger.info("OumnoSup API starting in {} mode", settings.environment.value)
    if not await ping():
        # Not fatal: the container may start before PostgreSQL accepts
        # connections, and the health endpoint reports the real state.
        logger.warning("database not reachable at startup; /health will report it")
    yield
    await get_cache().close()
    await dispose_engine()
    logger.info("OumnoSup API stopped")


class _InProcessRateLimiter:
    """Sliding-window request limiter, per client address.

    Used when Redis is unavailable. Its counters are per process, so several
    workers each allow the configured rate; that is a deliberate trade against
    refusing to serve at all.

    Entries are swept once the table grows past :data:`_SWEEP_THRESHOLD`. Without
    that, every address ever seen is remembered forever — a public API meets
    scanners and one-off clients constantly, so the table would grow without
    bound until the process runs out of memory.
    """

    #: Hard ceiling on tracked addresses.
    _MAX_TRACKED: Final[int] = 10_000

    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        # Ordered by least-recently-touched, so eviction is a cheap popitem.
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _evict(self, now: float) -> None:
        """Bound the table, dropping expired entries before live ones.

        Expired windows go first, which is enough in normal traffic. If a burst
        of distinct addresses keeps the table over the ceiling anyway, the
        least-recently-seen live entries are dropped too. That costs accuracy —
        an evicted address gets a fresh allowance — and is the right trade: an
        unbounded table eventually takes the process down, and Redis is the
        answer when exact limiting across a large client population matters.

        Args:
            now: Current monotonic time.
        """
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] > 60]:
            del self._hits[key]
        while len(self._hits) > self._MAX_TRACKED:
            self._hits.popitem(last=False)

    def allow(self, key: str) -> bool:
        """Record a request and report whether it is within the limit.

        Args:
            key: Client identifier, normally the remote address.

        Returns:
            ``True`` when the request may proceed.
        """
        now = time.monotonic()
        if len(self._hits) > self._MAX_TRACKED:
            self._evict(now)

        window = self._hits.get(key)
        if window is None:
            window = self._hits[key] = deque()
        else:
            self._hits.move_to_end(key)

        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True


def create_app() -> FastAPI:
    """Build the FastAPI application.

    Returns:
        The configured application.
    """
    settings = get_settings()

    app = FastAPI(
        title="OumnoSup API",
        version="0.1.0",
        summary="L'admission universitaire, sans frontières.",
        description=(
            "Public access to university admission data — programmes, capacities, "
            "deadlines and admission statistics — collected from official national "
            "platforms and normalised into one schema."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    limiter = _InProcessRateLimiter(settings.api_rate_limit_per_minute)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        """Reject a client that exceeds the configured request rate.

        Args:
            request: The incoming request.
            call_next: The next handler in the chain.

        Returns:
            The downstream response, or a 429.
        """
        # Docs and health are exempt: throttling them makes an API look broken
        # exactly when someone is trying to work out whether it is up.
        if request.url.path in {"/health", "/docs", "/redoc", "/openapi.json"}:
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RateLimitExceeded",
                    "message": (
                        f"more than {settings.api_rate_limit_per_minute} requests per "
                        f"minute; wait a moment and retry"
                    ),
                },
                headers={"Retry-After": "60"},
            )
        return await call_next(request)

    cache = get_cache()

    @app.middleware("http")
    async def cache_responses(request: Request, call_next):
        """Serve repeated GET requests from Redis.

        Only successful GETs under ``/api/v1`` are cached, and never the admin
        routes: those are authenticated and report live run state, so a stale
        answer there would be actively misleading.

        Args:
            request: The incoming request.
            call_next: The next handler in the chain.

        Returns:
            The cached body when one is warm, otherwise the fresh response.
        """
        if (
            not cache.enabled
            or request.method != "GET"
            or not request.url.path.startswith("/api/v1")
            or "/admin" in request.url.path
        ):
            return await call_next(request)

        key = cache.key_for(request.url.path, request.url.query)
        if (hit := await cache.get(key)) is not None:
            return JSONResponse(content=hit, headers={"X-Cache": "HIT"})

        response = await call_next(request)
        if response.status_code == 200:
            # Read the streamed body so it can be stored, then hand back an
            # equivalent response: the original iterator is consumed by now.
            body = b"".join([chunk async for chunk in response.body_iterator])
            await cache.set(key, body)
            # Only claim a miss when a cache actually exists to have missed.
            # Advertising MISS with Redis down would misreport the deployment.
            headers = {"X-Cache": "MISS"} if cache.active else None
            return Response(
                content=body,
                status_code=response.status_code,
                media_type=response.media_type,
                headers=headers,
            )
        return response

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        """Map an API error onto its declared status code.

        Args:
            request: The request that failed.
            exc: The raised error.

        Returns:
            The error rendered as JSON.
        """
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(OumnoSupError)
    async def handle_domain_error(request: Request, exc: OumnoSupError) -> JSONResponse:
        """Report an unexpected domain error without leaking internals.

        Args:
            request: The request that failed.
            exc: The raised error.

        Returns:
            A 500 response carrying the error class and message.
        """
        logger.error("unhandled domain error on {}: {}", request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"error": type(exc).__name__, "message": exc.message},
        )

    @app.get("/health", tags=["meta"], summary="Health check")
    async def health() -> dict[str, Any]:
        """Report whether the service and its database are usable.

        Returns:
            The service status and database reachability.
        """
        database_up = await ping()
        return {
            "status": "ok" if database_up else "degraded",
            "database": "up" if database_up else "down",
            "environment": settings.environment.value,
            "version": app.version,
        }

    @app.get("/", tags=["meta"], summary="Service index")
    async def index() -> dict[str, Any]:
        """Point callers at the documentation and the API root.

        Returns:
            Links into the service.
        """
        return {
            "service": "OumnoSup",
            "tagline": "L'admission universitaire, sans frontières.",
            "docs": "/docs",
            "api": "/api/v1",
        }

    prefix = "/api/v1"
    app.include_router(countries.router, prefix=prefix)
    app.include_router(universities.router, prefix=prefix)
    app.include_router(programs.router, prefix=prefix)
    app.include_router(stats.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)

    return app


app = create_app()
