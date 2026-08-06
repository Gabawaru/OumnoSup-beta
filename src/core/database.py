"""Asynchronous database access.

Exposes the declarative :class:`Base` every ORM model inherits from, a lazily
created asyncio engine, and the two ways the codebase acquires a session:

* :func:`get_session` — a FastAPI dependency, one session per request.
* :func:`session_scope` — a transactional context manager for scrapers, the
  pipeline and scheduled jobs.

The engine is created on first use rather than at import time, so importing a
model never opens a connection (which matters for Alembic, for unit tests and
for ``--help`` style CLI entry points).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Final

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import Settings, get_settings

#: Deterministic names for every index and constraint.
#:
#: Without this, PostgreSQL invents names and Alembic's ``--autogenerate`` cannot
#: reliably match an existing constraint to the model that produced it, so
#: migrations start dropping and recreating constraints at random. Setting it up
#: front is far cheaper than retrofitting it once migrations exist.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model.

    Carries the project-wide :data:`NAMING_CONVENTION`, so all models registered
    against it produce stable constraint names.

    Note:
        The ``ck`` convention interpolates ``%(constraint_name)s``. Every
        ``CheckConstraint`` in the project must therefore be given an explicit
        ``name=`` or SQLAlchemy raises at table-definition time.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    """Build the keyword arguments for :func:`create_async_engine`.

    Args:
        settings: Validated application settings.

    Returns:
        Engine options derived from configuration.
    """
    return {
        "echo": settings.db_echo,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        # Recycle connections before typical proxy/idle timeouts cut them, and
        # validate a connection before handing it out. Long-running scheduler
        # processes sit idle between runs and would otherwise pick up a dead
        # socket on their next job.
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call.

    Returns:
        The cached :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
    """
    settings = get_settings()
    return create_async_engine(settings.database_url, **_engine_kwargs(settings))


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory.

    ``expire_on_commit`` is disabled: with the asyncio engine, touching an
    attribute of an expired instance after commit triggers a lazy refresh outside
    of an await point and raises ``MissingGreenlet``. Keeping instances loaded
    lets callers return ORM objects from a closed session.

    Returns:
        The cached :class:`~sqlalchemy.ext.asyncio.async_sessionmaker`.
    """
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for the lifetime of one API request.

    Intended for use as a FastAPI dependency::

        @router.get("/countries")
        async def list_countries(session: AsyncSession = Depends(get_session)):
            ...

    The session is rolled back if the handler raises and always closed. It does
    not commit automatically — read endpoints need no transaction, and write
    endpoints commit explicitly so the commit point is visible in the code.

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` bound to the engine.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional session scope for non-request code paths.

    Commits when the block exits normally, rolls back on any exception. Used by
    scrapers, the pipeline and scheduled jobs::

        async with session_scope() as session:
            session.add(run)

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` bound to the engine.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def ping() -> bool:
    """Check that the database is reachable and answering queries.

    Used by the API health endpoint and by container start-up code waiting for
    PostgreSQL to accept connections.

    Returns:
        ``True`` if a trivial statement round-trips, ``False`` on any connection
        or query error.
    """
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


async def dispose_engine() -> None:
    """Close all pooled connections and drop the cached engine.

    Call on application shutdown (FastAPI lifespan, scheduler teardown) and
    between tests that need a fresh engine. Safe to call when no engine was ever
    created.
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
