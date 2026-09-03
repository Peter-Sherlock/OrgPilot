"""Feishu OpenAPI client with automatic token management and mock implementation."""

import json
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from orgpilot.adapter.contracts import PermanentDeliveryError


@runtime_checkable
class FeishuClient(Protocol):
    """Protocol defining Feishu OpenAPI operations."""

    async def send_message(
        self,
        receive_id: str,
        msg_type: str,
        content: str | dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        """Sends an IM message to a user or chat group."""
        ...

    async def send_card(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        """Sends an interactive card to a user or chat group."""
        ...

    async def update_task_deadline(
        self,
        task_guid: str,
        deadline: datetime,
    ) -> dict[str, Any]:
        """Updates task deadline via Feishu Task OpenAPI."""
        ...


class AsyncFeishuClient:
    """Production asynchronous Feishu API client with automatic token caching and refresh."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._tenant_token: str | None = None
        self._token_expires_at: float = 0.0

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, url, **kwargs)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            return await client.request(method, url, **kwargs)

    @staticmethod
    def _response_json(response: httpx.Response, operation: str) -> dict[str, Any]:
        response.raise_for_status()
        data = response.json()
        if data.get("code", 0) != 0:
            # A Feishu app-level rejection (invalid open_id, bad request, missing
            # scope) is permanent: surface it as non-retryable to the outbox.
            raise PermanentDeliveryError(
                f"Feishu {operation} failed: {data.get('msg', 'unknown error')}"
            )
        return data

    async def get_tenant_access_token(self) -> str:
        """Retrieves and caches valid tenant_access_token."""
        now = datetime.now(UTC).timestamp()
        if self._tenant_token and now < self._token_expires_at - 300:
            return self._tenant_token

        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}

        resp = await self._request("POST", url, json=payload)
        data = self._response_json(resp, "tenant token request")

        self._tenant_token = data["tenant_access_token"]
        # expire in seconds
        expire = data.get("expire", 7200)
        self._token_expires_at = now + expire
        return self._tenant_token

    async def send_message(
        self,
        receive_id: str,
        msg_type: str,
        content: str | dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        if receive_id_type == "open_id" and receive_id.startswith("oc_"):
            receive_id_type = "chat_id"
        token = await self.get_tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {"Authorization": f"Bearer {token}"}

        content_str = json.dumps(content) if isinstance(content, dict) else content
        body = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": content_str,
        }

        resp = await self._request("POST", url, json=body, headers=headers)
        return self._response_json(resp, "send message")

    async def send_card(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        """Sends an interactive card message."""
        return await self.send_message(
            receive_id=receive_id,
            msg_type="interactive",
            content=card,
            receive_id_type=receive_id_type,
        )

    async def update_task_deadline(
        self,
        task_guid: str,
        deadline: datetime,
    ) -> dict[str, Any]:
        """Updates task deadline via Task OpenAPI."""
        token = await self.get_tenant_access_token()
        url = f"{self.base_url}/open-apis/task/v2/tasks/{task_guid}"
        headers = {"Authorization": f"Bearer {token}"}
        body = {
            "due": {
                "timestamp": str(int(deadline.timestamp() * 1000)),
                "is_all_day": False,
            }
        }

        resp = await self._request("PATCH", url, json=body, headers=headers)
        return self._response_json(resp, "update task")


class MockFeishuClient:
    """Deterministic in-memory Mock Feishu client for offline testing and CI/CD."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_cards: list[dict[str, Any]] = []
        self.updated_tasks: list[dict[str, Any]] = []

    async def send_message(
        self,
        receive_id: str,
        msg_type: str,
        content: str | dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        record = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": content,
            "receive_id_type": receive_id_type,
            "message_id": f"om_mock_{len(self.sent_messages) + 1}",
        }
        self.sent_messages.append(record)
        return {"code": 0, "msg": "success", "data": record}

    async def send_card(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        record = {
            "receive_id": receive_id,
            "card": card,
            "receive_id_type": receive_id_type,
            "message_id": f"om_mock_card_{len(self.sent_cards) + 1}",
        }
        self.sent_cards.append(record)
        return {"code": 0, "msg": "success", "data": record}

    async def update_task_deadline(
        self,
        task_guid: str,
        deadline: datetime,
    ) -> dict[str, Any]:
        record = {
            "task_guid": task_guid,
            "deadline": deadline.isoformat(),
        }
        self.updated_tasks.append(record)
        return {"code": 0, "msg": "success", "data": record}
