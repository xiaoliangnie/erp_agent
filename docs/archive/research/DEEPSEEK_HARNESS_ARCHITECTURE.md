# DeepSeek Harness 架构调研

> 日期：2026-08-17  
> 目的：吸收「完整事实日志 → 派生模型上下文」，不复制 TypeScript / Cordis。  
> 主要来源：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的 `docs/architecture.md`、Session / Event 文档。

DeepSeek Harness（dsh）建立在 Cordis 上：几乎每个子系统都是可替换插件，包括模型适配器、工具注册表、会话日志和 Agent Loop。

## 核心模块

| dsh 包 | 职责 | 本仓库大致对应 |
|---|---|---|
| `core/session` | 只追加的 `SessionEvent` 日志 | `agent_sessions` + `agent_messages` |
| `core/system-prompt` | 提示分段与工具 schema 组装 | `sessions.context_messages` |
| `core/tools` | 带作用域的工具注册与受控执行 | `tools.py` + `runner._invoke` |
| `core/agent-loop` | 默认驱动 | `runner.py` |
| `llm/llm` | 消息/流式词表与适配缝 | `llm.py` |
| compaction 插件 | 超长上下文修剪/摘要 | `sanitize_summary` + 默认关的滚动摘要 |
| Cordis plugin | 服务、事件、可逆副作用 | **不引入** |

## 最值得学的：事实日志 ≠ 模型可见面

dsh 的硬约束：

```text
append-only Session Event Log   ← 完整事实
        ↓ deriveMessages()
model-visible surface           ← 本次真正发给模型的内容
```

规则是：**能进模型请求的东西，必须能从日志重建**。UI 回放看原始 chunk；模型上下文是派生投影。Fork / resume / transcript / telemetry 都从同一条流来。

事件分两类：

- **Session events**（`user/message`、`assistant/chunk`、`tool/call`、`tool/result`、`compaction/*`）：要落盘、能重放。
- **Agent events**（`agent/pre-step`、`agent/request`、`agent/status`）：飞行中的协调，不代替事实日志。

## 和本仓库的映射

| 当前项目 | DeepSeek Harness | 判断 |
|---|---|---|
| `agent_messages` | Session Event Log | 接近，但是「消息表」不是严格 event-sourced |
| `sessions.context_messages` | deriveMessages / surface | 已经是派生投影（规则→身份→记忆→摘要→快照→近讯） |
| `runner.py` | Agent Loop | 保留 |
| `tools.py` | Tool Runtime | 保留；本仓库多了 risk / pending |
| `staff_bindings` / `users` | Context Identity | users 是身份事实；bindings 是渠道映射 |
| `pending_actions` / `jobs` / `outbox` | 无直接等价（业务工作流） | 必须保留，dsh 不管 ERP |
| `operator_memories` | 不是 session log | 跨会话偏好，显式写入 |
| 无 | Cordis plugin / waterfall | **不引入** |

## Compaction

dsh 用 `agent/pre-step` 看上下文压力，必要时先剪工具结果再摘要，失败步骤和失败 turn 之间恢复。本仓库已有消毒摘要和字符预算；默认关滚动摘要是对的——采购数字不能靠压缩残留。

## 明确不复制

- Cordis / TypeScript 插件总线。
- 把工具执行放到通用 waterfall 里让第三方改权限。
- 用 session log 代替 ERP / Work Item 状态。
- 让模型自己决定 compaction 写回哪些业务数字。

## 结论

下一阶段若改会话存储，优先把 `agent_messages` 往「只追加事实、上下文现算」靠，而不是新开 `session_events` 重写。身份、确认、ERP 命令继续走确定性表。
