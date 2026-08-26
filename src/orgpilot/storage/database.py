"""Database engine management and async session provider."""

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
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
