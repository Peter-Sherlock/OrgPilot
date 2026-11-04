"""Material index and search/read primitives over a published generation (C4).

The index holds only AVAILABLE sources of the fixed generation; sharing
authorization is enforced *before* recall at the tool layer, so an
unauthorized member never sees a title, snippet, or hit count from material
they cannot read (ADR-0015 §7.4).

Cursors are server-issued opaque tokens bound to (session, generation, tool,
filters) and HMAC-signed with a key derived from that binding, so a client
cannot forge an offset or reuse a cursor across sessions or generations.
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from orgpilot.knowledge.models import (
    SEARCH_QUERY_MAX_CHARS,
    SourceAvailability,
    SourceKind,
    SourceObject,
    SourceRevision,
)


def _cursor_key(session_id: str, generation_id: str, tool: str, filters: dict) -> bytes:
    material = json.dumps(
        {"session": session_id, "generation": generation_id, "tool": tool, "f": filters},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


def encode_cursor(
    session_id: str,
    generation_id: str,
    tool: str,
    filters: dict,
    offset: int,
) -> str:
    """Signs an opaque pagination token bound to the session and generation."""
    key = _cursor_key(session_id, generation_id, tool, filters)
    payload = json.dumps({"o": offset, "f": filters}, sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()[:16]
    return f"{body}.{signature}"


def decode_cursor(
    session_id: str,
    generation_id: str,
    tool: str,
    offset_filters: dict,
    token: str,
) -> int:
    """Verifies and decodes a cursor; returns the offset or raises ValueError.

    ``offset_filters`` must equal the filters of the current call — a cursor
    issued for different filters (or another session/generation/tool) fails
    verification instead of leaking page data.
    """
    if not token or "." not in token:
        raise ValueError("malformed cursor")
    body, signature = token.rsplit(".", 1)
    padding = "=" * (-len(body) % 4)
    try:
        payload = base64.urlsafe_b64decode(body + padding)
    except ValueError as exc:
        raise ValueError("malformed cursor body") from exc
    key = _cursor_key(session_id, generation_id, tool, offset_filters)
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(signature, expected):
        raise ValueError("cursor verification failed")
    decoded = json.loads(payload)
    if decoded.get("f") != offset_filters:
        raise ValueError("cursor filters mismatch")
    offset = decoded.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("invalid cursor offset")
    return offset


@dataclass
class IndexEntry:
    """One searchable segment of material at a pinned revision."""

    source_id: str
    revision_id: str
    kind: SourceKind
    external_id: str
    title: str | None
    paragraph: str | None
    line_start: int | None
    line_end: int | None
    text: str


class MaterialIndex:
    """Read-only search surface over one generation's available sources."""

    def __init__(self) -> None:
        self._entries: list[IndexEntry] = []
        self._raw: dict[str, str] = {}  # source_id -> raw content

    @classmethod
    def from_generation_material(
        cls,
        objects: list[SourceObject],
        revisions: dict[str, SourceRevision],
    ) -> "MaterialIndex":
        """Builds the index; only AVAILABLE objects enter it (§7.4)."""
        index = cls()
        from orgpilot.knowledge.derive import segment_markdown

        for obj in objects:
            if obj.availability is not SourceAvailability.AVAILABLE:
                continue
            revision = revisions.get(obj.source_id)
            if revision is None:
                continue
            if obj.kind is SourceKind.DOCUMENT and revision.raw_content:
                index._raw[obj.source_id] = revision.raw_content
                for segment in segment_markdown(
                    obj.source_id, revision.revision_id, revision.raw_content
                ):
                    index._entries.append(
                        IndexEntry(
                            source_id=obj.source_id,
                            revision_id=revision.revision_id,
                            kind=obj.kind,
                            external_id=obj.external_id,
                            title=segment.title or obj.title,
                            paragraph=segment.locator,
                            line_start=segment.start_line,
                            line_end=segment.end_line,
                            text=segment.text,
                        )
                    )
            else:
                body = revision.metadata.get("body") or ""
                text = f"{obj.title or obj.external_id}\n{body}".strip()
                index._raw[obj.source_id] = text
                if text:
                    index._entries.append(
                        IndexEntry(
                            source_id=obj.source_id,
                            revision_id=revision.revision_id,
                            kind=obj.kind,
                            external_id=obj.external_id,
                            title=obj.title,
                            paragraph=None,
                            line_start=None,
                            line_end=None,
                            text=text,
                        )
                    )
        return index

    @property
    def entries(self) -> list[IndexEntry]:
        return list(self._entries)

    def raw_content(self, source_id: str) -> str | None:
        return self._raw.get(source_id)

    def source_ids(self) -> set[str]:
        return {entry.source_id for entry in self._entries}

    @staticmethod
    def _match_terms(query: str) -> list[str]:
        """Whitespace tokens plus CJK 2-char shingles for unspaced queries."""
        lowered = query.strip().lower()
        terms: list[str] = []
        for token in lowered.split():
            terms.append(token)
            if len(token) > 2:
                terms.extend(token[i : i + 2] for i in range(len(token) - 1))
        return terms

    def search(self, query: str, kinds: tuple[SourceKind, ...] | None = None) -> list[IndexEntry]:
        """Text search; query length enforced by the tool layer (§7.4)."""
        cleaned = query.strip()
        if not cleaned:
            return []
        if len(cleaned) > SEARCH_QUERY_MAX_CHARS:
            cleaned = cleaned[:SEARCH_QUERY_MAX_CHARS]
        lowered = cleaned.lower()
        terms = self._match_terms(cleaned)
        hits: list[IndexEntry] = []
        for entry in self._entries:
            if kinds is not None and entry.kind not in kinds:
                continue
            haystack = f"{entry.title or ''}\n{entry.text}".lower()
            if lowered in haystack or (terms and any(term in haystack for term in terms)):
                hits.append(entry)
        # No per-page cap here, ranking only: the tool layer slices pages of
        # SEARCH_MAX_HITS and signs cursors for the rest (§7.4). Rank order is
        # deterministic and kind-weighted: authoritative documents first, then
        # work records, then conversation sub-objects (comments last).
        weight = {
            SourceKind.DOCUMENT: 0,
            SourceKind.ISSUE: 1,
            SourceKind.PULL_REQUEST: 2,
            SourceKind.REVIEW: 3,
            SourceKind.COMMENT: 4,
        }
        hits.sort(key=lambda e: (weight[e.kind], e.source_id, e.external_id, e.line_start or 0))
        return hits
