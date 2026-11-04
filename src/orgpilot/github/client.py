"""Read-only GitHub client: protocol, permission catalog, mock, and HTTP impl.

First-version scope (ADR-0015 §2.2): a GitHub App installed on the single
bound repository, reading Metadata, Contents, Issues, and Pull requests.
``ENDPOINT_CATALOG`` freezes the endpoint ↔ permission mapping so the C2
acceptance can verify, item by item, that every client method maps to one of
the four read permissions and that nothing in this package writes.

Two implementations exist:

- :class:`MockGitHubClient` runs offline against a fixed snapshot and is the
  only client exercised by tests; it can simulate pagination, 403/404, and
  oversized payloads.
- :class:`HttpGitHubClient` speaks the real REST API via httpx. It is wired
  for later live acceptance (C6 optional) and is never exercised in tests:
  live use needs explicit scope, credentials, and budget (development-plan §2).

Token exchange (App → installation token) lives in
:class:`CredentialProvider` implementations and is deliberately separate from
content reads: obtaining a token never grants a business write.
"""

import base64
import time
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from orgpilot.domain.errors import OrgPilotError
from orgpilot.github.models import (
    CommentPayload,
    CommitInfo,
    IssuePayload,
    PullRequestPayload,
    RepositoryInfo,
    ReviewPayload,
    TreeEntry,
)

# Bounded retry per request (ADR-0015 §5.4: at most 3 attempts, then wait for
# the next round or a human). Content reads retry; the read loop never does.
MAX_HTTP_ATTEMPTS = 3
DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 100


class GitHubReadError(OrgPilotError):
    """A read failed with a platform status; 403/404 is never "deleted" (§5.4)."""

    def __init__(self, endpoint: str, status_code: int | None, message: str) -> None:
        super().__init__(f"{endpoint} failed (status={status_code}): {message}")
        self.endpoint = endpoint
        self.status_code = status_code


def is_access_failure(status_code: int | None) -> bool:
    """403/404 may be permissions — availability drops, deletion is not inferred."""
    return status_code in (403, 404)


@runtime_checkable
class GitHubReadClient(Protocol):
    """Read-only surface the importer may use; nothing else exists."""

    def get_repository(self) -> RepositoryInfo: ...
    def get_branch_head(self, branch: str) -> CommitInfo: ...
    def get_tree(self, commit_sha: str) -> list[TreeEntry]: ...
    def get_blob(self, blob_sha: str) -> str: ...
    def list_issues(self, state: str, page: int, per_page: int) -> list[IssuePayload]: ...
    def get_issue(self, number: int) -> IssuePayload: ...
    def list_issue_comments(
        self, issue_number: int, page: int, per_page: int
    ) -> list[CommentPayload]: ...
    def get_pull_request(self, number: int) -> PullRequestPayload: ...
    def list_pull_request_reviews(
        self, pr_number: int, page: int, per_page: int
    ) -> list[ReviewPayload]: ...


class GitHubPermission(StrEnum):
    """The four read permissions requested for the App (ADR-0015 §2.2)."""

    METADATA = "metadata"
    CONTENTS = "contents"
    ISSUES = "issues"
    PULL_REQUESTS = "pull_requests"


class EndpointSpec:
    """One catalog entry: which client method hits which endpoint, why."""

    def __init__(self, method: str, endpoint: str, permission: GitHubPermission) -> None:
        self.method = method
        self.endpoint = endpoint
        self.permission = permission


ENDPOINT_CATALOG: tuple[EndpointSpec, ...] = (
    EndpointSpec(
        "get_repository", "GET /repos/{owner}/{repo}", GitHubPermission.METADATA
    ),
    EndpointSpec(
        "get_branch_head",
        "GET /repos/{owner}/{repo}/branches/{branch}",
        GitHubPermission.METADATA,
    ),
    EndpointSpec(
        "get_tree",
        "GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1",
        GitHubPermission.CONTENTS,
    ),
    EndpointSpec(
        "get_blob", "GET /repos/{owner}/{repo}/git/blobs/{sha}", GitHubPermission.CONTENTS
    ),
    EndpointSpec(
        "list_issues",
        "GET /repos/{owner}/{repo}/issues?state=all&page=N",
        GitHubPermission.ISSUES,
    ),
    EndpointSpec(
        "get_issue", "GET /repos/{owner}/{repo}/issues/{number}", GitHubPermission.ISSUES
    ),
    EndpointSpec(
        "list_issue_comments",
        "GET /repos/{owner}/{repo}/issues/{number}/comments?page=N",
        GitHubPermission.ISSUES,
    ),
    EndpointSpec(
        "get_pull_request",
        "GET /repos/{owner}/{repo}/pulls/{number}",
        GitHubPermission.PULL_REQUESTS,
    ),
    EndpointSpec(
        "list_pull_request_reviews",
        "GET /repos/{owner}/{repo}/pulls/{number}/reviews?page=N",
        GitHubPermission.PULL_REQUESTS,
    ),
)

assert {spec.method for spec in ENDPOINT_CATALOG} == {
    name
    for name, _ in vars(GitHubReadClient).items()
    if not name.startswith("_") and callable(getattr(GitHubReadClient, name, None))
}, "ENDPOINT_CATALOG must cover exactly the GitHubReadClient methods"


class StaticTokenCredentialProvider:
    """Supplies a static installation token; real exchange is external to reads."""

    def __init__(self, token_provider: Callable[[], str]) -> None:
        self._token_provider = token_provider

    def installation_token(self) -> str:
        return self._token_provider()


class MockGitHubClient:
    """Offline client over a fixed snapshot; the only client tests exercise.

    Snapshot shape (all optional keys default empty)::

        {
          "repository": {"repository_id": "1001", "full_name": "org/demo",
                         "default_branch": "main"},
          "branches": {"main": {"sha": "c0ffee"}},
          "trees": {"c0ffee": [{"path": "README.md", "blob_sha": "b1", "size": 100}]},
          "blobs": {"b1": "# text"},
          "issues": [ {...}, ...],          # includes PR entries with is_pull_request
          "issue_comments": [ {...}, ...],
          "pull_requests": [ {...}, ...],
          "pull_request_reviews": [ {...}, ...],
          "failures": [{"method": "...", "match": {...}, "status": 403}],
        }

    Pagination slices by ``page``/``per_page`` like the REST API. A ``failures``
    entry raises :class:`GitHubReadError` whenever the named method is called
    with matching keyword arguments — used to simulate 403/404 and outages.
    """

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- failure simulation ------------------------------------------------------

    def _check_failure(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        # Failures are read live from the snapshot so tests may inject them
        # after construction (mutating the snapshot dict they hold).
        for spec in self._snapshot.get("failures", []):
            if spec.get("method") != method:
                continue
            match = spec.get("match", {})
            if all(kwargs.get(key) == value for key, value in match.items()):
                raise GitHubReadError(method, spec.get("status"), spec.get("message", "simulated"))

    @staticmethod
    def _page(items: list[Any], page: int, per_page: int) -> list[Any]:
        if page < 1 or per_page < 1:
            raise GitHubReadError("pagination", 400, "page/per_page must be >= 1")
        start = (page - 1) * per_page
        return items[start : start + per_page]

    # -- reads ---------------------------------------------------------------------

    def get_repository(self) -> RepositoryInfo:
        self._check_failure("get_repository")
        repo = self._snapshot.get("repository", {})
        return RepositoryInfo(
            repository_id=str(repo.get("repository_id", "0")),
            full_name=repo.get("full_name", "unknown/unknown"),
            default_branch=repo.get("default_branch", "main"),
        )

    def get_branch_head(self, branch: str) -> CommitInfo:
        self._check_failure("get_branch_head", branch=branch)
        branches = self._snapshot.get("branches", {})
        if branch not in branches:
            raise GitHubReadError("get_branch_head", 404, f"branch {branch} not found")
        return CommitInfo(sha=branches[branch]["sha"])

    def get_tree(self, commit_sha: str) -> list[TreeEntry]:
        self._check_failure("get_tree", commit_sha=commit_sha)
        tree = self._snapshot.get("trees", {}).get(commit_sha)
        if tree is None:
            raise GitHubReadError("get_tree", 404, f"commit {commit_sha} not found")
        return [
            TreeEntry(path=entry["path"], blob_sha=entry["blob_sha"], size=entry["size"])
            for entry in tree
        ]

    def get_blob(self, blob_sha: str) -> str:
        self._check_failure("get_blob", blob_sha=blob_sha)
        blobs = self._snapshot.get("blobs", {})
        if blob_sha not in blobs:
            raise GitHubReadError("get_blob", 404, f"blob {blob_sha} not found")
        return blobs[blob_sha]

    def list_issues(self, state: str, page: int, per_page: int) -> list[IssuePayload]:
        self._check_failure("list_issues", state=state, page=page, per_page=per_page)
        issues = [self._issue_from_snapshot(item) for item in self._snapshot.get("issues", [])]
        selected = [issue for issue in issues if state == "all" or issue.state == state]
        selected.sort(key=lambda item: item.number)
        return self._page(selected, page, per_page)

    def get_issue(self, number: int) -> IssuePayload:
        self._check_failure("get_issue", number=number)
        for item in self._snapshot.get("issues", []):
            if "pull_request" in item:
                continue
            payload = self._issue_from_snapshot(item)
            if payload.number == number:
                return payload
        raise GitHubReadError("get_issue", 404, f"issue {number} not found")

    def list_issue_comments(
        self, issue_number: int, page: int, per_page: int
    ) -> list[CommentPayload]:
        self._check_failure("list_issue_comments", issue_number=issue_number, page=page)
        comments = [
            self._comment_from_snapshot(item)
            for item in self._snapshot.get("issue_comments", [])
            if item["issue_number"] == issue_number
        ]
        comments.sort(key=lambda item: item.created_at)
        return self._page(comments, page, per_page)

    def get_pull_request(self, number: int) -> PullRequestPayload:
        self._check_failure("get_pull_request", number=number)
        for item in self._snapshot.get("pull_requests", []):
            payload = PullRequestPayload.model_validate(item)
            if payload.number == number:
                return payload
        raise GitHubReadError("get_pull_request", 404, f"pull request {number} not found")

    def list_pull_request_reviews(
        self, pr_number: int, page: int, per_page: int
    ) -> list[ReviewPayload]:
        self._check_failure("list_pull_request_reviews", pr_number=pr_number, page=page)
        reviews = [
            self._review_from_snapshot(item)
            for item in self._snapshot.get("pull_request_reviews", [])
            if item["pr_number"] == pr_number
        ]
        reviews.sort(key=lambda item: item.submitted_at)
        return self._page(reviews, page, per_page)

    @classmethod
    def _issue_from_snapshot(cls, item: dict[str, Any]) -> IssuePayload:
        """Maps a REST-shaped snapshot entry; ``pull_request`` keys route PRs."""
        entry = {key: value for key, value in item.items() if key != "pull_request"}
        entry.setdefault("is_pull_request", "pull_request" in item)
        entry["issue_id"] = str(entry.get("issue_id", entry.get("id", "0")))
        return IssuePayload.model_validate(entry)

    @staticmethod
    def _comment_from_snapshot(item: dict[str, Any]) -> CommentPayload:
        author = item.get("user") or {}
        return CommentPayload(
            comment_id=str(item["id"]),
            issue_number=item["issue_number"],
            author=author.get("login") if isinstance(author, dict) else str(author),
            body=item.get("body"),
            html_url=item.get("html_url"),
            created_at=item["created_at"],
            updated_at=item["updated_at"],
        )

    @staticmethod
    def _review_from_snapshot(item: dict[str, Any]) -> ReviewPayload:
        author = item.get("user") or {}
        return ReviewPayload(
            review_id=str(item["id"]),
            pr_number=item["pr_number"],
            author=author.get("login") if isinstance(author, dict) else str(author),
            state=item["state"],
            body=item.get("body"),
            submitted_at=item["submitted_at"],
        )


class HttpGitHubClient:
    """Real REST implementation of :class:`GitHubReadClient` (offline in tests).

    Live use is a C6-optional acceptance requiring explicit scope, an installed
    App, and credentials; nothing here writes. Each request retries at most
    :data:`MAX_HTTP_ATTEMPTS` times, honoring ``Retry-After`` on 403/429, and
    raises :class:`GitHubReadError` afterwards — the caller decides how a 403
    or 404 maps to availability.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        credentials: StaticTokenCredentialProvider | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
        retry_delay_seconds: float = 2.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._credentials is not None:
            headers["Authorization"] = f"Bearer {self._credentials.installation_token()}"
        return headers

    def _request(
        self, path: str, params: dict[str, Any] | None = None,
        **path_params: str | int,
    ) -> Any:
        values = {"owner": self._owner, "repo": self._repo, **path_params}
        encoded = {key: quote(str(value), safe="") for key, value in values.items()}
        url = f"{self._base_url}{path.format(**encoded)}"
        last_error: GitHubReadError | None = None
        for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
            try:
                request = self._client.request if self._client else httpx.request
                response = request(
                    "GET",
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=self._timeout_seconds,
                )
            except httpx.TransportError as exc:
                last_error = GitHubReadError("GET", None, str(exc))
                if attempt < MAX_HTTP_ATTEMPTS:
                    time.sleep(self._retry_delay_seconds)
                continue
            if response.status_code < 400:
                return response.json()
            retry_after = response.headers.get("retry-after")
            if (response.status_code >= 500 or response.status_code == 429 or (
                response.status_code == 403 and retry_after
            )) and attempt < MAX_HTTP_ATTEMPTS:
                try:
                    delay = float(retry_after) if retry_after else self._retry_delay_seconds
                except ValueError:
                    delay = self._retry_delay_seconds
                time.sleep(max(0.0, min(delay, 30.0)))
                continue
            raise GitHubReadError("GET", response.status_code, response.text[:500])
        raise last_error or GitHubReadError("GET", None, "unreachable")

    # -- reads ---------------------------------------------------------------------

    def get_repository(self) -> RepositoryInfo:
        data = self._request("/repos/{owner}/{repo}")
        return RepositoryInfo(
            repository_id=str(data["id"]),
            full_name=data["full_name"],
            default_branch=data["default_branch"],
        )

    def get_branch_head(self, branch: str) -> CommitInfo:
        data = self._request(
            "/repos/{owner}/{repo}/branches/{branch}", branch=branch
        )
        return CommitInfo(sha=data["commit"]["sha"])

    def get_tree(self, commit_sha: str) -> list[TreeEntry]:
        data = self._request(
            "/repos/{owner}/{repo}/git/trees/{sha}",
            {"recursive": "1"}, sha=commit_sha,
        )
        if data.get("truncated"):
            raise GitHubReadError("get_tree", 503, "tree enumeration is truncated")
        entries: list[TreeEntry] = []
        for item in data.get("tree", []):
            if item.get("type") != "blob":
                continue
            entries.append(
                TreeEntry(path=item["path"], blob_sha=item["sha"], size=item.get("size", 0))
            )
        return entries

    def get_blob(self, blob_sha: str) -> str:
        data = self._request(
            "/repos/{owner}/{repo}/git/blobs/{sha}", sha=blob_sha
        )

        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8")
        return str(data.get("content", ""))

    def list_issues(self, state: str, page: int, per_page: int) -> list[IssuePayload]:
        data = self._request(
            "/repos/{owner}/{repo}/issues",
            {"state": state, "page": page, "per_page": per_page},
        )
        # The issues API also returns PRs; is_pull_request lets the importer
        # route them out of issue scope by object type (§5.2).
        return [self._issue_from(item) for item in data]

    def get_issue(self, number: int) -> IssuePayload:
        data = self._request(
            "/repos/{owner}/{repo}/issues/{number}", number=number
        )
        if "pull_request" in data:
            raise GitHubReadError("get_issue", 409, f"#{number} is a pull request")
        return self._issue_from(data)

    def list_issue_comments(
        self, issue_number: int, page: int, per_page: int
    ) -> list[CommentPayload]:
        data = self._request(
            "/repos/{owner}/{repo}/issues/{number}/comments",
            {"page": page, "per_page": per_page}, number=issue_number,
        )
        return [
            CommentPayload(
                comment_id=str(item["id"]),
                issue_number=issue_number,
                author=item.get("user", {}).get("login"),
                body=item.get("body"),
                html_url=item.get("html_url"),
                created_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
                updated_at=datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")),
            )
            for item in data
        ]

    def get_pull_request(self, number: int) -> PullRequestPayload:
        data = self._request(
            "/repos/{owner}/{repo}/pulls/{number}", number=number
        )
        return PullRequestPayload(
            number=data["number"],
            pr_id=str(data["id"]),
            title=data["title"],
            body=data.get("body"),
            state=data["state"],
            milestone=(data.get("milestone") or {}).get("title"),
            merged=bool(data.get("merged", False)),
            merge_commit_sha=data.get("merge_commit_sha"),
            head_sha=data.get("head", {}).get("sha"),
            base_sha=data.get("base", {}).get("sha"),
            html_url=data.get("html_url"),
            updated_at=datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00")),
            closed_at=(
                datetime.fromisoformat(data["closed_at"].replace("Z", "+00:00"))
                if data.get("closed_at")
                else None
            ),
        )

    def list_pull_request_reviews(
        self, pr_number: int, page: int, per_page: int
    ) -> list[ReviewPayload]:
        data = self._request(
            "/repos/{owner}/{repo}/pulls/{number}/reviews",
            {"page": page, "per_page": per_page}, number=pr_number,
        )
        return [
            ReviewPayload(
                review_id=str(item["id"]),
                pr_number=pr_number,
                author=item.get("user", {}).get("login"),
                state=item["state"],
                body=item.get("body"),
                submitted_at=datetime.fromisoformat(item["submitted_at"].replace("Z", "+00:00")),
            )
            for item in data
        ]

    @staticmethod
    def _issue_from(item: dict[str, Any]) -> IssuePayload:
        return IssuePayload(
            number=item["number"],
            issue_id=str(item["id"]),
            title=item["title"],
            body=item.get("body"),
            state=item["state"],
            milestone=(item.get("milestone") or {}).get("title"),
            assignees=tuple(
                assignee.get("login", "") for assignee in item.get("assignees", [])
            ),
            labels=tuple(
                label["name"] if isinstance(label, dict) else str(label)
                for label in item.get("labels", [])
            ),
            is_pull_request="pull_request" in item,
            html_url=item.get("html_url"),
            updated_at=datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")),
            closed_at=(
                datetime.fromisoformat(item["closed_at"].replace("Z", "+00:00"))
                if item.get("closed_at")
                else None
            ),
        )
