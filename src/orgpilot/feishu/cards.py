"""Feishu 2.0 Interactive Card JSON generators for inquiry, approval, and notifications."""

from typing import Any


def build_inquiry_card(task_id: str, title: str, reason: str | None = None) -> dict[str, Any]:
    """Generates an inquiry card asking a team member for estimated recovery time."""
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**任务**：`{task_id}` {title}\n"
                    f"**风险发现**：{reason or '检测到潜在阻塞或进度滞后风险'}\n\n"
                    f"为了评估对下游依赖的影响，请回复或更新预计可恢复/完成的时间（例如：*“明天下午5点前搞定”*）。"
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "OrgPilot 协调助手将自动提取您的回复并同步项目状态",
                }
            ],
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 任务排期风险确认"},
            "template": "orange",
        },
        "elements": elements,
    }


def build_approval_card(
    approval_id: str,
    case_id: str,
    task_id: str,
    task_title: str,
    proposed_deadline_str: str,
    impacted_tasks: list[str] | tuple[str, ...],
    risk_level: str = "HIGH",
) -> dict[str, Any]:
    """Generates an interactive approval card with [Approve] and [Reject] action buttons."""
    impacted_desc = (
        ", ".join(f"`{t}`" for t in impacted_tasks) if impacted_tasks else "无直接影响任务"
    )

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**受影响任务**\n`{task_id}` {task_title}",
                    },
                },
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**风险等级**\n`{risk_level}`"},
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**建议新截止时间**\n`{proposed_deadline_str}`",
                    },
                },
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**波及下游**\n{impacted_desc}"},
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🟢 批准改期"},
                    "type": "primary",
                    "value": {
                        "action": "approved",
                        "approval_id": approval_id,
                        "case_id": case_id,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔴 拒绝申请"},
                    "type": "danger",
                    "value": {
                        "action": "rejected",
                        "approval_id": approval_id,
                        "case_id": case_id,
                    },
                },
            ],
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 任务改期调整审批请求"},
            "template": "red" if risk_level == "HIGH" else "carmine",
        },
        "elements": elements,
    }


def build_task_action_card(
    approval_id: str,
    case_id: str,
    proposal_kind: str,
    task_title: str,
    owner_name: str,
    deadline_str: str | None = None,
    previous_owner_name: str | None = None,
    proposed_by: str = "",
) -> dict[str, Any]:
    """Generates the approval card for NL task creation / reassignment proposals."""
    if proposal_kind == "task_create":
        header = "🆕 任务创建提案审批"
        fields = [
            ("新任务", task_title),
            ("负责人", owner_name),
            ("截止", deadline_str or "未指定"),
            ("发起人", proposed_by or "-"),
        ]
    else:
        header = "🔄 任务改派提案审批"
        fields = [
            ("任务", task_title),
            ("原负责人", previous_owner_name or "-"),
            ("新负责人", owner_name),
            ("发起人", proposed_by or "-"),
        ]
    element_fields = [
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**{label}**\n`{value}`"},
        }
        for label, value in fields
    ]
    elements: list[dict[str, Any]] = [
        {"tag": "div", "fields": element_fields},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🟢 批准"},
                    "type": "primary",
                    "value": {
                        "action": "approved",
                        "approval_id": approval_id,
                        "case_id": case_id,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔴 拒绝"},
                    "type": "danger",
                    "value": {
                        "action": "rejected",
                        "approval_id": approval_id,
                        "case_id": case_id,
                    },
                },
            ],
        },
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header},
            "template": "blue",
        },
        "elements": elements,
    }


def build_approval_updated_card(
    task_id: str,
    task_title: str,
    proposed_deadline_str: str,
    decision: str,
    approver_name: str,
    decided_at_str: str,
) -> dict[str, Any]:
    """Generates an in-place updated card replacing action buttons with final decision status."""
    is_approved = decision.lower() == "approved"
    status_text = (
        f"✅ **已批准**（审批人：{approver_name}，时间：{decided_at_str}）"
        if is_approved
        else f"❌ **已拒绝**（审批人：{approver_name}，时间：{decided_at_str}）"
    )
    template_color = "green" if is_approved else "grey"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**受影响任务**\n`{task_id}` {task_title}",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**目标调整时间**\n`{proposed_deadline_str}`",
                    },
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": status_text},
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "任务改期审批 [已完成]" if is_approved else "任务改期审批 [已拒绝]",
            },
            "template": template_color,
        },
        "elements": elements,
    }


def build_notification_card(
    task_id: str,
    task_title: str,
    new_deadline_str: str,
    impacted_tasks: list[str] | tuple[str, ...],
    approver_name: str,
) -> dict[str, Any]:
    """Generates a broadcast notification card for group announcements."""
    impacted_desc = (
        ", ".join(f"`{t}`" for t in impacted_tasks) if impacted_tasks else "无其他直接影响任务"
    )

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**排期变更通知**\n"
                    f"任务 `{task_id}` （{task_title}）已由 **{approver_name}** 审批通过，"
                    f"截止时间调整至 `{new_deadline_str}`。\n\n"
                    f"**下游受波及任务**：{impacted_desc}\n"
                    f"请相关责任人及时知悉并同步排期。"
                ),
            },
        }
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📢 团队任务排期对齐通知"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_executive_briefing_card(
    briefing: dict[str, Any], project_id: str = "default"
) -> dict[str, Any]:
    """Generates an executive briefing card summarizing probe and DAG impact results."""
    delayed = briefing.get("delayed_count", 0)
    at_risk = briefing.get("at_risk_count", 0)
    on_track = briefing.get("on_track_count", 0)
    total = briefing.get("total_active_tasks", 0)

    template_color = "red" if delayed > 0 else ("orange" if at_risk > 0 else "green")
    status_emoji = (
        "🔴 发现关键阻塞"
        if delayed > 0
        else ("🟡 存在潜在风险" if at_risk > 0 else "🟢 全线健康推进")
    )

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**整体健康度**\n{status_emoji}"},
                },
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**活跃任务总数**\n`{total}` 项"},
                },
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**🟢 正常推进**\n`{on_track}` 项"},
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**🔴 延误 / 🟡 风险**\n`{delayed}` 延误 / `{at_risk}` 风险",
                    },
                },
            ],
        },
        {"tag": "hr"},
    ]

    # Topological risks section
    risks = briefing.get("topological_risks", [])
    if risks:
        risk_md_lines = ["**⚠️ 关键拓扑阻塞与连锁影响**："]
        for r in risks:
            src_title = r.get("source_task_title") or r.get("source_task_id")
            owner = r.get("owner_name", "负责人")
            impacted = r.get("cascading_impact_tasks", [])
            impacted_str = ", ".join(f"`{t}`" for t in impacted) if impacted else "无直接下游"
            risk_md_lines.append(
                f"• **`{src_title}`** (@{owner})：处于 `{r.get('health_status')}` 状态\n"
                f"  ↳ **波及下游关键路径**：{impacted_str}"
            )
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(risk_md_lines)},
            }
        )
        elements.append({"tag": "hr"})

    # Recommendations
    recs = briefing.get("recommended_actions", [])
    if recs:
        recs_md = "**💡 智能体协同建议**：\n" + "\n".join(
            f"{idx + 1}. {act}" for idx, act in enumerate(recs)
        )
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": recs_md},
            }
        )
        elements.append({"tag": "hr"})

    # Action / Link buttons
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📊 查看实时 DAG 拓扑看板"},
                    "type": "primary",
                    "url": "http://localhost:8000/",
                }
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📊 项目全景进度与拓扑风险简报"},
            "template": template_color,
        },
        "elements": elements,
    }
