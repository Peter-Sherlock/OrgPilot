"""Async project-Q&A generation transport; extraction keeps its existing client."""

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class GenerationResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProjectLLMClient:
    """Anthropic Messages-compatible, one HTTP attempt per controller attempt."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        thinking: str = "omit",
        client: httpx.AsyncClient | None = None,
    ):
        self._key = api_key
        self._base_url = base_url.rstrip("/")
        self._thinking = thinking
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            timeout=10.0,
        )

    def payload(self, *, model, system, messages, max_tokens) -> dict[str, Any]:
        payload = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._thinking != "omit":
            payload["thinking"] = {"type": self._thinking}
        return payload

    async def generate(self, *, model, system, messages, max_tokens) -> GenerationResult:
        response = await self._client.post(
            f"{self._base_url}/v1/messages",
            headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            json=self.payload(model=model, system=system, messages=messages, max_tokens=max_tokens),
        )
        response.raise_for_status()
        data = response.json()
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        if not text:
            raise ValueError("project model response has no text")
        usage = data.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if type(input_tokens) is int and input_tokens >= 0:
            cache_tokens = [
                usage.get(key, 0)
                for key in (
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ]
            if all(type(value) is int and value >= 0 for value in cache_tokens):
                input_tokens += sum(cache_tokens)
            else:
                input_tokens = None
        else:
            input_tokens = None
        if type(output_tokens) is not int or output_tokens < 0:
            output_tokens = None
        return GenerationResult(text, input_tokens, output_tokens)

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()
