"""Contract tests for the AIHubMix Anthropic Messages-compatible client."""

import json
from datetime import UTC, datetime

import httpx
import pytest

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


def test_client_omits_thinking_field_by_default() -> None:
    seen: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"is_actionable": false}'}]},
        )

    client = AnthropicCompatibleLLMClient(
        api_key="test-key",
        model="gpt-5.6-luna",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.extract("s", "u", "m", _context())
    assert "thinking" not in seen["body"]


def test_client_injects_anthropic_thinking_switch() -> None:
    seen: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "internal scratchpad"},
                    {"type": "text", "text": '{"is_actionable": false}'},
                ]
            },
        )

    for mode, expected in (("disabled", {"type": "disabled"}), ("enabled", {"type": "enabled"})):
        client = AnthropicCompatibleLLMClient(
            api_key="ds-key",
            model="deepseek-flash",
            base_url="https://api.deepseek.com/anthropic",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            thinking=mode,
        )
        result = client.extract("s", "u", "m", _context())
        assert seen["body"]["thinking"] == expected
        assert seen["body"]["model"] == "deepseek-flash"
        # the thinking block is ignored; only the text block is parsed
        assert result.is_actionable is False

    with pytest.raises(ValueError, match="thinking"):
        AnthropicCompatibleLLMClient(api_key="k", model="m", thinking="balanced")


def _context() -> MessageContext:
    return MessageContext(
        project_id="project-1",
        actor_id="alice",
        occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
