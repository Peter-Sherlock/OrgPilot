"""The live Feishu demo script (docs/feishu-demo-runbook.md), end to end on a mock client.

Runs the exact steps of the runbook through the same wiring the gateway and the
standalone WebSocket listener use (``build_gateway_service``): roster import, a
member reporting a delay in a DM, the reschedule approval card reaching the PM,
the PM approving it, and a re-import that must keep the approved deadline. Only
the Feishu OpenAPI client is replaced, so a failure here means the live demo
would fail at the same step.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.domain.enums import ApprovalStatus, CoordinationCaseStatus
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.app import build_gateway_service
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database
from orgpilot.team import import_team, parse_team_spec

SHANGHAI = ZoneInfo("Asia/Shanghai")
T0 = datetime(2026, 10, 21, 10, 0, tzinfo=SHANGHAI)
PROJECT = "feishu-project"
PM = "ou_demo_pm"
ENGINEER = "ou_demo_engineer"

# Mirrors the team.yaml in the runbook: the engineer owns the delayed task, the PM
# owns the two downstream tasks so the approval card has impacts to show.
TEAM_YAML = """
project: feishu-project
members:
  wang: {mobile: "13800000001", name: 王鹏飞, role: pm}
  peer: {mobile: "13800000002", name: 同学A, role: engineer}
tasks:
  - id: task-payment
    title: 支付SDK接入
    owner: peer
    status: doing
    deadline: 2026-10-21 18:00
  - id: task-checkout
    title: 收银台前端结账
    owner: wang
    deadline: 2026-10-23 18:00
    depends_on: [task-payment]
  - id: task-qa
    title: 支付全链路验收
    owner: wang
    deadline: 2026-10-24 18:00
    depends_on: [task-checkout]
"""
DELAY_MESSAGE = "支付 SDK 报错，排查需要到明天下午 5 点"
APPROVED_DEADLINE = datetime(2026, 10, 22, 17, 0, tzinfo=SHANGHAI)


def _message(sender: str, text: str, message_id: str) -> dict:
    millis = str(int(T0.timestamp() * 1000))
    return {
        "header": {
            "event_id": f"evt_{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": millis,
        },
        "event": {
            "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
            "message": {
                "message_id": message_id,
                "message_type": "text",
                "chat_type": "p2p",
                "content": json.dumps({"text": text}),
                "create_time": millis,
            },
        },
    }


def _card_click(operator: str, decision: str, approval_id: str) -> dict:
    return {
        "open_message_id": "om_demo_card",
        "operator": {"open_id": operator},
        "action": {"value": {"action": decision, "approval_id": approval_id}},
    }


@pytest.fixture
async def demo() -> tuple[GatewayService, MockFeishuClient, FeishuWebhookHandler]:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    settings = OrgPilotSettings(
        collaboration_adapter="feishu",
        feishu_app_id="cli_demo",
        feishu_app_secret="demo-secret",
        feishu_project_id=PROJECT,
    )
    client = MockFeishuClient(mobile_directory={"13800000001": PM, "13800000002": ENGINEER})
    service, _ = build_gateway_service(settings, db, FrozenClock(T0), feishu_client=client)
    handler = FeishuWebhookHandler(service=service, project_id=PROJECT)
    yield service, client, handler
    await db.close()


async def _import(service: GatewayService, client: MockFeishuClient) -> object:
    spec = parse_team_spec(TEAM_YAML, SHANGHAI)
    return await import_team(service, spec, client, digest="demo", now=T0)


async def test_demo_delay_report_reaches_pm_and_approval_updates_the_deadline(
    demo: tuple[GatewayService, MockFeishuClient, FeishuWebhookHandler],
) -> None:
    service, client, handler = demo
    result = await _import(service, client)
    assert result.applied

    # Step 1: the engineer reports the delay in a DM.
    reply = await handler.handle_event(_message(ENGINEER, DELAY_MESSAGE, "om_demo_1"))
    assert reply["code"] == 0
    assert reply["data"]["turn_termination_reason"] == "waiting_approval"
    assert any(m["receive_id"] == ENGINEER for m in client.sent_messages)

    # Step 2: the PM, and only the PM, receives the reschedule approval card.
    agent = await service.get_or_replay_agent(PROJECT)
    [approval] = agent.approval_manager.get_pending_requests()
    assert approval.approver_id == PM
    assert [card["receive_id"] for card in client.sent_cards] == [PM]
    card_text = json.dumps(client.sent_cards[0], ensure_ascii=False)
    assert "支付SDK接入" in card_text
    assert "2026-10-22 17:00" in card_text
    # Both downstream tasks: the impact propagates through the dependency chain.
    assert "收银台前端结账" in card_text
    assert "支付全链路验收" in card_text

    # A click from anyone but the PM is refused.
    spoofed = await handler.handle_event(_card_click(ENGINEER, "approved", approval.approval_id))
    assert spoofed["code"] == 403

    # Step 3: the PM approves; the case resolves and the card updates in place.
    decided = await handler.handle_event(_card_click(PM, "approved", approval.approval_id))
    assert decided["code"] == 0
    assert decided["turn_termination_reason"] == "all_resolved"
    assert "已完成" in decided["card"]["header"]["title"]["content"]
    decided_text = json.dumps(decided["card"], ensure_ascii=False)
    assert "2026-10-22 17:00" in decided_text
    assert "审批人：王鹏飞" in decided_text  # the PM's name, not a raw open_id
    assert "2026-10-21 10:00" in decided_text  # decision time in the business timezone

    agent = await service.get_or_replay_agent(PROJECT)
    assert agent.approval_manager.get_request(approval.approval_id).status is (
        ApprovalStatus.APPROVED
    )
    [case] = agent.case_ledger.get_all_cases()
    assert case.status is CoordinationCaseStatus.RESOLVED
    assert agent.projector.state.tasks["task-payment"].deadline == APPROVED_DEADLINE
    # Imported tasks are not Feishu tasks: write-back is off, so Feishu Task is untouched.
    assert client.updated_tasks == []


async def test_demo_reimport_keeps_the_approved_deadline(
    demo: tuple[GatewayService, MockFeishuClient, FeishuWebhookHandler],
) -> None:
    service, client, handler = demo
    await _import(service, client)
    await handler.handle_event(_message(ENGINEER, DELAY_MESSAGE, "om_demo_2"))
    agent = await service.get_or_replay_agent(PROJECT)
    [approval] = agent.approval_manager.get_pending_requests()
    await handler.handle_event(_card_click(PM, "approved", approval.approval_id))

    # Step 4: re-importing the unchanged team.yaml must not roll the approval back.
    again = await _import(service, client)

    assert not again.applied
    agent = await service.get_or_replay_agent(PROJECT)
    assert agent.projector.state.tasks["task-payment"].deadline == APPROVED_DEADLINE
