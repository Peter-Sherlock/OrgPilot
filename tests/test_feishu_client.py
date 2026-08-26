"""Tests for Feishu OpenAPI client and Mock implementation."""

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
