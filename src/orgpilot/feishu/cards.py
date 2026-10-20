"""Feishu 2.0 Interactive Card JSON generators for inquiry, approval, and notifications.

Every user-derived value placed into a ``lark_md`` field goes through
``escape_lark_md``; the card's own markup (``**``, backticks, ``\\n``) is written
around the escaped values. ``plain_text`` fields and button ``value`` payloads are
not Markdown and stay raw.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

# Feishu's 特殊字符转义说明 table: characters that hit Markdown syntax must be
# written as HTML entities to show literally.
# https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-components/content-components/rich-text
# The table is on the 富文本组件 (``markdown`` tag) page; the 普通文本组件 page
# for ``lark_md`` documents no escaping of its own. Only the characters that
# drive lark_md's documented syntax are escaped: links ``[..](..)``, tags
# ``<at>``/``<font>``/``<a>``, ``*``/``~`` emphasis, and backticks plus ``\``,
# which could break out of the card's own code spans. ``_``, ``-``, ``:`` and
# ``/`` stay as they are because member ids, task ids and ISO times use them.
# ``&`` is escaped first so a literal entity in the input shows as typed.
_LARK_MD_ESCAPES = str.maketrans(
    {
        "&": "&#38;",
        "<": "&#60;",
        ">": "&#62;",
        "*": "&#42;",
        "~": "&sim;",
        "[": "&#91;",
        "]": "&#93;",
        "`": "&#96;",
        "\\": "&#92;",
        # A newline would let a value start its own line and fake a card row.
        "\r": " ",
        "\n": " ",
    }
)


def escape_lark_md(value: Any) -> str:
    """Renders an untrusted value as literal text inside a ``lark_md`` field."""
    return str(value).translate(_LARK_MD_ESCAPES)


def _code_list(values: Iterable[Any], separator: str = ", ") -> str:
    return separator.join(f"`{escape_lark_md(v)}`" for v in values)


def format_deadline(value: Any, fallback: str = "未指定") -> str:
    """Renders an ISO deadline as "YYYY-MM-DD HH:MM" in the offset it was stated in."""
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def build_inquiry_card(task_id: str, title: str, reason: str | None = None) -> dict[str, Any]:
    """Generates an inquiry card asking a team member for estimated recovery time."""
    reason_md = escape_lark_md(reason) if reason else "检测到潜在阻塞或进度滞后风险"
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**任务**：`{escape_lark_md(task_id)}` {escape_lark_md(title)}\n"
                    f"**风险发现**：{reason_md}\n\n"
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
    impacted_desc = _code_list(impacted_tasks) if impacted_tasks else "无直接影响任务"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**受影响任务**\n`{escape_lark_md(task_id)}` "
                            f"{escape_lark_md(task_title)}"
                        ),
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**风险等级**\n`{escape_lark_md(risk_level)}`",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**建议新截止时间**\n`{escape_lark_md(proposed_deadline_str)}`",
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
    decided_by = f"审批人：{escape_lark_md(approver_name)}，时间：{escape_lark_md(decided_at_str)}"
    status_text = (
        f"✅ **已批准**（{decided_by}）" if is_approved else f"❌ **已拒绝**（{decided_by}）"
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
                        "content": (
                            f"**受影响任务**\n`{escape_lark_md(task_id)}` "
                            f"{escape_lark_md(task_title)}"
                        ),
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**目标调整时间**\n`{escape_lark_md(proposed_deadline_str)}`",
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
    impacted_desc = _code_list(impacted_tasks) if impacted_tasks else "无其他直接影响任务"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**排期变更通知**\n"
                    f"任务 `{escape_lark_md(task_id)}` （{escape_lark_md(task_title)}）"
                    f"已由 **{escape_lark_md(approver_name)}** 审批通过，"
                    f"截止时间调整至 `{escape_lark_md(new_deadline_str)}`。\n\n"
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
            src_title = escape_lark_md(r.get("source_task_title") or r.get("source_task_id"))
            owner = escape_lark_md(r.get("owner_name", "负责人"))
            health = escape_lark_md(r.get("health_status"))
            impacted = r.get("cascading_impact_tasks", [])
            impacted_str = _code_list(impacted) if impacted else "无直接下游"
            risk_md_lines.append(
                f"• **`{src_title}`** (@{owner})：处于 `{health}` 状态\n"
                f"  ↳ **波及下游关键路径**：{impacted_str}"
            )
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(risk_md_lines)},
            }
        )
        elements.append({"tag": "hr"})

    # Recommendations are plain sentences from SyncCoordinator that embed member
    # display names, so each one is escaped whole.
    recs = briefing.get("recommended_actions", [])
    if recs:
        recs_md = "**💡 智能体协同建议**：\n" + "\n".join(
            f"{idx + 1}. {escape_lark_md(act)}" for idx, act in enumerate(recs)
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


def build_sync_confirmation_card(
    initiated_by: str,
    member_ids: list[str] | tuple[str, ...],
    requested_at_str: str,
) -> dict[str, Any]:
    """Generates the sync confirmation card shown BEFORE any probe is sent.

    The card payload carries ``kind=sync_confirmation`` so downstream consumers
    (and the intent evaluation) can identify a pre-side-effect confirmation
    explicitly (LABELING.md R8). Buttons carry the initiator and request time so
    the callback can enforce initiator-only confirmation and card expiry.
    """
    listing = _code_list(member_ids, separator="、") or "（无待询问成员）"
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**发起人**：`{escape_lark_md(initiated_by)}`\n"
                    f"**将询问的成员**：{listing}\n\n"
                    "确认后将向以上成员逐一发送 1 对 1 进度探针，并在全员收敛后生成决策简报。"
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🟢 确认发起"},
                    "type": "primary",
                    "value": {
                        "action": "sync_confirm",
                        "initiated_by": initiated_by,
                        "requested_at": requested_at_str,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "⚪ 取消"},
                    "type": "default",
                    "value": {
                        "action": "sync_cancel",
                        "initiated_by": initiated_by,
                        "requested_at": requested_at_str,
                    },
                },
            ],
        },
    ]
    return {
        "kind": "sync_confirmation",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 全员进度同步确认"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_sync_closed_card(status_text: str, note: str) -> dict[str, Any]:
    """Generates the in-place updated card after confirm/cancel/expiry.

    ``note`` is shown as literal text: callers build it from operator ids and
    session summaries, so it is escaped whole rather than trusted as Markdown.
    """
    return {
        "kind": "sync_confirmation",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📋 全员进度同步 [已关闭：{status_text}]"},
            "template": "grey",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": escape_lark_md(note)},
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "该确认卡片已关闭，无需再点击按钮。"}
                ],
            },
        ],
    }
