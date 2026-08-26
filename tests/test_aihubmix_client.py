"""Contract tests for the AIHubMix Anthropic Messages-compatible client."""

import json
from datetime import UTC, datetime

import httpx

from orgpilot.extraction.client import AnthropicCompatibleLLMClient
from orgpilot.extraction.models import ExtractionResult, MessageContext


def test_aihubmix_client_selects_text_block_and_validates_json() -> None:
    expected = ExtractionResult(
        is_actionable=False,
        claims=[],
        commitments=[],
        reasoning="No project state in message",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url == "https://aihubmix.com/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert body["model"] == "gpt-5.6-luna"
        assert body["stream"] is False
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": expected.model_dump_json()},
                ]
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = AnthropicCompatibleLLMClient(
        api_key="test-key",
        model="gpt-5.6-luna",
        client=http_client,
    )
    context = MessageContext(
        project_id="project-1",
        actor_id="alice",
        occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
    )

    result = client.extract("system", "user", "hello", context)

    assert result == expected


def test_aihubmix_client_accepts_fenced_json() -> None:
    payload = ExtractionResult(
        is_actionable=False,
        reasoning="No action",
    ).model_dump_json()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": f"```json\n{payload}\n```"}]},
        )

    client = AnthropicCompatibleLLMClient(
        api_key="test-key",
        model="gpt-5.6-luna",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.extract(
        "system",
        "user",
        "hello",
        MessageContext(
            project_id="project-1",
            actor_id="alice",
            occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
        ),
    )

    assert result.reasoning == "No action"
