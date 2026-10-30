"""Tests for Feishu 2.0 interactive card builders."""

import json
from typing import Any

import pytest

from orgpilot.feishu.cards import (
    build_approval_card,
    build_approval_updated_card,
    build_executive_briefing_card,
    build_inquiry_card,
    build_notification_card,
    build_sync_closed_card,
    build_sync_confirmation_card,
    escape_lark_md,
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

def test_build_executive_briefing_card_with_risks_and_recommendations() -> None:
    card = build_executive_briefing_card(
        briefing={
            "delayed_count": 1,
            "at_risk_count": 1,
            "on_track_count": 2,
            "total_active_tasks": 4,
            "topological_risks": [
                {
                    "source_task_id": "task-payment",
                    "source_task_title": "支付SDK接入",
                    "owner_name": "Alice",
                    "health_status": "delayed",
                    "cascading_impact_tasks": ["task-checkout"],
                }
            ],
            "recommended_actions": ["优先通过改期卡片完成排期审批。"],
        },
        project_id="feishu-project",
    )
    assert card["header"]["title"]["content"] == "📊 项目全景进度与拓扑风险简报"
    flattened = json.dumps(card, ensure_ascii=False)
    assert "发现关键阻塞" in flattened
    assert "支付SDK接入" in flattened
    assert "task-checkout" in flattened
    assert "智能体协同建议" in flattened
    assert "查看实时 DAG 拓扑看板" in flattened


def test_build_executive_briefing_card_without_risks_is_green() -> None:
    card = build_executive_briefing_card(
        briefing={
            "delayed_count": 0,
            "at_risk_count": 0,
            "on_track_count": 3,
            "total_active_tasks": 3,
            "topological_risks": [],
            "recommended_actions": [],
        },
    )
    flattened = json.dumps(card, ensure_ascii=False)
    assert "全线健康推进" in flattened
    assert "关键拓扑阻塞" not in flattened


# ---------------------------------------------------------------- lark_md escaping

HOSTILE_TITLE = "支付联调[批准](http://evil.example)**紧急**<at id=all></at>"
HOSTILE_NAME = "<font color='red'>CEO</font>~~x~~`y`\\"
RAW_MARKUP = ("[批准](", "<at id=all>", "<font", "**紧急**", "~~x~~", "`y`")


def _lark_md_contents(node: Any) -> list[str]:
    """Collects the content of every lark_md text object in a card."""
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("tag") == "lark_md":
            found.append(node["content"])
        for value in node.values():
            found.extend(_lark_md_contents(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_lark_md_contents(item))
    return found


def _assert_no_raw_markup(card: dict[str, Any]) -> str:
    joined = "\n".join(_lark_md_contents(card))
    for raw in RAW_MARKUP:
        assert raw not in joined, raw
    return joined


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("[批准](http://evil)", "&#91;批准&#93;(http://evil)"),
        ("<at id=all></at>", "&#60;at id=all&#62;&#60;/at&#62;"),
        ("<font color='red'>x</font>", "&#60;font color='red'&#62;x&#60;/font&#62;"),
        ("**加粗**", "&#42;&#42;加粗&#42;&#42;"),
        ("~~删除~~", "&sim;&sim;删除&sim;&sim;"),
        ("`code`", "&#96;code&#96;"),
        ("尾部\\", "尾部&#92;"),
        ("&#60;", "&#38;#60;"),
        ("第一行\n**伪造行**\r\n", "第一行 &#42;&#42;伪造行&#42;&#42;  "),
    ],
)
def test_escape_lark_md_neutralizes_markup(raw: str, escaped: str) -> None:
    assert escape_lark_md(raw) == escaped


@pytest.mark.parametrize(
    "text",
    [
        "支付接口联调",
        "张三（产品经理）",
        "前端页面重构：首页、详情页",
        "backend_api",
        "task-payment",
        "ou_7d8a6e6ed2c6e1b2",
        "2026-09-11T17:00:00+08:00",
        "已回复 1/2 人",
        "v2.0 发布 #3!",
    ],
)
def test_escape_lark_md_leaves_normal_text_unchanged(text: str) -> None:
    assert escape_lark_md(text) == text


def test_escape_lark_md_stringifies_non_strings() -> None:
    assert escape_lark_md(3) == "3"
    assert escape_lark_md(None) == "None"


def test_inquiry_card_escapes_title_and_reason() -> None:
    card = build_inquiry_card(task_id="backend_api", title=HOSTILE_TITLE, reason=HOSTILE_NAME)
    content = _assert_no_raw_markup(card)
    assert "&#91;批准&#93;(http://evil.example)" in content
    assert "&#60;at id=all&#62;&#60;/at&#62;" in content
    assert "&#60;font color='red'&#62;CEO" in content
    # The card's own markup is untouched.
    assert content.startswith("**任务**：`backend_api` 支付联调")


def test_inquiry_card_keeps_normal_chinese_and_default_reason() -> None:
    card = build_inquiry_card(task_id="backend_api", title="支付接口联调")
    content = card["elements"][0]["text"]["content"]
    assert content.startswith(
        "**任务**：`backend_api` 支付接口联调\n**风险发现**：检测到潜在阻塞或进度滞后风险\n\n"
    )


def test_approval_card_escapes_title_and_impacted_ids_but_not_button_values() -> None:
    card = build_approval_card(
        approval_id="appr:1",
        case_id="case:1",
        task_id="backend`_api",
        task_title=HOSTILE_TITLE,
        proposed_deadline_str="2026-09-11 17:00",
        impacted_tasks=["frontend_ui", "qa*test"],
    )
    content = _assert_no_raw_markup(card)
    assert "`backend&#96;_api` 支付联调&#91;批准&#93;" in content
    assert "`frontend_ui`, `qa&#42;test`" in content
    assert "`2026-09-11 17:00`" in content
    buttons = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
    assert buttons[0]["value"] == {
        "action": "approved",
        "approval_id": "appr:1",
        "case_id": "case:1",
    }


def test_approval_card_keeps_normal_chinese_fields() -> None:
    card = build_approval_card(
        approval_id="appr:1",
        case_id="case:1",
        task_id="backend_api",
        task_title="支付接口联调",
        proposed_deadline_str="2026-09-11 17:00",
        impacted_tasks=["frontend_ui", "qa_test"],
    )
    fields = [f["text"]["content"] for f in card["elements"][0]["fields"]]
    assert fields == [
        "**受影响任务**\n`backend_api` 支付接口联调",
        "**风险等级**\n`HIGH`",
        "**建议新截止时间**\n`2026-09-11 17:00`",
        "**波及下游**\n`frontend_ui`, `qa_test`",
    ]


def test_approval_updated_card_escapes_approver_name() -> None:
    card = build_approval_updated_card(
        task_id="backend_api",
        task_title=HOSTILE_TITLE,
        proposed_deadline_str="2026-09-11 17:00",
        decision="approved",
        approver_name=HOSTILE_NAME,
        decided_at_str="2026-09-10 14:30",
    )
    _assert_no_raw_markup(card)
    assert card["elements"][2]["text"]["content"] == (
        "✅ **已批准**（审批人：&#60;font color='red'&#62;CEO&#60;/font&#62;"
        "&sim;&sim;x&sim;&sim;&#96;y&#96;&#92;，时间：2026-09-10 14:30）"
    )


def test_approval_updated_card_keeps_normal_chinese_name() -> None:
    card = build_approval_updated_card(
        task_id="backend_api",
        task_title="支付接口联调",
        proposed_deadline_str="2026-09-11 17:00",
        decision="rejected",
        approver_name="王五",
        decided_at_str="2026-09-10 14:30",
    )
    assert card["elements"][2]["text"]["content"] == (
        "❌ **已拒绝**（审批人：王五，时间：2026-09-10 14:30）"
    )


def test_notification_card_escapes_title_and_approver() -> None:
    card = build_notification_card(
        task_id="backend_api",
        task_title=HOSTILE_TITLE,
        new_deadline_str="2026-09-11T17:00:00+08:00",
        impacted_tasks=["frontend_ui"],
        approver_name=HOSTILE_NAME,
    )
    content = _assert_no_raw_markup(card)
    assert "已由 **&#60;font color='red'&#62;CEO" in content
    assert "截止时间调整至 `2026-09-11T17:00:00+08:00`" in content


def test_notification_card_keeps_normal_chinese_text() -> None:
    card = build_notification_card(
        task_id="backend_api",
        task_title="支付接口联调",
        new_deadline_str="2026-09-11T17:00:00+08:00",
        impacted_tasks=[],
        approver_name="王五",
    )
    content = card["elements"][0]["text"]["content"]
    assert "任务 `backend_api` （支付接口联调）已由 **王五** 审批通过，" in content
    assert "**下游受波及任务**：无其他直接影响任务" in content


def test_executive_briefing_card_escapes_titles_owners_and_recommendations() -> None:
    card = build_executive_briefing_card(
        briefing={
            "delayed_count": 1,
            "topological_risks": [
                {
                    "source_task_id": "task-payment",
                    "source_task_title": HOSTILE_TITLE,
                    "owner_name": HOSTILE_NAME,
                    "health_status": "delayed",
                    "cascading_impact_tasks": ["task-checkout"],
                }
            ],
            "recommended_actions": [
                f"{HOSTILE_NAME} 超时未回复，本简报基于已收集到的信息生成，后续可单独跟进。",
                "全链路任务推进正常，无下游阻碍风险，保持当前交付节奏。",
            ],
        },
    )
    content = _assert_no_raw_markup(card)
    assert "• **`支付联调&#91;批准&#93;" in content
    assert "(@&#60;font color='red'&#62;CEO" in content
    assert "处于 `delayed` 状态" in content
    assert "2. 全链路任务推进正常，无下游阻碍风险，保持当前交付节奏。" in content


def test_sync_confirmation_card_escapes_ids_but_not_button_values() -> None:
    card = build_sync_confirmation_card(
        initiated_by="ou_pm*1",
        member_ids=["ou_alice", "<at id=all></at>"],
        requested_at_str="2026-09-10T09:00:00+08:00",
    )
    content = _assert_no_raw_markup(card)
    assert "**发起人**：`ou_pm&#42;1`" in content
    assert "`ou_alice`、`&#60;at id=all&#62;&#60;/at&#62;`" in content
    buttons = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
    assert buttons[0]["value"]["initiated_by"] == "ou_pm*1"


def test_sync_closed_card_escapes_note_and_keeps_plain_text_header() -> None:
    card = build_sync_closed_card("已取消", "⚪ <at id=all></at> 已取消本次全员同步。")
    assert card["elements"][0]["text"]["content"] == (
        "⚪ &#60;at id=all&#62;&#60;/at&#62; 已取消本次全员同步。"
    )
    assert card["header"]["title"]["content"] == "📋 全员进度同步 [已关闭：已取消]"
    note = "⌛ 该确认卡片已超过 30 分钟未处理，自动过期。"
    assert build_sync_closed_card("已过期", note)["elements"][0]["text"]["content"] == note
