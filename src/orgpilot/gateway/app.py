"""FastAPI application factory and lifecycle management."""

import contextlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.extraction.client import AnthropicCompatibleLLMClient, LLMClient
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import AsyncFeishuClient, FeishuClient
from orgpilot.feishu.ws import FeishuWebSocketListener
from orgpilot.gateway.routes import (
    approvals,
    cases,
    console,
    coordination,
    dag,
    events,
    feishu,
    github,
)
from orgpilot.gateway.service import GatewayService
from orgpilot.github.sync import GitHubSyncService, build_github_sync_service
from orgpilot.intent.router import MessageRouter
from orgpilot.storage.database import Database

logger = logging.getLogger(__name__)


def build_gateway_service(
    settings: OrgPilotSettings,
    database: Database,
    clock: Clock | None = None,
    feishu_client: FeishuClient | None = None,
) -> tuple[GatewayService, LLMClient | None]:
    """Wires the gateway service, adapter, extractor, and intent router from settings.

    Shared by the HTTP app and the standalone ``start-feishu-ws`` listener so both
    run the same adapter, intent client, and business timezone. Returns the LLM
    client too, because the caller owns closing it. ``feishu_client`` replaces the
    real OpenAPI client (tests drive the full wiring against a mock).
    """
    runtime_clock: Clock = clock or SystemClock()
    llm_client: LLMClient | None = None
    if settings.llm_provider == "aihubmix":
        llm_client = AnthropicCompatibleLLMClient(
            api_key=settings.aihubmix_api_key or "",
            base_url=settings.aihubmix_base_url,
            model=settings.aihubmix_model,
            thinking=settings.llm_thinking,
        )
    elif settings.llm_provider == "deepseek":
        llm_client = AnthropicCompatibleLLMClient(
            api_key=settings.deepseek_api_key or "",
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            thinking=settings.llm_thinking,
        )
    extractor = ClaimExtractor(llm_client=llm_client)

    if settings.collaboration_adapter == "feishu":
        client: FeishuClient = feishu_client or AsyncFeishuClient(
            app_id=settings.feishu_app_id or "",
            app_secret=settings.feishu_app_secret or "",
        )

        def adapter_factory(project_id: str) -> CollaborationAdapter:
            return FeishuCollaborationAdapter(
                client=client,
                project_id=project_id,
                clock=runtime_clock,
                task_writeback=settings.feishu_task_writeback,
            )

    else:

        def adapter_factory(project_id: str) -> CollaborationAdapter:
            return MockCollaborationAdapter(project_id=project_id)

    gateway_service = GatewayService(
        database,
        extractor=extractor,
        adapter_factory=adapter_factory,
        business_timezone=settings.business_timezone,
        clock=runtime_clock,
    )

    # Layered intent router (ADR-0008 §3): mock provider keeps the heuristic client.
    intent_router = MessageRouter(service=gateway_service, settings=settings)
    gateway_service.intent_router = intent_router  # type: ignore[attr-defined]

    # ADR-0015 C5: /项目 read-only Q&A, off unless explicitly enabled.
    if settings.project_qa_enabled:
        from orgpilot.knowledge.service import ProjectKnowledgeService

        gateway_service.project_knowledge = ProjectKnowledgeService(  # type: ignore[attr-defined]
            gateway_service, settings, clock=runtime_clock
        )

    # ADR-0015 C3: single-process GitHub sync writer. Both the HTTP app and the
    # standalone Feishu listener build through this function, so they share one
    # sync service instead of starting a second writer.
    sync_service: GitHubSyncService | None = None
    if settings.github_sync_enabled:
        sync_service = build_github_sync_service(settings, database, runtime_clock)
    return gateway_service, llm_client, sync_service


def _github_recheck_task(sync_service: GitHubSyncService, interval_seconds: int):
    async def loop() -> None:
        import asyncio

        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await sync_service.recheck_all_due()
            except Exception:  # noqa: BLE001 - the loop must survive one bad round
                logger.exception("periodic GitHub recheck failed")

    return loop


def create_app(
    db: Database | None = None,
    settings: OrgPilotSettings | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Creates and configures the FastAPI event gateway application."""
    runtime_settings = settings or OrgPilotSettings.from_env()
    runtime_settings.validate()
    database = db or Database(runtime_settings.database_url)
    gateway_service, llm_client, sync_service = build_gateway_service(
        runtime_settings, database, clock
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
                demo_mode=runtime_settings.demo_mode,
            )
            with contextlib.suppress(Exception):
                ws_listener.start()

        # ADR-0015 C3: resume interrupted sync work before accepting traffic,
        # then optionally run the (default-off) periodic material recheck.
        recheck_handle = None
        if sync_service is not None:
            await sync_service.startup_recovery()
            if runtime_settings.github_recheck_enabled:
                import asyncio

                loop = _github_recheck_task(
                    sync_service, runtime_settings.github_recheck_interval_seconds
                )
                recheck_handle = asyncio.create_task(loop())

        try:
            yield
        finally:
            # Shutdown order: stop sync receiving/workers first, then close the
            # database (§5.3). The recheck task cancellation is awaited so a
            # mid-flight write lands before the engine disposes.
            if recheck_handle is not None:
                import asyncio

                recheck_handle.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recheck_handle
            if isinstance(llm_client, AnthropicCompatibleLLMClient):
                llm_client.close()
            knowledge_service = getattr(gateway_service, "project_knowledge", None)
            if knowledge_service is not None:
                await knowledge_service.aclose()
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
    app.state.github_sync = sync_service

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
    app.include_router(console.router)
    app.include_router(feishu.router)
    app.include_router(dag.router)
    if sync_service is not None:
        app.include_router(github.router)

    return app
