"""FastAPI application factory and lifecycle management."""

import contextlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.extraction.client import AnthropicCompatibleLLMClient, LLMClient
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import AsyncFeishuClient
from orgpilot.feishu.ws import FeishuWebSocketListener
from orgpilot.gateway.routes import approvals, cases, coordination, dag, events, feishu
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database


def create_app(
    db: Database | None = None,
    settings: OrgPilotSettings | None = None,
) -> FastAPI:
    """Creates and configures the FastAPI event gateway application."""
    runtime_settings = settings or OrgPilotSettings.from_env()
    runtime_settings.validate()
    database = db or Database(runtime_settings.database_url)

    llm_client: LLMClient | None = None
    if runtime_settings.llm_provider == "aihubmix":
        llm_client = AnthropicCompatibleLLMClient(
            api_key=runtime_settings.aihubmix_api_key or "",
            base_url=runtime_settings.aihubmix_base_url,
            model=runtime_settings.aihubmix_model,
        )
    extractor = ClaimExtractor(llm_client=llm_client)

    if runtime_settings.collaboration_adapter == "feishu":
        feishu_client = AsyncFeishuClient(
            app_id=runtime_settings.feishu_app_id or "",
            app_secret=runtime_settings.feishu_app_secret or "",
        )

        def adapter_factory(project_id: str) -> CollaborationAdapter:
            return FeishuCollaborationAdapter(client=feishu_client, project_id=project_id)

    else:

        def adapter_factory(project_id: str) -> CollaborationAdapter:
            return MockCollaborationAdapter(project_id=project_id)

    gateway_service = GatewayService(
        database,
        extractor=extractor,
        adapter_factory=adapter_factory,
        reference_timezone=runtime_settings.reference_timezone,
        directive_reminder_minutes=runtime_settings.directive_reminder_minutes,
        directive_escalation_minutes=runtime_settings.directive_escalation_minutes,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await database.init_db()
        if (
            runtime_settings.collaboration_adapter == "feishu"
            and runtime_settings.feishu_use_ws
            and runtime_settings.feishu_app_id
            and runtime_settings.feishu_app_secret
        ):
            ws_listener = FeishuWebSocketListener(
                app_id=runtime_settings.feishu_app_id,
                app_secret=runtime_settings.feishu_app_secret,
                gateway_service=gateway_service,
                project_id=runtime_settings.feishu_project_id,
                demo_bootstrap=runtime_settings.demo_bootstrap,
            )
            with contextlib.suppress(Exception):
                ws_listener.start()

        try:
            yield
        finally:
            if isinstance(llm_client, AnthropicCompatibleLLMClient):
                llm_client.close()
            await database.close()

    app = FastAPI(
        title="OrgPilot Event Gateway",
        description="Persistent event gateway and coordination API for the OrgPilot kernel",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.db = database
    app.state.settings = runtime_settings
    app.state.gateway_service = gateway_service

    @app.middleware("http")
    async def require_api_token(request, call_next):
        token = runtime_settings.api_token
        if token and request.url.path.startswith("/api/v1/projects/"):
            authorization = request.headers.get("authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(authorization, expected):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    # Static Single-Page Dashboard routes
    static_html_path = Path(__file__).parent / "static" / "index.html"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def serve_dashboard():
        if static_html_path.exists():
            return HTMLResponse(content=static_html_path.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>OrgPilot Dashboard Not Found</h1>", status_code=404)

    # Include route modules
    app.include_router(events.router)
    app.include_router(cases.router)
    app.include_router(approvals.router)
    app.include_router(coordination.router)
    app.include_router(feishu.router)
    app.include_router(dag.router)

    return app
