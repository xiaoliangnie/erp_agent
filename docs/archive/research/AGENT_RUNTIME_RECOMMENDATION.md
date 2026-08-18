# Agent Runtime 建议

> 日期：2026-08-17  
> 依据：Grok Build 调研、DeepSeek Harness 调研、当前 `backend/agent/`。

本仓库已经是可用的薄 Agent。本轮只固化边界，不换框架。

## 保留什么

- `tools.py` 注册表：L0 直执行，L1/L2 转 pending。
- `runner.py` 工具循环与确认编排。
- `pending_actions` 谁发起谁确认、幂等一次执行。
- 分层上下文：系统规则 → 身份 → 记忆 → 摘要 → 业务对象快照 → 近讯。
- 网页 / 钉钉同一 `AgentRunner`。
- Digital Worker 只执行 Typed ERP Command，不开放通用浏览器 Agent。
- SQLite Agent 库；迁 MySQL 只换 `store.py`。

## 修改什么

- 身份事实从 `staff_bindings` 逐步迁到 `users.user_id`；bindings 留下做渠道映射。
- `classify_intent` 升为 Intent Router 的一等输出（`route` / `domain` / `operation`），未识别再进 LLM。
- `RequestContext` 预留 `tenant_id` / `roles` / `permissions` / `data_scope`，避免以后改所有 handler 签名。
- 会话存储若再演进：消息当事实日志，发给模型的内容继续现算。

## 新增什么（本轮已做或只留接口）

| 项 | 状态 |
|---|---|
| `users` 表 + 采购员归一化 + `resolve_user_by_erp_buyer` | 已做 |
| Intent Router 骨架 | 已做，先包现有意图 |
| `RequestContext` 预留字段 | 已做 |
| `MemoryStore` / `RetrievalRouter` Protocol | 接口预留，无消费者 |
| Tool 上的 `domain` / `concurrency_mode` | 字段预留，不重写全部工具 |

## 明确不引入什么

- LangGraph / Cordis / Grok Build Runtime / 通用 Computer Use。
- Neo4j、生产向量库、Kafka、Celery。
- 多 Agent A2A、自主 Skill、Agent 改自己的 Prompt/代码。
- 完整 IAM / SSO / OAuth。
- 为催办、换货、询价各复制一套 Agent Loop。

## 是否吸收这些运行时概念

| 概念 | 决定 |
|---|---|
| Session Event Log | **思想吸收**，不本轮重写表。消息 + 审计已能追责 |
| ToolSpec | **兼容演进**：现有 Tool 已有 risk / permission / side_effect；preview/execute/verify 分阶段加 |
| Tool Executor | **已有** `_invoke` + `PendingActions.execute` |
| Context Builder | **已有** `context_messages` |
| Model Router | 后置。现在只有一个 LLM 适配器 |
| Cancellation | 后置到长任务 Job；HTTP 对话已有忙时拒绝 |
| Ordered Tool Results | 保持现有顺序回写；不引入并行工具乱序 |
| Plugin-like Capability Registry | **不引入插件**。新能力继续 `registry.register` |

## 推荐的下一步（不是今晚）

1. 钉钉员工通讯录对齐 `users.dingtalk_userid`（需钉钉 API，见后置清单）。
2. 待确认采购员别名由人工拍板后补进 `users`（见分析报告）。镜像库全量扫描和自动种子已完成。
3. 会话消息若要更接近 dsh：加 event type，不改业务表。
4. RetrievalRouter 等有真实 SOP/报价附件再实现。
