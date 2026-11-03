"""ProjectKnowledgeService: the /项目 entry facade (ADR-0015 C5).

Wires the read loop with gateway-owned, strictly read-only state: the OrgState
snapshot comes from ``get_or_replay_agent``'s projector only — this service
never calls ``run_agent_turn``, ``commit_extracted_events``, or
``save_agent_state`` (ADR-0015 §6/§7.2). The generator is the scripted policy
by default; the online answerer requires an explicit opt-in plus provider
credentials, and live quality acceptance stays a C6 item.
"""

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.knowledge.answerer import LLMProjectAnswerer, ScriptedAnswerGenerator
from orgpilot.knowledge.models import BudgetLimits
from orgpilot.knowledge.read_loop import ProjectReadOrchestrator
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore
from orgpilot.storage.read_session_store import SqlReadSessionStore


class ProjectKnowledgeService:
    """Facade over binding/material lookup and the bounded read loop."""

    def __init__(
        self,
        gateway_service,
        settings: OrgPilotSettings | None = None,
        clock: Clock | None = None,
        timer=None,
        generator=None,
        limits: BudgetLimits | None = None,
    ) -> None:
        self.settings = settings or OrgPilotSettings()
        self.clock: Clock = clock or SystemClock()
        db = gateway_service.db
        self.gateway_service = gateway_service
        self.sources = SqlProjectSourceStore(db, self.clock)
        self.knowledge = SqlKnowledgeStore(db, self.clock)
        self.read_sessions = SqlReadSessionStore(db, self.clock)
        self.limits = limits or BudgetLimits()
        self.generator = generator or self._default_generator()
        self.orchestrator = ProjectReadOrchestrator(
            sources=self.sources,
            knowledge_store=self.knowledge,
            read_sessions=self.read_sessions,
            clock=self.clock,
            timer=timer,
            generator=self.generator,
            limits=self.limits,
            org_state_provider=self._read_only_org_state,
        )

    def _default_generator(self):
        """Scripted policy unless an online answerer is explicitly opted in."""
        if not self.settings.project_qa_online:
            return ScriptedAnswerGenerator()
        provider = self.settings.llm_provider
        if provider == "aihubmix" and self.settings.aihubmix_api_key:
            from orgpilot.extraction.client import AnthropicCompatibleLLMClient

            client = AnthropicCompatibleLLMClient(
                api_key=self.settings.aihubmix_api_key,
                base_url=self.settings.aihubmix_base_url,
                model=self.settings.aihubmix_model,
                thinking=self.settings.llm_thinking,
            )
            return LLMProjectAnswerer(client, self.settings.aihubmix_model)
        if provider == "deepseek" and self.settings.deepseek_api_key:
            from orgpilot.extraction.client import AnthropicCompatibleLLMClient

            client = AnthropicCompatibleLLMClient(
                api_key=self.settings.deepseek_api_key,
                base_url=self.settings.deepseek_base_url,
                model=self.settings.deepseek_model,
                thinking=self.settings.llm_thinking,
            )
            return LLMProjectAnswerer(client, self.settings.deepseek_model)
        return ScriptedAnswerGenerator()

    async def _read_only_org_state(self, project_id: str):
        """Read-only OrgState snapshot; never advances any coordination turn."""
        provider = getattr(self.gateway_service, "get_or_replay_agent", None)
        if provider is None:
            return None
        agent = await provider(project_id)
        return agent.projector.state

    async def answer(
        self,
        project_id: str,
        requester_id: str,
        message_key: str,
        question: str,
        occurred_at=None,
    ):
        """Runs (or replays) one bounded read session and returns the answer."""
        return await self.orchestrator.run(
            project_id=project_id,
            requester_id=requester_id,
            message_key=message_key,
            question=question,
            occurred_at=occurred_at,
        )
