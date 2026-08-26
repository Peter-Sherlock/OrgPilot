"""Tests for Feishu 2.0 interactive card builders."""

from orgpilot.feishu.cards import (
    build_approval_card,
    build_approval_updated_card,
    build_inquiry_card,
    build_notification_card,
)


def test_build_inquiry_card() -> None:
    card = build_inquiry_card(
        task_id="backend_api",
        title="支付接口联调",
        reason="遇到第三方 SDK 报错",
    )
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["title"]["content"] == "⚠️ 任务排期风险确认"
    assert "backend_api" in card["elements"][0]["text"]["content"]
    assert "支付接口联调" in card["elements"][0]["text"]["content"]


def test_build_approval_card() -> None:
    card = build_approval_card(
        approval_id="appr:123",
        case_id="case:456",
        task_id="backend_api",
        task_title="支付接口联调",
        proposed_deadline_str="2026-09-11 17:00:00",
        impacted_tasks=["frontend_ui", "qa_test"],
        risk_level="HIGH",
    )
    assert card["header"]["title"]["content"] == "⚠️ 任务改期调整审批请求"
    assert card["header"]["template"] == "red"

    # Verify action buttons
    action_elem = [e for e in card["elements"] if e.get("tag") == "action"][0]
    buttons = action_elem["actions"]
    assert len(buttons) == 2

    approve_btn = buttons[0]
    assert approve_btn["text"]["content"] == "🟢 批准改期"
    assert approve_btn["value"]["action"] == "approved"
    assert approve_btn["value"]["approval_id"] == "appr:123"

    reject_btn = buttons[1]
    assert reject_btn["text"]["content"] == "🔴 拒绝申请"
    assert reject_btn["value"]["action"] == "rejected"
    assert reject_btn["value"]["approval_id"] == "appr:123"


def test_build_approval_updated_card() -> None:
    card_approved = build_approval_updated_card(
        task_id="backend_api",
        task_title="支付接口联调",
        proposed_deadline_str="2026-09-11 17:00:00",
        decision="approved",
        approver_name="Carol (PM)",
        decided_at_str="2026-09-10 14:30:00",
    )
    assert "任务改期审批 [已完成]" in card_approved["header"]["title"]["content"]
    assert card_approved["header"]["template"] == "green"
    assert "已批准" in card_approved["elements"][2]["text"]["content"]

    card_rejected = build_approval_updated_card(
        task_id="backend_api",
        task_title="支付接口联调",
        proposed_deadline_str="2026-09-11 17:00:00",
        decision="rejected",
        approver_name="Carol (PM)",
        decided_at_str="2026-09-10 14:30:00",
    )
    assert "任务改期审批 [已拒绝]" in card_rejected["header"]["title"]["content"]
    assert card_rejected["header"]["template"] == "grey"
    assert "已拒绝" in card_rejected["elements"][2]["text"]["content"]


def test_build_notification_card() -> None:
    card = build_notification_card(
        task_id="backend_api",
        task_title="支付接口联调",
        new_deadline_str="2026-09-11 17:00:00",
        impacted_tasks=["frontend_ui"],
        approver_name="Carol (PM)",
    )
    assert card["header"]["title"]["content"] == "📢 团队任务排期对齐通知"
    assert "frontend_ui" in card["elements"][0]["text"]["content"]
