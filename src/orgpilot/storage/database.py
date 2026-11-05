"""Database engine management and async session provider."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from orgpilot.storage.models import Base


class Database:
    """Async database engine manager supporting PostgreSQL and SQLite."""

    def __init__(self, db_url: str = "sqlite+aiosqlite:///:memory:") -> None:
        self.db_url = db_url
        # :memory: sessions share one physical SQLite connection.
        self._memory_lock = asyncio.Lock() if ":memory:" in db_url else None
        self.engine: AsyncEngine = create_async_engine(
            self.db_url,
            echo=False,
            future=True,
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def init_db(self) -> None:
        """Initializes database schema tables."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        """Closes the underlying database connection pool."""
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Provides an async transactional database session context."""
        if self._memory_lock is not None:
            async with self._memory_lock, self._session() as session:
                yield session
        else:
            async with self._session() as session:
                yield session

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


@asynccontextmanager
async def coowned_session(
    db: Database, session: AsyncSession | None
) -> AsyncIterator[AsyncSession]:
    """Yields the caller's session, or opens (and commits) a fresh one.

    Store methods take an optional ``session`` so a caller can make several
    stores write atomically in one transaction (ADR-0015 C1); when none is
    given, the store owns a short self-committing transaction instead.
    """
    if session is not None:
        yield session
    else:
        async with db.session() as owned:
            yield owned
