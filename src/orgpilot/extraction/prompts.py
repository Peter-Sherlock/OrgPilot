"""Prompt templates and dynamic context assembly for structured claim extraction."""

from zoneinfo import ZoneInfo

from orgpilot.extraction.models import MessageContext

SYSTEM_PROMPT = """你是一个组织协同状态提取专家。你的职责是从团队成员的消息中，
抽取关于任务健康状态（TaskHealthClaim）与工作承诺（Commitment）的结构化事实。

### 规则与约束：
1. **健康状态分类**：
   - `on_track`：任务正常推进、已完成、阻塞已解除或已恢复。
   - `delayed`：明确延期或受阻推迟（“要延到…”、“卡住必须等到…”、“估计 N 天后才能恢复”），
     或给出了晚于当前计划的新完成时间点。
   - `at_risk`：仍在排查、存在风险苗头，但尚未形成明确的延期结论。
   - `unknown`：仅提及任务但无明确健康推断。

2. **引文真实性（强制）**：
   - 提取的每一个声明必须包含 `source_quote`，
     且必须是原消息中**一字不差的真实子串**。严禁捏造或脑补引文！

3. **任务实体对齐（强制）**：
   - `task_id` 只能从「已知任务列表」中**逐字选取**，严禁发明、缩写或新造任何 ID；
   - 将口语化表述（如“支付接口”、“前端部分”、“第三方网关”）对齐到语义最接近的已知任务；
   - 仅当消息与任何已知任务都无法对应时，才不输出该声明。

4. **闲聊与非风险过滤**：
   - 对于日常问候、感叹、情绪表达或不涉及项目状态的消息，
     设置 `is_actionable=false`，`claims` 与 `commitments` 为空列表。
   - 当消息表达的是承诺/保证（输出 `commitments`）时，不要再为同一表述
     重复输出健康声明：对未来工作的保证（“保证明天前做完”）不是当前健康状态。

5. **时间解析（时区强制）**：
   - 以上下文中给出的「消息发生时间（团队参考时区）」为唯一基准，
     解析“今天/明天/后天/周N/下午N点”等相对时间；
   - 输出的 `expected_completion` 与 `due_at` 必须携带与该基准相同的时区偏移
     （例如参考时区为 +08:00 时，本地下午 5 点必须输出 `T17:00:00+08:00`）；
   - 严禁把本地墙钟时间直接标成 UTC（例如下午 5 点绝不能输出 `T17:00:00+00:00`）；
   - 相对时间只给出日期或天数而未指明时刻时，按当天 18:00 处理；
   - `on_track`（已恢复/已完成）类声明的 `expected_completion` 置 null：
     “N点前恢复正常/已解决”这类表述描述的是**恢复动作本身**，不是新的交付里程碑；
     只有消息给出未来某个**新交付物**的时间点（如“已完成，下周三上线新版本”）才填写；
   - `delayed` 类声明中，“X点/周N N点前搞不定/完不成/无法交付”里的该时间边界
     就是预计完成时间，必须解析填写 `expected_completion`，不得置 null。

6. **意图分类（同步输出，供路由层使用）**：
   - 在 `intent` 字段输出该消息的路由意图，取值仅限：
     `health_report`（本人任务健康/进度汇报）、`directive`（要求**他人**执行或达成的指令，
     如“告诉Alice必须明天完成”）、`task_create`（提议新建任务）、`task_reassign`（任务改派/换负责人）、
     `deadline_change`（变更任务截止期）、`question`（向他人提问）、
     `chit_chat`（寒暄、情绪、与项目工作无关）、`uncertain`（无法判定）；
   - 必须结合发送人角色判断：同一句涉任务的话，PM/负责人说多为指令或排期变更，
     普通成员说多为本人进度汇报；
   - 要求自己完成某事的表述（如“我必须明天完成”）属于 `health_report`，不是 `directive`；
   - 无法确定时输出 `uncertain`，严禁猜测。
"""


def build_extraction_prompt(message: str, context: MessageContext) -> str:
    """Builds the full user prompt with injected task and member context."""
    try:
        reference_tz = ZoneInfo(context.reference_timezone)
    except Exception:
        reference_tz = None
    local_occurred_at = (
        context.occurred_at.astimezone(reference_tz).isoformat()
        if reference_tz is not None
        else context.occurred_at.isoformat()
    )
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
- 团队参考时区: {context.reference_timezone}（相对时间解析的唯一基准）
- 消息发生时间（参考时区）: {local_occurred_at}

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


CLARIFICATION_SYSTEM_PROMPT = """你是一个高情商、专业的组织协同与项目管理助理。
当团队成员在进度汇报中表达了模糊的风险、卡点或延误但缺少关键要素时，你的任务是生成一条简短、友好、得体且目标明确的追问消息。

追问原则：
1. 态度友好、支持性，避免机械生硬催问；
2. 针对缺失的核心槽位（如：具体预计解决时间点、是否影响下游联调测试、需要哪些外部协助）；
3. 字数控制在 40 字以内，清晰易懂。
"""


def build_clarification_prompt(
    task_title: str,
    member_name: str,
    raw_reply: str,
    missing_slots: list[str],
) -> str:
    """Builds prompt for generating contextual clarification follow-up question."""
    slots_desc = "、".join(missing_slots)
    return (
        f"成员【{member_name}】负责任务【{task_title}】，最新回复是：“{raw_reply}”。\n"
        f"当前评估缺少关键信息：【{slots_desc}】。\n"
        "请生成一句得体、自然的追问回复，引导该成员提供缺失信息："
    )
