"""Runtime reply categories and their merge priority (ADR-0008 §4).

When one message yields several intent items, each handler contributes a reply
category; the final reply uses the highest-priority one (ADR-0008 implementation
log): no_permission > sync_started > sync_confirm > clarify > claim_recorded >
query_answer > noted > unsupported > help > smalltalk.
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import Field

from orgpilot.extraction.models import StrictModel

FALLBACK_TEXT = "没理解，可以换个说法吗？也可以发送 /帮助 查看用法"

HELP_TEXT = (
    "🤖 OrgPilot 使用指南\n\n"
    "我可以帮你：\n"
    "1. 记录任务进展：直接说「支付SDK报错，明天下午5点能修好」；\n"
    "2. 查询任务状态：问「支付SDK现在什么情况？」；\n"
    "3. 发起全员进度同步：PM 发送「大家进展怎么样了」，确认后自动逐个收集；\n"
    "4. 回答追问：直接回复时间或阻塞原因，我会合并进未完成的记录。\n\n"
    "可用命令：\n"
    "• /同步 —— 立即发起全员进度同步（仅 PM）\n"
    "• /帮助 —— 查看本帮助"
)

WELCOME_TEXT = (
    "👋 你好！我是 OrgPilot 组织风险与排期协调智能体。\n\n"
    "🎯 已为您自动初始化体验项目。\n"
    "💬 直接汇报进展（如「支付SDK报错，明天下午5点能修好」），或发送 /帮助 查看完整用法。"
)


class ReplyKind(StrEnum):
    """What the bot did with one message, at reply granularity."""

    CLAIM_RECORDED = "claim_recorded"
    QUERY_ANSWER = "query_answer"
    SYNC_CONFIRM = "sync_confirm"
    SYNC_STARTED = "sync_started"
    NO_PERMISSION = "no_permission"
    UNSUPPORTED = "unsupported"
    NOTED = "noted"
    SMALLTALK = "smalltalk"
    HELP = "help"
    CLARIFY = "clarify"
    FALLBACK = "fallback"


# Highest priority first; merge_reply_kinds picks the first present entry.
REPLY_PRIORITY: tuple[ReplyKind, ...] = (
    ReplyKind.NO_PERMISSION,
    ReplyKind.SYNC_STARTED,
    ReplyKind.SYNC_CONFIRM,
    ReplyKind.CLARIFY,
    ReplyKind.CLAIM_RECORDED,
    ReplyKind.QUERY_ANSWER,
    ReplyKind.NOTED,
    ReplyKind.UNSUPPORTED,
    ReplyKind.HELP,
    ReplyKind.SMALLTALK,
)


def merge_reply_kinds(kinds: Iterable[ReplyKind]) -> ReplyKind | None:
    """Merges per-item reply categories by ADR priority; None when nothing replied."""
    present = [kind for kind in kinds if kind != ReplyKind.FALLBACK]
    if not present:
        return None
    return min(present, key=REPLY_PRIORITY.index)


class RouterOutcome(StrictModel):
    """Result of routing one inbound message through the layered pipeline."""

    reply_category: ReplyKind = ReplyKind.SMALLTALK
    reply_text: str = ""
    asks_clarification: bool = False
    msg: str = "success"
    data: dict[str, Any] = Field(default_factory=dict)
    suppress_reply_send: bool = False
    duplicate: bool = False
