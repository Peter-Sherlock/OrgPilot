"""Tests for Feishu OpenAPI client and Mock implementation."""

import json
from datetime import datetime

import httpx
import pytest

from orgpilot.feishu.client import AsyncFeishuClient, MockFeishuClient

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


async def test_mock_feishu_client_operations() -> None:
    client = MockFeishuClient()

    # Send text message
    res_msg = await client.send_message(
        receive_id="ou_user_1",
        msg_type="text",
        content={"text": "hello"},
    )
    assert res_msg["code"] == 0
    assert len(client.sent_messages) == 1

    # Send card
    res_card = await client.send_card(
        receive_id="ou_user_1",
        card={"header": {"title": "test"}},
    )
    assert res_card["code"] == 0
    assert len(client.sent_cards) == 1

    # Update task
    res_task = await client.update_task_deadline(
        task_guid="task_123",
        deadline=NOW,
    )
    assert res_task["code"] == 0
    assert len(client.updated_tasks) == 1


async def test_async_feishu_client_token_and_calls() -> None:
    # Use httpx mock transport to test AsyncFeishuClient
    def handler(request: httpx.Request) -> httpx.Response:
        if "/open-apis/auth/v3/tenant_access_token/internal" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "t-mock-token-12345",
                    "expire": 7200,
                },
            )
        if "/open-apis/im/v1/messages" in str(request.url):
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"message_id": "om_real_123"}},
            )
        if "/open-apis/task/v2/tasks" in str(request.url):
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"task_guid": "task_real_123"}},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://open.feishu.cn")

    client = AsyncFeishuClient(
        app_id="cli_test",
        app_secret="sec_test",
        client=http_client,
    )

    # 1. Get token
    token = await client.get_tenant_access_token()
    assert token == "t-mock-token-12345"

    # 2. Re-get token from cache (should not make another HTTP call)
    token2 = await client.get_tenant_access_token()
    assert token2 == token

    # 3. Send message
    resp_msg = await client.send_message(
        receive_id="ou_test",
        msg_type="text",
        content="hello real",
    )
    assert resp_msg["code"] == 0
    assert resp_msg["data"]["message_id"] == "om_real_123"

    # 4. Update task
    resp_task = await client.update_task_deadline(
        task_guid="task_real_123",
        deadline=NOW,
    )
    assert resp_task["code"] == 0


async def test_async_feishu_client_token_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 10001, "msg": "invalid app_secret"},
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://open.feishu.cn")
    client = AsyncFeishuClient(app_id="cli_bad", app_secret="bad", client=http_client)

    with pytest.raises(RuntimeError, match="tenant token request failed"):
        await client.get_tenant_access_token()

async def test_async_feishu_client_temp_client_and_chat_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an injected client each request uses a temporary client; chat ids
    routed as open_id fall back to receive_id_type=chat_id, and cards delegate."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "t-mock-token-12345",
                    "expire": 7200,
                },
            )
        if "/open-apis/im/v1/messages" in str(request.url):
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"message_id": "om_real_123"}},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    client = AsyncFeishuClient(app_id="cli_test", app_secret="sec_test")

    token = await client.get_tenant_access_token()
    assert token == "t-mock-token-12345"

    resp_msg = await client.send_message(receive_id="oc_group_chat", msg_type="text", content="hi")
    assert resp_msg["code"] == 0

    resp_card = await client.send_card(receive_id="ou_user", card={"header": {"title": "t"}})
    assert resp_card["code"] == 0


async def test_async_feishu_client_batch_get_open_ids_chunks_and_skips_unmatched() -> None:
    """ADR-0009 §3: 50 emails per request; entries without user_id are unresolved."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/open-apis/auth/v3/tenant_access_token/internal" in str(request.url):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-1"})
        assert request.url.path == "/open-apis/contact/v3/users/batch_get_id"
        assert request.url.params["user_id_type"] == "open_id"
        assert request.headers["Authorization"] == "Bearer t-1"
        body = json.loads(request.content)
        requests.append(body)
        user_list = []
        for email in body["emails"]:
            if email.startswith("missing"):
                user_list.append({"email": email})  # matched shape unknown: no user_id
            elif not email.startswith("absent"):
                user_list.append({"email": email, "user_id": f"ou_{email.split('@')[0]}"})
        return httpx.Response(200, json={"code": 0, "data": {"user_list": user_list}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    ) as http:
        client = AsyncFeishuClient(app_id="cli", app_secret="secret", client=http)
        emails = [f"user{i}@example.com" for i in range(51)]
        emails += ["User0@Example.com", "missing@example.com", "absent@example.com"]

        resolved = await client.batch_get_open_ids(emails)

    assert [len(body["emails"]) for body in requests] == [50, 3]
    assert all(body["include_resigned"] is False for body in requests)
    assert len(resolved) == 51
    assert resolved["user0@example.com"] == "ou_user0"
    assert "missing@example.com" not in resolved
    assert "absent@example.com" not in resolved


async def test_async_feishu_client_batch_get_open_ids_raises_on_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-1"})
        return httpx.Response(200, json={"code": 99991672, "msg": "Access denied"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    ) as http:
        client = AsyncFeishuClient(app_id="cli", app_secret="secret", client=http)
        with pytest.raises(RuntimeError, match="batch get user id failed: Access denied"):
            await client.batch_get_open_ids(["a@example.com"])


async def test_mock_feishu_client_directory_lookup() -> None:
    client = MockFeishuClient(email_directory={"Alice@Example.com": "ou_alice"})
    emails = ["alice@example.com", "ALICE@example.com"] + [f"x{i}@example.com" for i in range(50)]

    resolved = await client.batch_get_open_ids(emails)

    assert resolved == {"alice@example.com": "ou_alice"}
    assert [len(chunk) for chunk in client.directory_requests] == [50, 1]
