"""Contract tests for the AIHubMix Anthropic Messages-compatible client."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from orgpilot.extraction.client import AnthropicCompatibleLLMClient, LLMUnavailableError
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
        assert body["thinking"] == {"type": "disabled"}
        assert "output_config" not in body
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
        reasoning_effort="none",
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


def test_aihubmix_client_omits_reasoning_when_not_configured() -> None:
    payload = ExtractionResult(is_actionable=False, reasoning="No action").model_dump_json()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "thinking" not in body
        assert "output_config" not in body
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": payload}]},
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


def test_aihubmix_client_enables_anthropic_thinking_effort_when_configured() -> None:
    payload = ExtractionResult(is_actionable=False, reasoning="No action").model_dump_json()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "enabled"}
        assert body["output_config"] == {"effort": "low"}
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": payload}]},
        )

    client = AnthropicCompatibleLLMClient(
        api_key="test-key",
        model="deepseek-v4-flash",
        reasoning_effort="low",
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


def test_aihubmix_client_reports_safe_metadata_when_text_block_is_missing() -> None:
    client = AnthropicCompatibleLLMClient(
        api_key="test-key",
        model="deepseek-v4-flash",
        max_retries=0,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "content": [{"type": "thinking", "thinking": "private"}],
                        "stop_reason": "max_tokens",
                    },
                )
            )
        ),
    )

    with pytest.raises(
        LLMUnavailableError,
        match=r"block_types=\['thinking'\], stop_reason='max_tokens'",
    ):
        client.extract(
            "system",
            "user",
            "hello",
            MessageContext(
                project_id="project-1",
                actor_id="alice",
                occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
            ),
        )
