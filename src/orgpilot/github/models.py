"""Frozen payload contracts for read-only GitHub access (ADR-0015 §2.1)."""

from datetime import datetime

from pydantic import field_validator

from orgpilot.extraction.models import StrictModel


def _require_tz(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    raise ValueError("timestamps must include an explicit timezone")


class GitHubPayload(StrictModel):
    """Base for platform payloads with timezone-required timestamps."""


class RepositoryInfo(GitHubPayload):
    """Stable repository identity; full_name may change without new identity."""

    repository_id: str
    full_name: str
    default_branch: str


class CommitInfo(GitHubPayload):
    """One pinned commit used to read documents consistently (§5.2)."""

    sha: str


class TreeEntry(GitHubPayload):
    """One path/blob entry of the pinned commit's tree."""

    path: str
    blob_sha: str
    size: int


class IssuePayload(GitHubPayload):
    """One issue entry; ``is_pull_request`` routes PRs out of issue scope (§5.2)."""

    number: int
    issue_id: str
    title: str
    body: str | None = None
    state: str
    milestone: str | None = None
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    html_url: str | None = None
    is_pull_request: bool = False
    updated_at: datetime
    closed_at: datetime | None = None

    @field_validator("updated_at", "closed_at")
    @classmethod
    def _tz_required(cls, value: datetime | None) -> datetime | None:
        return _require_tz(value)


class CommentPayload(GitHubPayload):
    """One ordinary conversation comment under an issue or pull request."""

    comment_id: str
    issue_number: int
    author: str | None = None
    body: str | None = None
    html_url: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz_required(cls, value: datetime) -> datetime:
        return _require_tz(value)


class PullRequestPayload(GitHubPayload):
    """One pull request with merge facts; merging proves merging only (§3)."""

    number: int
    pr_id: str
    title: str
    body: str | None = None
    state: str
    merged: bool = False
    merge_commit_sha: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    html_url: str | None = None
    updated_at: datetime
    closed_at: datetime | None = None

    @field_validator("updated_at", "closed_at")
    @classmethod
    def _tz_required(cls, value: datetime | None) -> datetime | None:
        return _require_tz(value)


class ReviewPayload(GitHubPayload):
    """One PR review record; approval ≠ business acceptance (§3)."""

    review_id: str
    pr_number: int
    author: str | None = None
    state: str
    body: str | None = None
    submitted_at: datetime

    @field_validator("submitted_at")
    @classmethod
    def _tz_required(cls, value: datetime) -> datetime:
        return _require_tz(value)
