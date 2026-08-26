"""Prompt templates and dynamic context assembly for structured claim extraction."""

from orgpilot.extraction.models import MessageContext

SYSTEM_PROMPT = """你是一个组织协同状态提取专家。你的职责是从团队成员的消息中，
抽取关于任务健康状态（TaskHealthClaim）与工作承诺（Commitment）的结构化事实。

### 规则与约束：
1. **健康状态分类**：
   - `on_track`：任务正常推进、已完成、阻塞已解除或已恢复。
   - `at_risk`：存在潜在风险、遇到困难、排查中或不确定能否按时完成。
   - `delayed`：明确延期、无法在原截止日前完成、遇到严重阻碍。
   - `unknown`：仅提及任务但无明确健康推断。

2. **引文真实性（强制）**：
   - 提取的每一个声明必须包含 `source_quote`，
     且必须是原消息中**一字不差的真实子串**。严禁捏造或脑补引文！

3. **任务实体消歧**：
   - 将用户口语化的任务表述（如“支付接口”、“前端部分”）对齐到上下文给定的标准 `task_id`。
     如果无法对应任何已知任务，不要强行匹配。

4. **闲聊与非风险过滤**：
   - 对于日常问候、感叹、情绪表达或不涉及项目状态的消息，
     设置 `is_actionable=false`，`claims` 与 `commitments` 为空列表。

5. **时间解析**：
   - 依据给定的消息发生基准时间（occurred_at），
     将相对时间解析为带时区的 ISO 8601 字符串格式。
"""


def build_extraction_prompt(message: str, context: MessageContext) -> str:
    """Builds the full user prompt with injected task and member context."""
    tasks_desc = (
        "\n".join(f"- {task_id}: {desc}" for task_id, desc in context.known_tasks.items())
        if context.known_tasks
        else "无已知任务列表（禁止创建任务声明，需先同步任务或请求澄清）"
    )

    members_desc = (
        "\n".join(f"- {member_id}: {role}" for member_id, role in context.known_members.items())
        if context.known_members
        else "无已知成员列表"
    )

    history_desc = (
        "\n".join(f"> {item}" for item in context.conversation_history)
        if context.conversation_history
        else "无前序对话"
    )

    prompt = f"""### 当前项目上下文：
- 项目 ID: {context.project_id}
- 发送人 ID: {context.actor_id}
- 消息发生时间: {context.occurred_at.isoformat()}

### 已知任务列表：
{tasks_desc}

### 已知成员角色：
{members_desc}

### 前序对话上下文：
{history_desc}

---
### 待分析的用户消息：
\"\"\"{message}\"\"\"

请严格按照 JSON Schema 格式输出提取结果。"""
    return prompt
