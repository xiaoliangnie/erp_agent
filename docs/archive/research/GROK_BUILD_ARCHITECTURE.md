# Grok Build 架构调研

> 日期：2026-08-17  
> 目的：吸收运行时边界，不引入 Grok Build 本身。  
> 主要来源：[xai-org/grok-build](https://github.com/xai-org/grok-build)、[docs.x.ai/build](https://docs.x.ai/build/overview)、公开用户指南。

Grok Build 是 xAI 的**终端编码 Agent**（Rust TUI / headless / ACP），不是 ERP 业务运行时。本仓库只回答：哪些模块边界值得吸收。

## 1. 核心 Agent Loop

公开形态是典型的：

```text
用户消息 / slash / skill
      ↓
模型采样（可流式）
      ↓
工具调用（可能并行 / 子 Agent）
      ↓
本机或沙箱执行
      ↓
结果回模型
      ↓
直到文本回复或等待权限
```

入口有三种：交互 TUI、`grok -p` 无头、ACP（`session/new` → `session/prompt` → `session/update`，工具执行可请求批准）。

对本项目：循环本身已经在 `runner.py`。值得吸收的是「先处理确定性命令，再进采样」，而不是再写一套 ACP 服务器。

## 2. Tool 如何注册

工具是独立 crate / 实现（终端、改文件、搜索、MCP）。运行时按配置发现 Skill、Plugin、Hook、MCP。`grok inspect` 能列出当前目录实际装了什么。

对本项目：继续用 `tools.py` 的显式 `registry.register`。不要动态发现 ERP 对象，不要让模型注册工具。

## 3. Tool 执行与 Loop 如何解耦

Grok Build 把「选工具」和「执行工具」分开：模型只发调用，执行在 workspace / sandbox / MCP 适配器。高风险动作可弹权限（`--always-approve` 能关掉，编码场景才合理）。

对本项目：已经解耦。L1/L2 必须走 `pending_actions`，不能用 yolo 批准。

## 4. 插件 / Hook 如何介入

公开能力包括：

- Skill：Markdown 指令，可自动匹配或按名调用；`/skillify` 能把会话收成 Skill。
- Plugin / Marketplace：打包 skill、hook、MCP。
- Hook：在循环前后插入策略。
- MCP：外接数据库、工单、浏览器等。

对本项目：

- Hook 思想可对应权限检查、确认、审计，但应写成普通函数，不引入插件总线。
- **明确不引入** Agent 自己 `/skillify`、自己改 Prompt、自己发布 Tool。

## 5. Session 如何维护

会话可在 ACP 服务进程里跨重连保持。子 Agent 可并行、可有独立 worktree。这是编码产品的会话，不是企业内部员工会话。

对本项目：继续 `channel + session_key + epoch + user_id`。不要群共享一个 Agent Session。不要为催办/换货/询价各起一个 Runtime。

## 6. Error / Cancel

公开材料强调权限拒绝、沙箱隔离、流式更新。取消和失败通过 ACP / TUI 状态回传。没有看到类似本仓库 `ErpUnknownResult` 的「结果未知不得重试」契约。

对本项目：ERP 写失败语义必须继续由业务代码定义，不能交给通用编码 Agent。

## 7. 适合本项目的

| 点 | 怎么吸收 |
|---|---|
| 先命令后模型 | 已有 `session_commands` + Intent Router |
| 工具与 Loop 分离 | 保持 `tools.py` / `runner._invoke` |
| 高风险先批准 | 保持 L1/L2 pending |
| 可检查的能力清单 | `grok inspect` → 本仓库用工具目录 / `/api/health` |
| MCP 只作未来外接知识源 | 不接实时库存/价格 |

## 8. 明显不适合 ERP 的

- 本机 Shell / 改文件 / 通用 Computer Use。
- 最多 8 路子 Agent 并行改同一业务对象。
- `/skillify` 或 Agent 自己创建 Skill。
- `--always-approve`。
- 把 ERP 页面当 workspace。
- 为每个业务场景复制一个 Agent Runtime。

## 结论

Grok Build 是优秀的**编码 Agent 产品**。本仓库吸收「命令优先、工具与循环分离、执行可拦截」，不引入它的 Runtime、Sandbox、子 Agent 或 Skill 市场。
