"""Transport-level contracts: real adapters over offline HTTP transports."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from orgpilot.github.client import (
    GitHubReadError,
    HttpGitHubClient,
    StaticTokenCredentialProvider,
)
from tests.test_project_review_regressions import material_fixture  # noqa: F401

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures/github_snapshot.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "method,args,path,query",
    [
        ("get_repository", (), "/repos/org/demo", {}),
        ("get_branch_head", ("feature/a",), "/repos/org/demo/branches/feature%2Fa", {}),
        ("get_tree", ("c0ffee1",), "/repos/org/demo/git/trees/c0ffee1", {"recursive": "1"}),
        ("get_blob", ("b-readme",), "/repos/org/demo/git/blobs/b-readme", {}),
        (
            "list_issues",
            ("all", 2, 50),
            "/repos/org/demo/issues",
            {"state": "all", "page": "2", "per_page": "50"},
        ),
        ("get_issue", (1,), "/repos/org/demo/issues/1", {}),
        (
            "list_issue_comments",
            (1, 2, 50),
            "/repos/org/demo/issues/1/comments",
            {"page": "2", "per_page": "50"},
        ),
        ("get_pull_request", (5,), "/repos/org/demo/pulls/5", {}),
        (
            "list_pull_request_reviews",
            (5, 2, 50),
            "/repos/org/demo/pulls/5/reviews",
            {"page": "2", "per_page": "50"},
        ),
    ],
)
def test_r4_every_real_endpoint_constructs_read_only_request(method, args, path, query):
    captured = []

    def handler(request):
        captured.append(request)
        assert request.method == "GET"
        assert request.url.raw_path.split(b"?")[0].decode() == path
        assert dict(request.url.params) == query
        assert request.headers["authorization"] == "Bearer dummy-offline"
        if method == "get_repository":
            data = {"id": 1001, "full_name": "org/demo", "default_branch": "main"}
        elif method == "get_branch_head":
            data = {"commit": {"sha": "c0ffee1"}}
        elif method == "get_tree":
            data = {"tree": [{"type": "blob", "path": "README.md", "sha": "b-readme"}]}
        elif method == "get_blob":
            data = {"encoding": "base64", "content": base64.b64encode("目标".encode()).decode()}
        elif method in ("get_issue", "list_issues"):
            data = {
                **SNAPSHOT["issues"][0],
                "id": 9001,
                "assignees": [{"login": "bob"}],
                "milestone": {"title": "m2"},
            }
            data = [data] if method == "list_issues" else data
        elif method == "get_pull_request":
            data = {**SNAPSHOT["pull_requests"][0], "id": 9500, "milestone": {"title": "m2"}}
        elif method == "list_issue_comments":
            data = SNAPSHOT["issue_comments"][:1]
        else:
            data = SNAPSHOT["pull_request_reviews"][:1]
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = HttpGitHubClient(
            "org", "demo", StaticTokenCredentialProvider(lambda: "dummy-offline"), client=transport
        )
        assert getattr(client, method)(*args) is not None
    assert len(captured) == 1


def test_r4_truncated_tree_is_never_complete_enumeration():
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={"tree": [], "truncated": True},
                )
            )
        ) as transport,
        pytest.raises(GitHubReadError, match="truncated"),
    ):
        HttpGitHubClient("org", "demo", client=transport).get_tree("sha")


def test_r4_transport_and_server_failures_have_three_attempt_limit(monkeypatch):
    calls = []
    monkeypatch.setattr("orgpilot.github.client.time.sleep", lambda seconds: None)

    def fail(request):
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    with (
        httpx.Client(transport=httpx.MockTransport(fail)) as transport,
        pytest.raises(GitHubReadError),
    ):
        HttpGitHubClient("org", "demo", client=transport).get_repository()
    assert len(calls) == 3


async def test_r5_r6_service_uses_real_async_adapter_and_only_serialized_model_context(
    env, monkeypatch
):
    from orgpilot.config import OrgPilotSettings
    from orgpilot.knowledge.llm import ProjectLLMClient
    from orgpilot.knowledge.service import ProjectKnowledgeService

    calls = []

    async def handler(request):
        payload = json.loads(request.content)
        view = json.loads(payload["messages"][0]["content"])
        calls.append(view)
        if len(calls) == 1:
            decision = {"kind": "lookup", "tool": "search_project", "args": {"query": "支付接口"}}
        elif len(calls) == 2:
            issue = next(
                item for item in view["tool_results"][-1]["items"] if item["kind"] == "issue"
            )
            decision = {
                "kind": "lookup",
                "tool": "get_related_items",
                "args": {"source_id": issue["source_id"]},
            }
        elif len(calls) == 3:
            relation = next(
                item
                for item in view["tool_results"][-1]["items"]
                if item["relation_type"] == "implements"
            )
            decision = {
                "kind": "lookup",
                "tool": "read_source",
                "args": {"source_id": relation["from_ref"]},
            }
        else:
            ref = next(ref for ref in reversed(view["evidence"]) if ref["kind"] == "pull_request")
            decision = {
                "kind": "finish",
                "answer_status": "complete",
                "statements": [
                    {
                        "text": "PR 工作记录",
                        "source_id": ref["source_id"],
                        "revision_id": ref["revision_id"],
                        "locator": ref["paragraph"],
                        "snippet": ref["snippet"],
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(decision, ensure_ascii=False)}],
                "usage": {"input_tokens": 400, "output_tokens": 128},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        monkeypatch.setattr(
            "orgpilot.knowledge.llm.ProjectLLMClient",
            lambda **kwargs: ProjectLLMClient(**kwargs, client=transport),
        )
        service = ProjectKnowledgeService(
            type("Gateway", (), {"db": env["db"]})(),
            OrgPilotSettings(
                project_qa_enabled=True,
                project_qa_online=True,
                llm_provider="deepseek",
                deepseek_api_key="dummy-offline",
            ),
            clock=env["clock"],
        )
        answer = await service.answer("proj-a", "alice", "r5-wire", "支付接口联调记录")
        assert answer.stop_reason.value == "answered"
        assert answer.answer_status.value == "complete"
        assert len(calls) == 4
        assert answer.statements[0].evidence_refs[0].revision_id.startswith("rev-")
        await service.aclose()


async def test_r5_transport_usage_and_no_hidden_retries():
    from orgpilot.knowledge.llm import ProjectLLMClient

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="offline failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = ProjectLLMClient("dummy-offline", "https://model.invalid", client=transport)
        with pytest.raises(httpx.HTTPStatusError):
            await client.generate(model="test", system="rules", messages=[], max_tokens=20)
    assert len(calls) == 1
