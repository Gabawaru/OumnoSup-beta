"""Alembic environment, wired to the project's async engine and settings.

The database URL comes from :mod:`src.core.config`, so migrations use the same
configuration as the application and no credential is ever written to
``alembic.ini``.

Importing :mod:`src.core.models` is what populates ``Base.metadata``; without it
autogenerate would compare against an empty schema and cheerfully propose
dropping every table.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.config import get_settings
from src.core.database import Base
import src.core.models  # noqa: F401  -- registers every table on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", get_settings().database_url)


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Decide whether an object participates in autogeneration.

    Args:
        obj: The schema object.
        name: Its name.
        type_: Its kind, e.g. ``table`` or ``index``.
        reflected: Whether it came from the database.
        compare_to: The object being compared against, if any.

    Returns:
        ``True`` when the object should be considered.
    """
    # Alembic's own bookkeeping table must never appear in a migration.
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def run_migrations_offline() -> None:
    """Emit migration SQL without connecting to a database."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Run migrations on an established synchronous connection.

    Args:
        connection: A connection produced by the async engine's ``run_sync``.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Connect with the async engine and run the migrations."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
