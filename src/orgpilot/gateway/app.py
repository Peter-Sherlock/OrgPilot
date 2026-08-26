"""FastAPI application factory and lifecycle management."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orgpilot.gateway.routes import approvals, cases, coordination, events
from orgpilot.storage.database import Database


def create_app(db: Database | None = None) -> FastAPI:
    """Creates and configures the FastAPI event gateway application."""
    database = db or Database()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await database.init_db()
        yield
        await database.close()

    app = FastAPI(
        title="OrgPilot Event Gateway",
        description="Production event gateway and coordination API for OrgPilot agent kernel",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.db = database

    # Include route modules
    app.include_router(events.router)
    app.include_router(cases.router)
    app.include_router(approvals.router)
    app.include_router(coordination.router)

    return app
