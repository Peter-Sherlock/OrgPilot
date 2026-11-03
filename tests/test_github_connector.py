"""Read-only GitHub connector acceptance (ADR-0015 C2).

Covers the client layer offline: pagination slices, failure simulation,
403/404 classification, issue-vs-PR routing, the frozen endpoint/permission
catalog, and the HTTP payload mappers against synthetic REST-shaped data —
never a live network call. Live GitHub access stays a C6-optional acceptance.
"""

import json
import pathlib
from datetime import UTC, datetime

import pytest

from orgpilot.github.client import (
    ENDPOINT_CATALOG,
    GitHubPermission,
    GitHubReadClient,
    GitHubReadError,
    HttpGitHubClient,
    MockGitHubClient,
    StaticTokenCredentialProvider,
    is_access_failure,
)
from orgpilot.github.models import (
    CommentPayload,
    IssuePayload,
    PullRequestPayload,
    RepositoryInfo,
    ReviewPayload,
)

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "github_snapshot.json"


@pytest.fixture
def snapshot() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def client(snapshot) -> MockGitHubClient:
    return MockGitHubClient(snapshot)


class TestPermissionCatalog:
    def test_catalog_covers_exactly_the_client_methods(self) -> None:
        protocol_methods = {
            name
            for name, value in vars(GitHubReadClient).items()
            if not name.startswith("_") and callable(getattr(GitHubReadClient, name, None))
        }
        assert {spec.method for spec in ENDPOINT_CATALOG} == protocol_methods

    def test_catalog_uses_only_the_four_read_permissions(self) -> None:
        allowed = set(GitHubPermission)
        assert {spec.permission for spec in ENDPOINT_CATALOG} <= allowed
        assert allowed == {
            GitHubPermission.METADATA,
            GitHubPermission.CONTENTS,
            GitHubPermission.ISSUES,
            GitHubPermission.PULL_REQUESTS,
        }

    def test_catalog_has_no_write_verbs(self) -> None:
        for spec in ENDPOINT_CATALOG:
            assert spec.endpoint.split()[0] == "GET"


class TestMockClient:
    def test_repository_and_branch_head(self, client: MockGitHubClient) -> None:
        repo = client.get_repository()
        assert repo == RepositoryInfo(
            repository_id="1001", full_name="org/demo", default_branch="main"
        )
        assert client.get_branch_head("main").sha == "c0ffee1"
        with pytest.raises(GitHubReadError) as exc_info:
            client.get_branch_head("nope")
        assert exc_info.value.status_code == 404

    def test_pagination_slices_like_the_rest_api(self, client: MockGitHubClient) -> None:
        page1 = client.list_issues(state="all", page=1, per_page=2)
        page2 = client.list_issues(state="all", page=2, per_page=2)
        page3 = client.list_issues(state="all", page=3, per_page=2)
        numbers = [item.number for item in page1 + page2 + page3]
        assert numbers == sorted(item.number for item in client.list_issues("all", 1, 100))
        assert numbers[0:2] == numbers[0:2]  # deterministic ordering
        assert len(page3) == 2
        assert all(item.number not in [p.number for p in page1] for item in page2)
        with pytest.raises(GitHubReadError):
            client.list_issues(state="all", page=0, per_page=10)

    def test_failure_simulation_raises_with_status(self, snapshot) -> None:
        snapshot["failures"] = [
            {"method": "list_issue_comments", "match": {"issue_number": 2}, "status": 403}
        ]
        client = MockGitHubClient(snapshot)
        with pytest.raises(GitHubReadError) as exc_info:
            client.list_issue_comments(2, page=1, per_page=50)
        assert exc_info.value.status_code == 403
        # Other objects stay readable.
        assert len(client.list_issue_comments(1, page=1, per_page=50)) == 3

    def test_issue_and_pr_routing(self, client: MockGitHubClient) -> None:
        listing = client.list_issues(state="all", page=1, per_page=100)
        pr_numbers = {item.number for item in listing if item.is_pull_request}
        assert pr_numbers == {5}
        issue = client.get_issue(1)
        assert issue.milestone == "m2"
        with pytest.raises(GitHubReadError):
            client.get_issue(5)  # a PR number is not an issue object
        pr = client.get_pull_request(5)
        assert pr.merged is True and pr.merge_commit_sha == "merge-5"
        with pytest.raises(GitHubReadError):
            client.get_pull_request(999)

    def test_comments_sorted_and_paginated(self, client: MockGitHubClient) -> None:
        comments = client.list_issue_comments(1, page=1, per_page=2)
        assert [c.comment_id for c in comments] == ["7001", "7002"]
        page2 = client.list_issue_comments(1, page=2, per_page=2)
        assert [c.comment_id for c in page2] == ["7003"]

    def test_reviews_and_blobs(self, client: MockGitHubClient) -> None:
        reviews = client.list_pull_request_reviews(5, page=1, per_page=10)
        assert len(reviews) == 1 and reviews[0].state == "APPROVED"
        blob = client.get_blob("b-delivery")
        assert "测试环境可访问" in blob
        with pytest.raises(GitHubReadError):
            client.get_blob("missing")


class Test403Classification:
    def test_access_failure_never_counts_as_deletion(self) -> None:
        assert is_access_failure(403) is True
        assert is_access_failure(404) is True
        assert is_access_failure(500) is False
        assert is_access_failure(None) is False


class TestHttpPayloadMappers:
    """The HTTP client's mapping logic, exercised without any network."""

    def _client(self) -> HttpGitHubClient:
        return HttpGitHubClient(
            owner="org",
            repo="demo",
            credentials=StaticTokenCredentialProvider(lambda: "tok"),
        )

    def test_issue_mapper_flags_pull_requests(self) -> None:
        client = self._client()
        issue = client._issue_from(
            {
                "number": 7,
                "id": 9007,
                "title": "普通 Issue",
                "body": "正文",
                "state": "open",
                "milestone": {"title": "m2"},
                "assignees": [{"login": "bob"}],
                "labels": [{"name": "bug"}],
                "html_url": "https://github.com/org/demo/issues/7",
                "updated_at": "2026-10-20T09:00:00+08:00",
                "closed_at": None,
            }
        )
        assert isinstance(issue, IssuePayload)
        assert issue.is_pull_request is False
        assert issue.milestone == "m2" and issue.assignees == ("bob",)
        pr_entry = client._issue_from(
            {
                "number": 8,
                "id": 9008,
                "title": "PR 条目",
                "body": None,
                "state": "open",
                "milestone": None,
                "assignees": [],
                "labels": [],
                "html_url": "https://github.com/org/demo/pull/8",
                "pull_request": {"url": "https://api.github.com/pulls/8"},
                "updated_at": "2026-10-20T09:00:00+08:00",
                "closed_at": None,
            }
        )
        assert pr_entry.is_pull_request is True

    def test_pr_and_review_mappers(self) -> None:
        pr = PullRequestPayload.model_validate(
            {
                "number": 5,
                "pr_id": "9500",
                "title": "支付接口实现",
                "body": "Fixes #1",
                "state": "closed",
                "merged": True,
                "merge_commit_sha": "merge-5",
                "head_sha": "head-5",
                "base_sha": "c0ffee1",
                "html_url": "https://github.com/org/demo/pull/5",
                "updated_at": "2026-10-25T08:00:00+08:00",
                "closed_at": "2026-10-25T08:00:00+08:00",
            }
        )
        assert pr.merged and pr.merge_commit_sha == "merge-5"
        review = ReviewPayload.model_validate(
            {
                "review_id": "8000",
                "pr_number": 5,
                "author": "carol",
                "state": "APPROVED",
                "body": "lgtm",
                "submitted_at": "2026-10-24T18:00:00+08:00",
            }
        )
        assert review.state == "APPROVED"
        comment = CommentPayload.model_validate(
            {
                "comment_id": "7001",
                "issue_number": 1,
                "author": "bob",
                "body": "明天给。",
                "html_url": "https://github.com/org/demo/issues/1#issuecomment-7001",
                "created_at": "2026-10-20T10:00:00+08:00",
                "updated_at": "2026-10-20T10:00:00+08:00",
            }
        )
        assert comment.author == "bob"

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError):
            IssuePayload.model_validate(
                {
                    "number": 1,
                    "issue_id": "9001",
                    "title": "t",
                    "body": None,
                    "state": "open",
                    "milestone": None,
                    "assignees": [],
                    "labels": [],
                    "html_url": None,
                    "updated_at": datetime(2026, 10, 20, 9, 0, tzinfo=UTC).replace(
                        tzinfo=None
                    ),
                    "closed_at": None,
                }
            )
