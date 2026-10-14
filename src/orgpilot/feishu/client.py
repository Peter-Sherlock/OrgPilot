"""Feishu OpenAPI client with automatic token management and mock implementation."""

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

# contact/v3 users/batch_get_id accepts at most 50 emails and 50 mobiles per request.
BATCH_GET_ID_LIMIT = 50


def normalize_mobile(value: str) -> str:
    """Drops separators and the +86 prefix: batch_get_id takes mainland numbers bare
    and other numbers with a leading +country code."""
    cleaned = re.sub(r"[\s\-()]", "", value)
    return cleaned[3:] if cleaned.startswith("+86") else cleaned


def _unique_emails(emails: Sequence[str]) -> list[str]:
    """Strips and de-duplicates emails case-insensitively, keeping first spellings."""
    seen: dict[str, str] = {}
    for email in emails:
        cleaned = email.strip()
        seen.setdefault(cleaned.lower(), cleaned)
    return list(seen.values())


def _lookup_batches(emails: Sequence[str], mobiles: Sequence[str]) -> list[dict[str, list[str]]]:
    """Splits lookups into request bodies of up to 50 emails plus 50 mobiles each."""
    unique_emails = _unique_emails(emails)
    unique_mobiles = list(dict.fromkeys(normalize_mobile(m) for m in mobiles))
    batches: list[dict[str, list[str]]] = []
    for start in range(0, max(len(unique_emails), len(unique_mobiles)), BATCH_GET_ID_LIMIT):
        batch: dict[str, list[str]] = {}
        if chunk := unique_emails[start : start + BATCH_GET_ID_LIMIT]:
            batch["emails"] = chunk
        if chunk := unique_mobiles[start : start + BATCH_GET_ID_LIMIT]:
            batch["mobiles"] = chunk
        batches.append(batch)
    return batches


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


@runtime_checkable
class FeishuDirectory(Protocol):
    """Resolves team members' emails or mobiles to app-scoped open_ids (ADR-0009 §3)."""

    async def batch_get_open_ids(
        self, emails: Sequence[str] = (), mobiles: Sequence[str] = ()
    ) -> dict[str, str]:
        """Returns lower-cased email or normalized mobile -> open_id for every match."""
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
            raise RuntimeError(f"Feishu {operation} failed: {data.get('msg', 'unknown error')}")
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

    async def batch_get_open_ids(
        self, emails: Sequence[str] = (), mobiles: Sequence[str] = ()
    ) -> dict[str, str]:
        """Resolves emails and mobiles to open_ids via contact/v3 users/batch_get_id.

        An unmatched lookup comes back as an entry without user_id (seen on a real
        tenant, 2026-10-14): unknown, outside the app's contact scope, resigned, or
        an address that is not the account's email (a "work email" set in the
        admin console does not match). Missing entries count as unresolved too.
        """
        token = await self.get_tenant_access_token()
        url = f"{self.base_url}/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id"
        headers = {"Authorization": f"Bearer {token}"}
        resolved: dict[str, str] = {}
        for batch in _lookup_batches(emails, mobiles):
            body = {**batch, "include_resigned": False}
            resp = await self._request("POST", url, json=body, headers=headers)
            data = self._response_json(resp, "batch get user id")
            for entry in (data.get("data") or {}).get("user_list") or []:
                user_id = entry.get("user_id")
                if not user_id:
                    continue
                if entry.get("email"):
                    resolved[entry["email"].strip().lower()] = user_id
                if entry.get("mobile"):
                    resolved[normalize_mobile(entry["mobile"])] = user_id
        return resolved


class MockFeishuClient:
    """Deterministic in-memory Mock Feishu client for offline testing and CI/CD."""

    def __init__(
        self,
        email_directory: dict[str, str] | None = None,
        mobile_directory: dict[str, str] | None = None,
    ) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_cards: list[dict[str, Any]] = []
        self.updated_tasks: list[dict[str, Any]] = []
        self.email_directory = {
            email.strip().lower(): open_id for email, open_id in (email_directory or {}).items()
        }
        self.mobile_directory = {
            normalize_mobile(mobile): open_id
            for mobile, open_id in (mobile_directory or {}).items()
        }
        self.directory_requests: list[dict[str, list[str]]] = []

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

    async def batch_get_open_ids(
        self, emails: Sequence[str] = (), mobiles: Sequence[str] = ()
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for batch in _lookup_batches(emails, mobiles):
            self.directory_requests.append(batch)
            for email in batch.get("emails", []):
                if open_id := self.email_directory.get(email.lower()):
                    resolved[email.lower()] = open_id
            for mobile in batch.get("mobiles", []):
                if open_id := self.mobile_directory.get(mobile):
                    resolved[mobile] = open_id
        return resolved
