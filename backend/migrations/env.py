"""Alembic environment. Runs migrations through the same async driver the app uses
(asyncpg for PostgreSQL, aiosqlite for SQLite)."""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db_schema import metadata

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.getenv("DATABASE_URL")
    if not url:
        from app.config import get_settings
        url = get_settings().database_url
    if not url:
        raise RuntimeError("Set DATABASE_URL, e.g. postgresql+asyncpg://user:pass@host:5432/vbw")
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    return url


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True,
                      render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        if connection.dialect.name == "postgresql":
            # Many app instances may start at once with DB_AUTO_MIGRATE=true: one migrates, the rest
            # wait here and then find nothing to do. Released when the migration transaction ends.
            connection.execute(text("SELECT pg_advisory_xact_lock(7274110)"))
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online())
