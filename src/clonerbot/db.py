"""Async database engine and session factory.

Defaults to a local SQLite file so the whole bot runs with zero external infra
for a first paper run; point CLONERBOT_DATABASE_URL at Postgres for production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from clonerbot.config import get_settings
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import Base

log = get_logger("db")

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, future=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


def _scalar_default_sql(col) -> str | None:
    """SQL literal for a column's scalar default, or None if not applicable."""
    d = col.default
    if d is None or not getattr(d, "is_scalar", False):
        return None
    v = d.arg
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, (int, float)):
        return str(v)
    return None


def _ensure_columns(sync_conn) -> None:
    """Add any model columns missing from existing tables (additive migration).

    We only ever add columns, so this poor-man's migration keeps old databases
    working without Alembic: for each existing table, ALTER TABLE ADD COLUMN for
    every mapped column the table doesn't have yet. Scalar defaults are applied
    so existing rows are backfilled. Works on SQLite and Postgres.
    """
    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue  # brand-new table → create_all already made it in full
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(dialect=sync_conn.dialect)
            ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype}'
            default_sql = _scalar_default_sql(col)
            if default_sql is not None:
                ddl += f" DEFAULT {default_sql}"
            sync_conn.exec_driver_sql(ddl)
            log.info("db.add_column", table=table.name, column=col.name)


async def init_db() -> None:
    """Create missing tables and add any missing columns (additive migration)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_columns)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session context manager."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
