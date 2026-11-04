"""The four read-only lookup tools (ADR-0015 §7.4).

Server-injected session context (project, requester, binding version, access
epoch, generation) is never taken from model arguments; unknown argument keys
are rejected, so a model cannot smuggle write parameters or expand its scope.
All boundary violations return typed tool errors — never a generic executor,
never a fallback that runs the request anyway:

- ``invalid_parameter``: malformed/oversized input, unknown argument keys,
  shell/URL/SQL-looking content, forged or foreign cursors;
- ``unavailable``: out-of-scope, unauthorized, revoked, or missing sources —
  one unified result that leaks neither titles nor existence;
- ``tool_denied``: reads that bypass the whitelist (e.g. raw URLs) or
  params outside the tool contract;
- ``result_overflow``: a single item too large to return verifiably — we
  refuse rather than return a half-unverifiable record.

Results are capped at 4,096 serialized chars (identity, locator, and
availability flags are never silently dropped to fit — snippets are). The
per-session cache short-circuits identical normalized calls but still counts
as a tool call and re-validates availability before returning.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from orgpilot.domain.clock import Clock
from orgpilot.domain.models import OrgState
from orgpilot.knowledge.access import check_binding, check_generation
from orgpilot.knowledge.models import (
    READ_SOURCE_MAX_CHARS,
    READ_TASK_STATE_MAX,
    RELATED_ITEMS_MAX,
    SEARCH_MAX_HITS,
    SEARCH_QUERY_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
    KnowledgeGeneration,
    KnowledgeRelation,
    ProjectSourceBinding,
    RelationType,
    SourceAvailability,
    SourceKind,
)
from orgpilot.knowledge.retrieval import MaterialIndex, decode_cursor, encode_cursor
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore

_TOOL_NAMES = ("search_project", "read_source", "get_related_items", "read_task_state")

_URL_MARKERS = ("http://", "https://", "www.", "ftp://")
_SHELL_MARKERS = (";", "|", "&", "$(", "`", "&&", "||", "\nrm ", "rm -rf", "sudo ")
_SQL_MARKERS = ("--", "/*", "select ", "union select", "drop table", "insert into", "delete from")


def guard_text(value: str) -> str | None:
    """Returns a denial reason when text carries URLs/shell/SQL content."""
    lowered = value.lower()
    for marker in _URL_MARKERS:
        if marker in lowered:
            return "raw URLs are not accepted"
    for marker in _SHELL_MARKERS:
        if marker in lowered:
            return "shell-like content is not accepted"
    for marker in _SQL_MARKERS:
        if marker in lowered:
            return "SQL-like content is not accepted"
    return None


@dataclass(frozen=True)
class ToolCallResult:
    """Typed result of one tool call; errors are values, never exceptions."""

    ok: bool
    tool: str
    error_code: str | None = None
    detail: str | None = None
    items: tuple[dict[str, Any], ...] = ()
    next_cursor: str | None = None
    truncated: bool = False
    cached: bool = False
    chars: int = 0
    # Progress identity: (revision_id, locator) pairs the loop uses to decide
    # whether a round produced NEW evidence (ADR-0015 §7.4).
    evidence_keys: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "error_code": self.error_code,
            "detail": self.detail,
            "items": list(self.items),
            "next_cursor": self.next_cursor,
            "truncated": self.truncated,
            "cached": self.cached,
        }


@dataclass
class ReadToolSession:
    """Server-injected per-session context; models never supply these."""

    session_id: str
    project_id: str
    requester_id: str
    binding: ProjectSourceBinding
    generation: KnowledgeGeneration
    index: MaterialIndex
    org_state: OrgState | None
    relations: tuple[KnowledgeRelation, ...] = ()
    sources: SqlProjectSourceStore | None = None
    clock: Clock | None = None
    context_check: Callable[[], Awaitable[Any]] | None = None
    max_evidence_chars: int = 12000
    max_fragments: int = 12
    cache: dict[tuple[str, str], ToolCallResult] = field(default_factory=dict)


def _evidence_key(revision_id: str, locator: str, snippet: str) -> str:
    digest = hashlib.sha1(snippet.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - non-crypto
    return f"{revision_id}|{locator}|{digest}"


def _fit_payload(result: ToolCallResult) -> ToolCallResult:
    """Shrinks snippets, then items, to fit the serialized cap; flags truncation.

    Identity (revision/source ids, locators, availability) is preserved; only
    snippet text shrinks. A single item that cannot fit at all returns
    ``result_overflow`` instead of a half-verifiable record (§7.4).
    """
    payload = result.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if len(serialized) <= TOOL_RESULT_MAX_CHARS:
        return ToolCallResult(**{**result.__dict__, "chars": len(serialized)})
    # Pass 1: shrink snippets to 100 chars.
    shrunk_items = []
    for item in result.items:
        item = dict(item)
        if "snippet" in item and isinstance(item["snippet"], str):
            item["snippet"] = item["snippet"][:100]
        shrunk_items.append(item)
    candidate = ToolCallResult(**{**result.__dict__, "items": tuple(shrunk_items)})
    serialized = json.dumps(candidate.to_payload(), ensure_ascii=False, default=str)
    if len(serialized) <= TOOL_RESULT_MAX_CHARS:
        return ToolCallResult(**{**candidate.__dict__, "truncated": True, "chars": len(serialized)})
    # Pass 2: drop tail items while at least one identity-bearing item stays;
    # a single item that still cannot fit is a refused overflow, never a
    # half-verifiable record (§7.4).
    while len(shrunk_items) > 1:
        shrunk_items = shrunk_items[:-1]
        candidate = ToolCallResult(**{**candidate.__dict__, "items": tuple(shrunk_items)})
        serialized = json.dumps(candidate.to_payload(), ensure_ascii=False, default=str)
        if len(serialized) <= TOOL_RESULT_MAX_CHARS:
            return ToolCallResult(
                **{**candidate.__dict__, "truncated": True, "chars": len(serialized)}
            )
    return ToolCallResult(
        ok=False,
        tool=result.tool,
        error_code="result_overflow",
        detail="a single result item exceeds the serialized size cap",
    )


def _deny(tool: str, code: str, detail: str) -> ToolCallResult:
    return ToolCallResult(ok=False, tool=tool, error_code=code, detail=detail)


async def _unified_unavailable(tool: str) -> ToolCallResult:
    """One result for out-of-scope/unauthorized/missing/revoked sources."""
    return _deny(tool, "unavailable", "requested source is not available in this session")


def _canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


class ReadTools:
    """Dispatches validated tool calls for one read session."""

    def __init__(self, session: ReadToolSession) -> None:
        self.session = session

    async def _context_problem(self):
        if self.session.context_check:
            return await self.session.context_check()
        problem = await check_binding(
            self.session.sources,
            self.session.binding,
            self.session.requester_id,
        )
        if problem or self.session.sources is None:
            return problem
        return await check_generation(
            self.session.sources,
            SqlKnowledgeStore(self.session.sources.db, self.session.clock),
            self.session.generation,
        )

    async def call(self, tool: str, args: dict[str, Any]) -> ToolCallResult:
        """Validates and executes one call; cache hits count as calls."""
        if await self._context_problem():
            return await _unified_unavailable(tool)
        if tool not in _TOOL_NAMES:
            return _deny(tool, "tool_denied", f"unknown tool {tool!r}")
        allowed_keys = {
            "search_project": {"query", "kinds", "cursor"},
            "read_source": {"source_id", "revision_id", "cursor"},
            "get_related_items": {"source_id", "relation_type", "cursor"},
            "read_task_state": {"task_ids"},
        }[tool]
        unknown = set(args) - allowed_keys
        if unknown:
            return _deny(tool, "invalid_parameter", f"unknown arguments: {sorted(unknown)}")
        cache_key = (tool, _canonical_args(args))
        cached = self.session.cache.get(cache_key)
        if cached is not None and await self._cache_still_valid(cached):
            result = ToolCallResult(**{**cached.__dict__, "cached": True})
        else:
            self.session.cache.pop(cache_key, None)
            result = await self._dispatch(tool, args)
        if await self._context_problem():
            return await _unified_unavailable(tool)
        body_chars = sum(
            len(
                item.get("snippet")
                or item.get("content")
                or (json.dumps(item, ensure_ascii=False) if "task_id" in item else "")
            )
            for item in result.items
        )
        if body_chars > self.session.max_evidence_chars or len(result.items) > (
            self.session.max_fragments
        ):
            return _deny(
                tool, "budget_exhausted", "returned data exceeds remaining evidence budget"
            )
        if result.ok:
            self.session.cache[cache_key] = result
        return result

    async def _source_available(self, source_id: str) -> bool:
        """Re-validates availability against the store before returning data."""
        sources = self.session.sources
        if sources is None:
            return True
        obj = await sources.get_source_object(source_id)
        if obj is None:
            return False
        return obj.availability is SourceAvailability.AVAILABLE

    async def _cache_still_valid(self, cached: ToolCallResult) -> bool:
        """Cached reads re-check availability before returning (§7.4)."""
        sources = self.session.sources
        if sources is None:
            return True
        for item in cached.items:
            source_id = item.get("source_id")
            if source_id is None:
                continue
            obj = await sources.get_source_object(str(source_id))
            if obj is None or obj.availability is not SourceAvailability.AVAILABLE:
                return False
        return True

    async def _dispatch(self, tool: str, args: dict[str, Any]) -> ToolCallResult:
        if tool == "search_project":
            return await self.search_project(args)
        if tool == "read_source":
            return await self.read_source(args)
        if tool == "get_related_items":
            return await self.get_related_items(args)
        return await self.read_task_state(args)

    # ------------------------------------------------------------------ tools

    async def search_project(self, args: dict[str, Any]) -> ToolCallResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _deny("search_project", "invalid_parameter", "query must be a non-empty string")
        if len(query) > SEARCH_QUERY_MAX_CHARS:
            return _deny(
                "search_project",
                "invalid_parameter",
                f"query exceeds {SEARCH_QUERY_MAX_CHARS} chars",
            )
        denial = guard_text(query)
        if denial:
            return _deny("search_project", "tool_denied", denial)
        kinds = args.get("kinds")
        kind_filter: tuple[SourceKind, ...] | None = None
        filters: dict[str, Any] = {"kinds": None, "query": query}
        if kinds is not None:
            if not isinstance(kinds, list) or not kinds:
                return _deny("search_project", "invalid_parameter", "kinds must be a list")
            try:
                kind_filter = tuple(SourceKind(kind) for kind in kinds)
            except ValueError:
                return _deny("search_project", "invalid_parameter", "unknown kind value")
            filters = {"kinds": sorted(k.value for k in kind_filter), "query": query}
        cursor_token = args.get("cursor")
        offset = 0
        if cursor_token is not None:
            try:
                offset = decode_cursor(
                    self.session.session_id,
                    self.session.generation.generation_id,
                    "search_project",
                    filters,
                    str(cursor_token),
                )
            except ValueError as exc:
                return _deny("search_project", "invalid_parameter", str(exc))
        all_hits = self.session.index.search(query, kind_filter)
        # Fresh calls re-check availability too: a source revoked since index
        # build must not surface its hits (§7.4: checks before AND after).
        live_hits = [entry for entry in all_hits if await self._source_available(entry.source_id)]
        page = live_hits[offset : offset + min(SEARCH_MAX_HITS, self.session.max_fragments)]
        items = []
        evidence_keys = []
        chars_left = self.session.max_evidence_chars
        for entry in page:
            if chars_left <= 0:
                break
            snippet = entry.text[: min(200, chars_left)]
            chars_left -= len(snippet)
            items.append(
                {
                    "source_id": entry.source_id,
                    "revision_id": entry.revision_id,
                    "kind": entry.kind.value,
                    "title": entry.title,
                    "external_id": entry.external_id,
                    "paragraph": entry.paragraph,
                    "snippet": snippet,
                }
            )
            locator = entry.paragraph or entry.external_id
            evidence_keys.append(_evidence_key(entry.revision_id, locator, snippet))
        remaining = len(live_hits) - (offset + len(items))
        next_cursor = None
        if remaining > 0:
            next_cursor = encode_cursor(
                self.session.session_id,
                self.session.generation.generation_id,
                "search_project",
                filters,
                offset + len(items),
            )
        result = ToolCallResult(
            ok=True,
            tool="search_project",
            items=tuple(items),
            next_cursor=next_cursor,
            evidence_keys=tuple(evidence_keys),
        )
        fitted = _fit_payload(result)
        if fitted.ok and len(fitted.items) < len(items):
            fitted = ToolCallResult(
                **{
                    **fitted.__dict__,
                    "next_cursor": encode_cursor(
                        self.session.session_id,
                        self.session.generation.generation_id,
                        "search_project",
                        filters,
                        offset + len(fitted.items),
                    ),
                }
            )
        return fitted

    async def read_source(self, args: dict[str, Any]) -> ToolCallResult:
        source_id = args.get("source_id")
        if not isinstance(source_id, str) or not source_id.startswith("src-"):
            return _deny(
                "read_source", "invalid_parameter", "source_id must be a session source id"
            )
        entry = next(
            (item for item in self.session.index.entries if item.source_id == source_id),
            None,
        )
        if entry is None or not await self._source_available(source_id):
            return await _unified_unavailable("read_source")
        revision_id = args.get("revision_id")
        if revision_id is None:
            revision_id = entry.revision_id
        if not isinstance(revision_id, str) or not revision_id.startswith("rev-"):
            return _deny("read_source", "invalid_parameter", "revision_id must be a revision id")
        if revision_id != entry.revision_id:
            # Not the generation's revision for this source: not readable here.
            return await _unified_unavailable("read_source")
        cursor_token = args.get("cursor")
        filters = {"source_id": source_id, "revision_id": revision_id}
        offset = 0
        if cursor_token is not None:
            try:
                offset = decode_cursor(
                    self.session.session_id,
                    self.session.generation.generation_id,
                    "read_source",
                    filters,
                    str(cursor_token),
                )
            except ValueError as exc:
                return _deny("read_source", "invalid_parameter", str(exc))
        raw = self.session.index.raw_content(source_id) or ""
        window = raw[offset : offset + min(READ_SOURCE_MAX_CHARS, self.session.max_evidence_chars)]
        start_line = raw[:offset].count("\n") + 1
        end_line = start_line + window.count("\n")
        truncated = offset + len(window) < len(raw)
        next_cursor = None
        if truncated:
            next_cursor = encode_cursor(
                self.session.session_id,
                self.session.generation.generation_id,
                "read_source",
                filters,
                offset + len(window),
            )
        item = {
            "source_id": source_id,
            "revision_id": revision_id,
            "kind": entry.kind.value,
            "title": entry.title,
            "external_id": entry.external_id,
            "char_offset": offset,
            "line_start": start_line,
            "line_end": end_line,
            "content": window,
            "has_more": truncated,
        }
        result = ToolCallResult(
            ok=True,
            tool="read_source",
            items=(item,),
            next_cursor=next_cursor,
            truncated=truncated,
            evidence_keys=(_evidence_key(revision_id, f"offset:{offset}", window[:200]),),
        )
        return _fit_payload(result)

    async def get_related_items(self, args: dict[str, Any]) -> ToolCallResult:
        source_id = args.get("source_id")
        if not isinstance(source_id, str) or not source_id.startswith("src-"):
            return _deny(
                "get_related_items", "invalid_parameter", "source_id must be a session source id"
            )
        known_sources = self.session.index.source_ids()
        if source_id not in known_sources or not await self._source_available(source_id):
            return await _unified_unavailable("get_related_items")
        relation_type = args.get("relation_type")
        type_filter: RelationType | None = None
        filters: dict[str, Any] = {"source_id": source_id, "relation_type": None}
        if relation_type is not None:
            try:
                type_filter = RelationType(str(relation_type))
            except ValueError:
                return _deny("get_related_items", "invalid_parameter", "unknown relation_type")
            filters = {"source_id": source_id, "relation_type": type_filter.value}
        cursor_token = args.get("cursor")
        offset = 0
        if cursor_token is not None:
            try:
                offset = decode_cursor(
                    self.session.session_id,
                    self.session.generation.generation_id,
                    "get_related_items",
                    filters,
                    str(cursor_token),
                )
            except ValueError as exc:
                return _deny("get_related_items", "invalid_parameter", str(exc))
        relations = [
            relation
            for relation in self.session.relations
            if relation.from_ref == source_id or relation.to_ref == source_id
        ]
        if type_filter is not None:
            relations = [r for r in relations if r.relation_type is type_filter]
        relations.sort(key=lambda r: r.relation_id)
        page = relations[offset : offset + min(RELATED_ITEMS_MAX, self.session.max_fragments)]
        items = [
            {
                "relation_id": relation.relation_id,
                "from_ref": relation.from_ref,
                "relation_type": relation.relation_type.value,
                "to_ref": relation.to_ref,
                "confirmation_status": relation.confirmation_status.value,
                # Candidates never enter the formal DAG; the flag travels so
                # downstream consumers cannot mistake them for dependencies.
                "is_candidate": relation.relation_type is RelationType.CANDIDATE_DEPENDENCY,
            }
            for relation in page
        ]
        remaining = len(relations) - (offset + len(page))
        next_cursor = None
        if remaining > 0:
            next_cursor = encode_cursor(
                self.session.session_id,
                self.session.generation.generation_id,
                "get_related_items",
                filters,
                offset + len(page),
            )
        evidence_keys = tuple(
            _evidence_key(r.evidence_refs[0].revision_id, f"rel:{r.relation_id}", r.relation_id)
            for r in page
            if r.evidence_refs
        )
        result = ToolCallResult(
            ok=True,
            tool="get_related_items",
            items=tuple(items),
            next_cursor=next_cursor,
            evidence_keys=evidence_keys,
        )
        return _fit_payload(result)

    async def read_task_state(self, args: dict[str, Any]) -> ToolCallResult:
        task_ids = args.get("task_ids")
        if not isinstance(task_ids, list) or not task_ids:
            return _deny(
                "read_task_state", "invalid_parameter", "task_ids must be a non-empty list"
            )
        if len(task_ids) > READ_TASK_STATE_MAX:
            return _deny(
                "read_task_state",
                "invalid_parameter",
                f"at most {READ_TASK_STATE_MAX} tasks per call",
            )
        for task_id in task_ids:
            if not isinstance(task_id, str) or not task_id.strip():
                return _deny("read_task_state", "invalid_parameter", "task ids must be strings")
            denial = guard_text(task_id)
            if denial:
                return _deny("read_task_state", "tool_denied", denial)
        if self.session.org_state is None:
            return _deny("read_task_state", "unavailable", "no formal task state in session")
        items = []
        missing = []
        for task_id in task_ids:
            task = self.session.org_state.tasks.get(str(task_id))
            if task is None:
                missing.append(str(task_id))
                continue
            items.append(
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "owner_id": task.owner_id,
                    "workflow_status": (
                        task.workflow_status.value
                        if hasattr(task.workflow_status, "value")
                        else str(task.workflow_status)
                    ),
                    "health_status": (
                        task.health_status.value
                        if hasattr(task.health_status, "value")
                        else str(task.health_status)
                    ),
                    "deadline": task.deadline.isoformat() if task.deadline else None,
                    "dependencies": list(task.dependencies),
                    "event_refs": list(task.source_event_ids),
                }
            )
        result = ToolCallResult(
            ok=True,
            tool="read_task_state",
            items=tuple(items),
            detail=(f"not registered in formal state: {', '.join(missing)}" if missing else None),
            evidence_keys=tuple(
                f"task|{item['task_id']}|{item['event_refs'][-1] if item['event_refs'] else 'none'}"
                for item in items
            ),
        )
        return _fit_payload(result)
