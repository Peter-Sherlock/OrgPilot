# ADR-0005：飞书开放平台协作适配器与交互式卡片

- 状态：Accepted
- 日期：2026-08-26

## 背景

在 P0 至 P1 阶段，OrgPilot 建立了协调内核、Case 状态机、LLM 声明抽取与 SQL 持久化网关。为了将系统真正接入团队的真实工作流，需要对接企业级即时通讯平台（飞书 / Lark）：
1. 团队成员使用飞书桌面端与移动端日常沟通，无需切换到任何外部管理后台；
2. PM 审批需要以交互式富文本卡片（带按钮）的形式在飞书内原地完成；
3. 需要兼顾生产 Webhook 部署与本地开发/测试的零配置免公网 IP 需求。

## 决策

1. **协作适配器协议实现 (`FeishuCollaborationAdapter`)**：
   - 实现 `CollaborationAdapter` 统一协议；
   - 将 `SEND_PRIVATE_INQUIRY` 映射为向责任人私聊发送延期追问卡片；
   - 将 `REQUEST_APPROVAL` 映射为向 PM 发送带【🟢 批准】与【🔴 拒绝】回调按钮的交互式卡片；
   - 将 `UPDATE_TASK_DEADLINE` 映射为调用飞书 Task v2 OpenAPI 更新截止时间；
   - 将 `POST_GROUP_NOTIFICATION` 映射为向项目协作群发送富文本周知卡片。

2. **飞书 2.0 交互式卡片与原地审批机制**：
   - 按钮点击触发 `card.action.trigger` 事件回调，携带审批 ID 与操作 Token；
   - OrgPilot 校验 Token 合法性后原地更新飞书卡片样式（置灰按钮并展示审批人与时间），同时触发 Agent Loop 推进改期执行。

3. **双模客户端与安全鉴权 (`FeishuClient`)**：
   - 自动维护与刷新 `tenant_access_token`；
   - 提供 `AsyncFeishuClient`（调用真实飞书 OpenAPI）与 `MockFeishuClient`（离线录制与审计），保障 CI/CD 100% 确定性与零成本回归。

4. **统一事件路由与 Webhook 接入**：
   - 支持 URL 校验挑战（`url_verification`）；
   - 消息接收事件（`im.message.receive_v1`）自动转入 `ClaimExtractor` 抽取为领域事件；
   - 审批回调事件（`card.action.trigger`）自动驱动 `ApprovalManager` 决策并持久化。

## 后果

正面影响：
- 团队完全在飞书桌面端/手机端享受沉浸式 AI 协调，极大降低使用门槛；
- 审批秒级闭环，卡片原地刷新体验流畅；
- 核心内核与 IM 平台完全解耦。

限制与代价：
- 依赖飞书开放平台应用凭据配置（`App ID` / `App Secret`）；
- 需注意飞书 API 频率限制（Rate Limits），客户端需内置错误重试与超时控制。
