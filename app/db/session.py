"""Async SQLAlchemy engine + session factory.

The app talks to Postgres through a single async engine. Sessions are handed to
the request via a FastAPI dependency (see app.core.deps.get_db). No sync DB
calls ever run in the request path (Coding conventions §6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_settings = get_settings()

# asyncpg prepares and CACHES a plan per SQL string, and a plan is bound to the
# tables the search_path resolved to when it was made. Avora moves a pooled
# connection between workspace schemas, so a connection that served one
# workspace carries plans pointing at ITS tables into the next and Postgres
# rejects them (InvalidCachedStatementError).
#
# Turning the cache off is what makes the same query text mean different tables
# on the same connection. The cost is re-planning each statement, which on this
# size of workload is nothing next to a dashboard that errors the moment
# somebody switches workspace.
#
# No test could have caught this: SQLite has neither schemas nor a plan cache.
#
# Guarded by driver: these are asyncpg's own connect arguments and any other
# driver rejects them outright, so a local SQLite URL would fail to connect
# rather than simply not needing the setting.
_URL = str(_settings.database_url)
_NO_STATEMENT_CACHE = (
    {"statement_cache_size": 0, "prepared_statement_cache_size": 0} if "asyncpg" in _URL else {}
)

engine: AsyncEngine = create_async_engine(
    _URL,
    connect_args=_NO_STATEMENT_CACHE,
    echo=_settings.db_echo,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    # pre_ping costs one extra round-trip per checkout — worth it against a DB
    # that drops idle connections (Neon), but switch it off (DB_POOL_PRE_PING=false)
    # once the compute is kept warm. pool_recycle proactively retires old conns.
    pool_pre_ping=_settings.db_pool_pre_ping,
    pool_recycle=_settings.db_pool_recycle_seconds,
    future=True,
)

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session, committing on success and rolling back on error."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
