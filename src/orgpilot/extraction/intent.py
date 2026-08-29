"""Rule-first intent routing executed before claim extraction.

The router classifies each inbound message into a ``MessageIntent`` using
deterministic lexical rules with role awareness: the same sentence is a
command from a PM but merely a report or request from a peer engineer.
Confident non-report intents short-circuit the LLM extraction call entirely
(cost tiering); report-like or ambiguous messages fall through to the LLM,
which refines the intent inside the normal extraction call (no extra call).
Directive/task lifecycle execution itself lands in later milestones — this
layer only recognizes, annotates (targets, time expressions, authority), and
routes.
"""

import re

from orgpilot.domain.enums import MessageIntent
from orgpilot.extraction.models import IntentHint, IntentResult, MessageContext

PRIVILEGED_ROLES = frozenset({"pm", "lead"})

_TASK_CREATE_RE = re.compile(
    r"(新增|新建|创建|建个|立个|加个|排一?个|开一?个)[一个项]*任务|new\s+task", re.IGNORECASE
)

_REASSIGN_RE = re.compile(r"改派给?|移交给?|负责人?换成?|转给?|交给?")

_DIRECTIVE_VERBS = (
    "告诉",
    "转告",
    "通知",
    "吩咐",
    "安排",
    "指派",
    "命令",
    "要求",
    "下达",
    "催",
    "提醒",
    "让",
    "叫",
)

_MANDATE_RE = re.compile(
    r"必须|务必|一定要?|务必要?|最晚[于要]?|之前[^，。;；\n]{0,4}(完成|搞定|交付|提交|上线)"
)

_DEADLINE_CHANGE_RE = re.compile(
    r"(截止(时间|日期)?|deadline|交付时间|完成时间)[^，。;；\n]{0,6}"
    r"(改到|改为|改成|变更为?|调整到?|提前到?|推迟到?)"
    r"|把[^，。;；\n]{0,12}改到",
    re.IGNORECASE,
)

_CASUAL_RE = re.compile(
    r"你好|您好|早安|早上好|晚安|辛苦了|谢谢|太搞笑了|好累|吃饭|吃火锅|天气|周末|放假|加油|赞"
    r"|\bhello\b|\bhi\b|\bthanks?\b|\blol\b",
    re.IGNORECASE,
)

_WORK_MARKER_RE = re.compile(
    r"任务|进度|延期|延到|改期|卡|报错|完成|搞定|阻塞|受阻|风险|接口|sdk|支付|前端|后端"
    r"|测试|上线|修复|bug|联调|压测|验收|环境|部署|交付|排查|恢复|调通|开发|需求|文档"
    r"|deadline|block|issue|review|\bpr\b",
    re.IGNORECASE,
)

_QUESTION_RE = re.compile(
    r"[？？?]\s*$|什么时候|几号|几点|是否|能不能|可不可以|怎么办|如何|怎么样|哪个|谁来"
)

_REPORT_MARKER_RE = re.compile(
    r"报错|延期|卡住|受阻|排查|已解决|恢复|完成|搞定|调通|推进|正常|风险|不稳|挂了"
    r"|\bblock\b|\bissue\b|\bdelay\b|\bdone\b",
    re.IGNORECASE,
)

_TIME_EXPR_RE = re.compile(
    r"(今天|明天|后天|大后天|周[一二三四五六日天]|下周[一二三四五六日天]?|\d+\s*天[之]?后|\d+\s*[周月]后)"
    r"[^，。;；？！?!\n]{0,10}?"
    r"(上午|早上|中午|下午|晚上|凌晨)?"
    r"\s*\d{1,2}\s*[点时:：]\s*(半|\d{1,2})?"
)


class IntentRouter:
    """Deterministic role-aware intent classifier run before claim extraction."""

    def route(self, message: str, context: MessageContext) -> IntentResult:
        text = message.strip()
        lower = text.lower()

        members = self._detect_members(lower, context)
        tasks = self._detect_tasks(lower, context)
        time_match = _TIME_EXPR_RE.search(text)
        hints = IntentHint(
            mentioned_member_ids=tuple(mid for mid, _, _ in members),
            mentioned_task_ids=tuple(tasks),
            raw_time_expr=time_match.group(0).strip() if time_match else None,
        )
        actor_role = context.known_members.get(context.actor_id, "member")
        privileged = actor_role in PRIVILEGED_ROLES

        if _TASK_CREATE_RE.search(text):
            return IntentResult(
                intent=MessageIntent.TASK_CREATE,
                confidence=0.9 if privileged else 0.7,
                authority_ok=privileged,
                reasoning="识别到任务创建表述",
                hints=hints,
            )

        if _REASSIGN_RE.search(text) and members:
            return IntentResult(
                intent=MessageIntent.TASK_REASSIGN,
                confidence=0.9 if privileged else 0.7,
                authority_ok=privileged,
                reasoning=f"识别到任务改派表述，目标成员 {members[0][0]}",
                hints=hints,
            )

        directive_reason = self._match_directive(text, lower, members, context)
        if directive_reason:
            return IntentResult(
                intent=MessageIntent.DIRECTIVE,
                confidence=0.95 if privileged else 0.75,
                authority_ok=privileged,
                reasoning=directive_reason,
                hints=hints,
            )

        if _DEADLINE_CHANGE_RE.search(text) or (
            _MANDATE_RE.search(text) and tasks and not members
        ):
            mandate_note = "（含强制语气）" if _MANDATE_RE.search(text) else ""
            return IntentResult(
                intent=MessageIntent.DEADLINE_CHANGE,
                confidence=0.85 if privileged else 0.65,
                authority_ok=privileged,
                reasoning="识别到截止期变更表述" + mandate_note,
                hints=hints,
            )

        if _CASUAL_RE.search(lower) and not _WORK_MARKER_RE.search(lower):
            return IntentResult(
                intent=MessageIntent.CHIT_CHAT,
                confidence=0.9,
                reasoning="寒暄/情绪表达且无任何工作标记词",
                hints=hints,
            )

        if _QUESTION_RE.search(text):
            return IntentResult(
                intent=MessageIntent.QUESTION,
                confidence=0.75,
                reasoning="识别到疑问表述",
                hints=hints,
            )

        if _REPORT_MARKER_RE.search(lower):
            return IntentResult(
                intent=MessageIntent.HEALTH_REPORT,
                confidence=0.6,
                reasoning="健康汇报标记词，交由抽取管线解析槽位",
                hints=hints,
            )

        return IntentResult(
            intent=MessageIntent.UNCERTAIN,
            confidence=0.3,
            reasoning="规则无法判定，交由 LLM 抽取调用兜底分类",
            hints=hints,
        )

    @staticmethod
    def _detect_members(lower: str, context: MessageContext) -> list[tuple[str, str, int]]:
        """Finds known member mentions as (member_id, matched_token, position)."""
        found: list[tuple[str, str, int]] = []
        for member_id in context.known_members:
            candidates = {member_id.lower()}
            parts = member_id.lower().split("_")
            suffix = parts[-1]
            if len(suffix) >= 3 and suffix.isascii() and suffix.isalpha():
                candidates.add(suffix)
            for token in sorted(candidates, key=len, reverse=True):
                idx = lower.find(token)
                if idx >= 0:
                    found.append((member_id, token, idx))
                    break
        found.sort(key=lambda item: item[2])
        return found

    @staticmethod
    def _detect_tasks(lower: str, context: MessageContext) -> list[str]:
        matched: list[str] = []
        for task_id, title in context.known_tasks.items():
            token_id = task_id.lower()
            token_title = title.lower().replace(" ", "")
            if token_id in lower or (token_title and token_title in lower.replace(" ", "")):
                matched.append(task_id)
        return matched

    def _match_directive(
        self,
        text: str,
        lower: str,
        members: list[tuple[str, str, int]],
        context: MessageContext,
    ) -> str | None:
        """Directive requires another member as target; instructing oneself is a report."""
        non_self = [(mid, tok, idx) for mid, tok, idx in members if mid != context.actor_id]
        if not non_self:
            return None
        for mid, _, idx in non_self:
            prefix = lower[max(0, idx - 2) : idx]
            if any(prefix.endswith(verb) for verb in _DIRECTIVE_VERBS):
                return f"指令动词「{prefix[-2:]}」紧邻目标成员 {mid}"
        if _MANDATE_RE.search(text):
            return f"强制语气约束目标成员 {non_self[0][0]}"
        return None
