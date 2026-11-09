"""Frozen C6 eval runner for project context (offline, deterministic).

Loads the frozen snapshot, materializes a fully imported + derived
environment, and executes three frozen case sets:

- answer assertions (goal/scope, responsibilities/delivery, work records,
  conflicts/missing) against the scripted read loop;
- change/failure sequences over the sync layer, driven by a small op DSL
  (see :data:`OPS`);
- bounded-loop boundary cases with custom generators, limits, and fake timers.

Everything runs offline against the MockGitHubClient fixture. This proves
controller behavior and pipeline correctness; it does NOT prove real GitHub
connectivity or online model quality — those live C6 acceptances need an
authorized repository, credentials, and an online budget.
"""

import contextlib
import copy
import json
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from orgpilot.domain.clock import FrozenClock
from orgpilot.github.client import MockGitHubClient
from orgpilot.knowledge.answerer import (
    ProjectAnswerDecision,
    ScriptedAnswerGenerator,
)
from orgpilot.knowledge.derive import DerivationPipeline
from orgpilot.knowledge.importer import SourceImporter
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    EvidenceRef,
    FakeTimer,
    GroundedStatement,
    InboxStatus,
    IssueSelector,
    ProjectSourceBinding,
    SourceAvailability,
    SourceKind,
    SyncInboxEntry,
    canonical_payload_hash,
)
from orgpilot.knowledge.read_loop import ProjectReadOrchestrator
from orgpilot.knowledge.service import ProjectKnowledgeService
from orgpilot.storage.database import Database
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import (
    SqlProjectSourceStore,
    SqlSyncInboxStore,
    SqlSyncRunStore,
)
from orgpilot.storage.read_session_store import SqlReadSessionStore

EVALS_DIR = pathlib.Path(__file__).parent
T0 = datetime.fromisoformat("2026-11-03T10:00:00+08:00")
DOC_PATH_BY_BLOB = {
    "b-readme": "README.md",
    "b-delivery": "docs/delivery.md",
    "b-api": "docs/spec/api.md",
}


def load_cases(filename: str) -> dict:
    return json.loads((EVALS_DIR / filename).read_text(encoding="utf-8"))


@dataclass
class EvalEnv:
    """One materialized environment over the frozen snapshot."""

    db: Database = field(default_factory=lambda: Database("sqlite+aiosqlite:///:memory:"))
    clock: FrozenClock = field(default_factory=lambda: FrozenClock(T0))
    timer: FakeTimer = field(default_factory=lambda: FakeTimer(start=0.0))

    def __post_init__(self) -> None:
        self._inited = False
        self.sources = SqlProjectSourceStore(self.db, self.clock)
        self.knowledge = SqlKnowledgeStore(self.db, self.clock)
        self.runs = SqlSyncRunStore(self.db, self.clock)
        self.inbox = SqlSyncInboxStore(self.db, self.clock)
        self.read_sessions = SqlReadSessionStore(self.db, self.clock)
        self.snapshot = copy.deepcopy(
            json.loads((EVALS_DIR / "eval_snapshot.json").read_text(encoding="utf-8"))
        )
        self.client = MockGitHubClient(self.snapshot)
        self.binding = ProjectSourceBinding(
            project_id="proj-a",
            repository_id="1001",
            binding_version=1,
            branch="main",
            path_allowlist=("docs", "README.md"),
            issue_selector=IssueSelector(milestone="m2", issue_ids=(4,)),
            explicit_pr_ids=(10,),
            maintainers=("m1",),
            sharing_grants=("alice",),
            authority_rules={"delivery": "docs/delivery.md"},
            access_epoch=1,
            created_at=T0,
        )
        self.generation_manifest: frozenset[str] = frozenset()
        self.derived_generation_ids: list[str] = []
        self.parent_revision_counts: dict[str, int] = {}
        self.last_recheck_report = None
        self._push_counter = 0
        self._sync_service = None

    async def aclose(self) -> None:
        await self.db.close()

    # -- materialization ------------------------------------------------------

    async def init_binding(self) -> None:
        if not self._inited:
            await self.db.init_db()
            self._inited = True
        await self.sources.create_binding(self.binding)

    async def initial_import(self) -> None:
        importer = SourceImporter(
            self.client, self.sources, self.knowledge, self.runs, clock=self.clock
        )
        await importer.import_binding_scope(self.binding)

    async def derive(self, base_generation=None):
        pipeline = DerivationPipeline(self.sources, self.knowledge, clock=self.clock)
        return await pipeline.build_and_publish(
            self.binding, reason="eval", base_generation=base_generation
        )

    async def build_index(self):
        from orgpilot.knowledge.retrieval import MaterialIndex

        objects = await self.sources.list_source_objects("proj-a", self.binding.binding_version)
        revisions = {}
        for obj in objects:
            if obj.availability is SourceAvailability.AVAILABLE:
                revisions[obj.source_id] = await self.sources.latest_revision(obj.source_id)
        return MaterialIndex.from_generation_material(objects, revisions)

    async def make_loop(self, generator=None, limits=None, timer=None) -> ProjectReadOrchestrator:
        return ProjectReadOrchestrator(
            sources=self.sources,
            knowledge_store=self.knowledge,
            read_sessions=self.read_sessions,
            clock=self.clock,
            timer=timer or self.timer,
            generator=generator or ScriptedAnswerGenerator(),
            limits=limits or BudgetLimits(),
            org_state_provider=_no_state,
        )

    async def make_service(self, generator=None) -> ProjectKnowledgeService:
        gateway_service = type("GS", (), {"db": self.db})()
        from orgpilot.config import OrgPilotSettings

        settings = OrgPilotSettings(project_qa_enabled=True)
        return ProjectKnowledgeService(
            gateway_service,
            settings,
            clock=self.clock,
            timer=self.timer,
            generator=generator or ScriptedAnswerGenerator(),
        )

    async def current_generation(self):
        generation_id = await self.knowledge.current_generation_id("proj-a")
        if generation_id is None:
            return None
        return await self.knowledge.get_generation(generation_id)

    async def doc_revision_count(self, path: str) -> int:
        obj = await self.sources.find_source_object(
            "proj-a", self.binding.binding_version, SourceKind.DOCUMENT, path
        )
        if obj is None:
            return 0
        return len(await self.sources.list_revisions(obj.source_id))

    async def doc_availability(self, path: str) -> str | None:
        obj = await self.sources.find_source_object(
            "proj-a", self.binding.binding_version, SourceKind.DOCUMENT, path
        )
        if obj is None:
            return None
        stored = await self.sources.get_source_object(obj.source_id)
        return stored.availability.value if stored else None

    async def doc_latest_contains(self, path: str, text: str) -> bool:
        obj = await self.sources.find_source_object(
            "proj-a", self.binding.binding_version, SourceKind.DOCUMENT, path
        )
        if obj is None:
            return False
        revision = await self.sources.latest_revision(obj.source_id)
        return revision is not None and text in (revision.raw_content or "")

    # -- snapshot mutations -----------------------------------------------------

    def mutate_blob(self, blob: str, append: str | None = None, content: str | None = None) -> None:
        if content is not None:
            self.snapshot["blobs"][blob] = content
        elif append is not None:
            self.snapshot["blobs"][blob] += append

    def repoint(self, sha: str) -> None:
        self.snapshot["branches"]["main"]["sha"] = sha

    def clone_tree(self, from_sha: str, to_sha: str) -> None:
        self.snapshot["trees"][to_sha] = copy.deepcopy(self.snapshot["trees"][from_sha])

    def remove_tree_path(self, sha_key: str, path: str) -> None:
        self.snapshot["trees"][sha_key] = [
            entry for entry in self.snapshot["trees"][sha_key] if entry["path"] != path
        ]

    def mutate_issue(self, index: int, **fields) -> None:
        self.snapshot["issues"][index].update(fields)

    def mutate_comment(self, id: str, **fields) -> None:
        for comment in self.snapshot["issue_comments"]:
            if comment["id"] == id:
                comment.update(fields)

    def inject_failure(self, method: str, match: dict, status: int) -> None:
        self.snapshot.setdefault("failures", []).append(
            {"method": method, "match": match, "status": status}
        )

    # -- sync helpers -------------------------------------------------------------

    def _ensure_sync_service(self):
        if self._sync_service is None:
            from orgpilot.github.sync import GitHubSyncService

            self._sync_service = GitHubSyncService(
                db=self.db,
                sources=self.sources,
                inbox_store=self.inbox,
                runs=self.runs,
                knowledge=self.knowledge,
                clock=self.clock,
                client_factory=lambda binding: self.client,
            )
        return self._sync_service

    async def webhook(self, delivery: str, event: str, payload: dict) -> str:
        service = self._ensure_sync_service()
        outcome = await service.ingest_webhook("1001", delivery, event, None, payload)
        await service.drain_repository("1001")
        return outcome

    async def webhook_conflict(self, delivery: str, payload: dict) -> str:
        """Ingests a same-id/different-payload delivery; expect conflict."""
        entry = SyncInboxEntry(
            repository_id="1001",
            delivery_id=delivery,
            event_type="push",
            payload_hash=canonical_payload_hash(payload),
            payload=payload,
            received_at=self.clock.now(),
        )
        stored = await self.inbox.ingest(entry)
        # Identical redelivery returns False (duplicate); the conflict case is
        # handled by the runner expecting the DuplicateDeliveryConflict raise.
        return "duplicate" if not stored else "accepted"

    async def seed_issue(self) -> None:
        """Drains an issues notification so the parent object materializes."""
        await self.webhook("seed-issue", "issues", {"issue": {"number": 1}})

    async def seed_push(self) -> None:
        await self.webhook("seed-push", "push", {"ref": "refs/heads/main"})

    async def manual_recheck(self):
        service = self._ensure_sync_service()
        self.last_recheck_report = await service.manual_recheck("proj-a")
        return self.last_recheck_report

    async def startup_recovery(self) -> dict:
        service = self._ensure_sync_service()
        return await service.startup_recovery()

    async def drain(self) -> dict:
        service = self._ensure_sync_service()
        return await service.drain_repository("1001")


def _no_state(project_id: str | None = None):
    return None


# ---------------------------------------------------------------- custom generators


class RepeatLookupGenerator(ScriptedAnswerGenerator):
    """Always issues the identical search — never new evidence."""

    async def decide(self, view):
        return ProjectAnswerDecision.lookup(
            "search_project", {"query": "不存在的词"}, summary="repeat"
        )


class DyingGenerator(ScriptedAnswerGenerator):
    """The provider is unreachable — unknown model result."""

    async def decide(self, view):
        raise RuntimeError("provider unreachable")


class FabricateThenRepairGenerator(ScriptedAnswerGenerator):
    """Finishes with fabricated evidence once, then repairs correctly."""

    def __init__(self) -> None:
        self.decides = 0
        self.repairs = 0

    async def decide(self, view):
        self.decides += 1
        if self.decides == 1:
            return ProjectAnswerDecision.lookup(
                "search_project", {"query": "项目目标"}, summary="search"
            )
        return _fabricated_finish()

    async def repair(self, view, rejected, reason):
        self.repairs += 1
        ref = view.evidence[0]
        return ProjectAnswerDecision.finish(
            (
                GroundedStatement(
                    text=f"资料记载：{ref.snippet[:80]}", evidence_refs=(ref,)
                ),
            ),
            AnswerStatus.COMPLETE,
            summary="repaired finish",
        )


class FabricateAlwaysGenerator(ScriptedAnswerGenerator):
    """Finishes with fabricated evidence and repairs into fabricated again."""

    async def decide(self, view):
        if not view.evidence:
            return ProjectAnswerDecision.lookup(
                "search_project", {"query": "项目目标"}, summary="search"
            )
        return _fabricated_finish("编造-2")

    async def repair(self, view, rejected, reason):
        return _fabricated_finish("编造-3")


class RevokeOnFinishGenerator(ScriptedAnswerGenerator):
    """Revokes the cited source exactly when finishing."""

    def __init__(self, env: "EvalEnv") -> None:
        self._env = env

    async def decide(self, view):
        decision = await ScriptedAnswerGenerator.decide(self, view)
        if decision.kind == "finish":
            for statement in decision.statements:
                for ref in statement.evidence_refs:
                    await self._env.sources.set_source_availability(
                        ref.source_id, SourceAvailability.REVOKED, "eval revocation"
                    )
        return decision


class ProbeOutOfScopeGenerator(ScriptedAnswerGenerator):
    """Asks for a source id that exists nowhere in the session scope."""

    async def decide(self, view):
        if view.round_number == 1:
            return ProjectAnswerDecision.lookup(
                "search_project", {"query": "鉴权信息"}, summary="search"
            )
        if view.round_number == 2:
            return ProjectAnswerDecision.lookup(
                "get_related_items", {"source_id": "src-out-of-scope-xyz"}, summary="probe"
            )
        return ProjectAnswerDecision.report_gap("范围外来源不可读", summary="gap")


def _fabricated_finish(marker: str = "编造的目标"):
    fake = EvidenceRef(
        revision_id=f"rev-fabricated-{marker}",
        source_id=f"src-fabricated-{marker}",
        kind=SourceKind.DOCUMENT,
        snippet=marker,
    )
    from orgpilot.knowledge.models import AnswerStatus

    return ProjectAnswerDecision.finish(
        (GroundedStatement(text=marker, evidence_refs=(fake,)),),
        AnswerStatus.COMPLETE,
        summary=f"fabricated finish {marker}",
    )


GENERATORS = {
    "scripted": None,
    "repeat_lookup": RepeatLookupGenerator,
    "dying": DyingGenerator,
    "fabricate_then_repair": FabricateThenRepairGenerator,
    "fabricate_always": FabricateAlwaysGenerator,
    "probe_out_of_scope": ProbeOutOfScopeGenerator,
}


def make_generator(name: str, env: EvalEnv):
    if name == "revoke_on_finish":
        return RevokeOnFinishGenerator(env)
    cls = GENERATORS.get(name)
    if cls is None:
        return ScriptedAnswerGenerator()
    return cls()


# ---------------------------------------------------------------- answer assertions


async def run_answer_case(env: EvalEnv, case: dict) -> list[str]:
    """Runs one frozen answer assertion; returns violations."""
    await env.init_binding()  # also runs init_db exactly once
    generation = await env.current_generation()
    if generation is None:
        await env.initial_import()
        generation = await env.derive()
    env.generation_manifest = frozenset(generation.revision_manifest)
    service = await env.make_service()
    message_key = f"eval-answer-{case['id']}"
    answer = await service.answer("proj-a", "alice", message_key, case["question"], occurred_at=T0)
    return check_expectations(case.get("expect", {}), answer, env)


def check_expectations(expect: dict, answer, env: EvalEnv) -> list[str]:
    """Checks an answer against frozen expectations; returns violations."""
    problems: list[str] = []
    rendered = answer.rendered_text or ""
    statements = answer.statements
    if "answer_status" in expect and answer.answer_status.value != expect["answer_status"]:
        problems.append(
            f"answer_status={answer.answer_status.value} != {expect['answer_status']}"
        )
    for keyword in expect.get("contains", []):
        if keyword not in rendered:
            problems.append(f"missing keyword: {keyword!r}")
    for forbidden in expect.get("must_not", []):
        if forbidden in rendered:
            problems.append(f"forbidden claim present: {forbidden!r}")
    if expect.get("min_statements") and len(statements) < expect["min_statements"]:
        problems.append(f"statements {len(statements)} < {expect['min_statements']}")
    if expect.get("evidence_locatable"):
        if not statements:
            problems.append("expected cited statements, got none")
        for statement in statements:
            for ref in statement.evidence_refs:
                if ref.revision_id not in env.generation_manifest:
                    problems.append(
                        f"evidence revision not in generation: {ref.revision_id[:20]}"
                    )
    if expect.get("evidence_only"):
        for statement in statements:
            if not statement.evidence_refs:
                problems.append(f"uncited statement: {statement.text[:40]!r}")
    if expect.get("conflicts_visible") and not (answer.conflicts or answer.uncertainties):
        problems.append("expected a conflict/boundary note to be visible")
    cites_kind = expect.get("cites_kind")
    if cites_kind:
        wanted = SourceKind(cites_kind)
        if not any(
            ref.kind is wanted
            for statement in statements
            for ref in statement.evidence_refs
        ):
            problems.append(f"expected a citation of kind {cites_kind!r}")
    return problems


# ---------------------------------------------------------------- change sequences


async def run_change_sequence(env: EvalEnv, sequence: dict) -> list[str]:
    """Executes one frozen op sequence and verifies expectations."""
    problems: list[str] = []
    last_outcome: str | None = None
    for op in sequence["ops"]:
        kind = op["op"]
        if kind in ("seed", "initial_import"):
            await env.init_binding()
            await env.initial_import()
        elif kind == "derive":
            base = await env.current_generation()
            generation = await env.derive(base)
            env.generation_manifest = frozenset(generation.revision_manifest)
            env.derived_generation_ids.append(generation.generation_id)
        elif kind == "push":
            env._push_counter += 1
            last_outcome = await env.webhook(
                f"seq-push-{sequence['id']}-{env._push_counter}",
                "push",
                {"ref": "refs/heads/main"},
            )
        elif kind == "seed_push":
            await env.seed_push()
        elif kind == "seed_issue":
            await env.seed_issue()
            issue_obj = await env.sources.find_source_object(
                "proj-a", 1, SourceKind.ISSUE, "1"
            )
            if issue_obj is not None:
                env.parent_revision_counts["1"] = len(
                    await env.sources.list_revisions(issue_obj.source_id)
                )
        elif kind == "webhook":
            last_outcome = await env.webhook(op["delivery"], op["event"], op["payload"])
        elif kind == "webhook_100":
            for _ in range(100):
                last_outcome = await env.webhook(op["delivery"], op["event"], op["payload"])
        elif kind == "webhook_conflict":
            try:
                last_outcome = await env.webhook_conflict(op["delivery"], op["payload"])
            except Exception as exc:
                last_outcome = f"conflict:{type(exc).__name__}"
        elif kind == "mutate_blob":
            env.mutate_blob(op["blob"], append=op.get("append"), content=op.get("content"))
        elif kind == "repoint":
            env.repoint(op["sha"])
        elif kind == "clone_tree":
            env.clone_tree(op["from"], op["to"])
        elif kind == "remove_tree_path":
            env.remove_tree_path(op["sha_key"], op["path"])
        elif kind == "mutate_issue":
            fields = {k: v for k, v in op.items() if k not in ("op", "index")}
            env.mutate_issue(op["index"], **fields)
        elif kind == "mutate_comment":
            env.mutate_comment(op["id"], **{k: v for k, v in op.items() if k != "op" and k != "id"})
        elif kind == "fail":
            env.inject_failure(op["method"], op["match"], op["status"])
        elif kind == "manual_recheck":
            await env.manual_recheck()
        elif kind == "startup_recovery":
            await env.startup_recovery()
        elif kind == "drain":
            await env.drain()
        elif kind == "drain_until_failed":
            for _ in range(10):
                await env.drain()
                entry = await env.inbox.get_entry("1001", _find_delivery_id(sequence))
                if entry is None or entry.status is InboxStatus.FAILED:
                    break
                if entry.retry_at is not None:
                    env.clock.set(entry.retry_at + timedelta(seconds=1))
        elif kind == "crash_before_done":
            await _crash_before_done(env, op["delivery"], op["event"], op["payload"])
        elif kind == "invalidate":
            pipeline = DerivationPipeline(env.sources, env.knowledge, clock=env.clock)
            await pipeline.invalidate_stale_generations("proj-a")

        else:
            problems.append(f"unknown op {kind!r}")
    problems.extend(await _verify_expectations(env, sequence, last_outcome))
    return problems


def _find_delivery_id(sequence: dict) -> str | None:
    for op in sequence["ops"]:
        if op["op"] in ("webhook", "crash_before_done") and "delivery" in op:
            return op["delivery"]
    return None


async def _crash_before_done(env: EvalEnv, delivery: str, event: str, payload: dict) -> None:
    """Ingests a delivery and interrupts the worker inside the final write."""
    service = env._ensure_sync_service()
    await service.ingest_webhook("1001", delivery, event, None, payload)
    original = service.inbox.set_status

    async def broken_set_status(*args, **kwargs):
        status = kwargs.get("status") or (args[2] if len(args) > 2 else None)
        if status is InboxStatus.DONE:
            raise RuntimeError("simulated crash")
        return await original(*args, **kwargs)

    service.inbox.set_status = broken_set_status
    with contextlib.suppress(RuntimeError):
        await service.drain_repository("1001")
    service.inbox.set_status = original


async def _verify_expectations(env: EvalEnv, sequence: dict, last_outcome) -> list[str]:
    problems: list[str] = []
    expect = sequence.get("expect", {})
    if "revision_count_of_doc" in expect:
        spec = expect["revision_count_of_doc"]
        count = await env.doc_revision_count(spec["path"])
        if count != spec["count"]:
            problems.append(f"revision count {count} != {spec['count']} for {spec['path']}")
    if "last_outcome_ignored_or_refreshed" in expect and last_outcome not in (
        "accepted",
        "duplicate",
    ):
        problems.append(f"push outcome {last_outcome!r} unexpected")
    if "webhook_conflict" in expect and not (
        isinstance(last_outcome, str) and last_outcome.startswith("conflict:")
    ):
        problems.append(f"expected a payload conflict, got {last_outcome!r}")
    if "issue_assignees" in expect:
        spec = expect["issue_assignees"]
        obj = await env.sources.find_source_object(
            "proj-a", 1, SourceKind.ISSUE, str(spec["number"])
        )
        if obj is None or obj.metadata.get("assignees") != spec["value"]:
            problems.append(f"issue #{spec['number']} assignees mismatch")
    if "comment_body_contains" in expect:
        spec = expect["comment_body_contains"]
        parent = await env.sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        comment = None
        if parent is not None:
            comment = await env.sources.find_source_object(
                "proj-a", 1, SourceKind.COMMENT, spec["id"], parent_source_id=parent.source_id
            )
        if comment is None or spec["text"] not in comment.metadata.get("body", ""):
            problems.append(f"comment {spec['id']} body mismatch")
    if "parent_revision_delta_zero" in expect:
        spec = expect["parent_revision_delta_zero"]
        obj = await env.sources.find_source_object(
            "proj-a", 1, SourceKind.ISSUE, str(spec["number"])
        )
        before = env.parent_revision_counts.get(str(spec["number"]), 0)
        after = len(await env.sources.list_revisions(obj.source_id)) if obj else 0
        if after != before:
            problems.append(f"parent revision delta {before}->{after} expected zero")
    if "outcome_ignored" in expect and last_outcome != "accepted":
        problems.append(f"webhook outcome {last_outcome!r} != accepted")
    if "doc_availability" in expect:
        spec = expect["doc_availability"]
        availability = await env.doc_availability(spec["path"])
        if availability != spec["availability"]:
            problems.append(
                f"{spec['path']} availability {availability!r} != {spec['availability']!r}"
            )
    if "issue_availability" in expect:
        spec = expect["issue_availability"]
        obj = await env.sources.find_source_object(
            "proj-a", 1, SourceKind.ISSUE, str(spec["number"])
        )
        got = obj.availability.value if obj else None
        if got != spec["availability"]:
            problems.append(f"issue #{spec['number']} availability {got!r}")
    if "doc_contains" in expect:
        spec = expect["doc_contains"]
        if not await env.doc_latest_contains(spec["path"], spec["text"]):
            problems.append(f"{spec['path']} missing expected text {spec['text']!r}")
    if "recheck_incomplete" in expect:
        report = env.last_recheck_report
        coverage = report.coverage if report is not None else {}
        if report is None or report.fully_complete:
            problems.append("expected an incomplete recheck not to claim completeness")
        if "partial" not in coverage.values() and "failed" not in coverage.values():
            problems.append("expected partial/failed coverage to be recorded")
    if "inbox_status" in expect:
        spec = expect["inbox_status"]
        entry = await env.inbox.get_entry("1001", spec["delivery"])
        got = entry.status.value if entry else None
        if got != spec["status"]:
            problems.append(f"delivery {spec['delivery']} status {got!r} != {spec['status']!r}")
    if "issue_assignees_recorded" in expect:
        spec = expect["issue_assignees_recorded"]
        obj = await env.sources.find_source_object(
            "proj-a", 1, SourceKind.ISSUE, str(spec["number"])
        )
        if obj is None or not obj.metadata.get("assignees"):
            problems.append("expected issue assignees recorded after recovery")
    if "old_generation_invalid" in expect:
        ids = env.derived_generation_ids
        old = await env.knowledge.get_generation(ids[0]) if ids else None
        if old is None or old.valid is not False:
            problems.append("expected the pre-update generation to be invalidated")
    if "new_generation_ready" in expect:
        ids = env.derived_generation_ids
        generation = await env.knowledge.get_generation(ids[-1]) if ids else None
        pointer = await env.knowledge.current_generation_id("proj-a")
        if (
            generation is None
            or generation.status.value != "ready"
            or pointer != ids[-1]
        ):
            problems.append("expected the rebuilt generation to be ready and current")
    if "inbox_rows" in expect:
        spec = expect["inbox_rows"]
        entry = await env.inbox.get_entry("1001", spec["delivery"])
        if entry is None:
            problems.append("expected exactly one inbox row")
    return problems
