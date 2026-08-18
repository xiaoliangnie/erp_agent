# Memory 架构建议

> 日期：2026-08-17  
> 候选：Hermes、Letta/MemGPT、Mem0、LangGraph Store、DeepSeek Harness Session/Compaction。Graphiti 只作了解。

## 五种概念必须分开

| 概念 | 本仓库 | 能不能当 Memory |
|---|---|---|
| Session History | `agent_messages` | 否，这是原始历史 |
| Working Context | `context_messages` 的投影 | 否，这是当次发送面 |
| Session Summary | `agent_session_summaries`，默认关 | 压缩历史，必须消毒 |
| User Memory | `operator_memories`，按 `user_id` | 跨会话稳定偏好 |
| Business State | 镜像库 / pending / jobs / 换货 | **绝对不能**依赖 Memory |

## 候选结论

### Hermes

精选、有上限的 `MEMORY.md` / `USER.md`，显式增删，写入可审批，注入扫描。和本仓库最接近。已在 `docs/architecture/会话与钉钉.md` 对齐。

**吸收**：显式记住/忘记、上限、注入扫描、不自动写。

### Letta / MemGPT

核心/归档记忆，Agent 自己编辑画像。

**不引入**：本项目禁止 Agent 自主修改长期记忆。

### Mem0

自动从对话抽取记忆。

**不引入**：会把交期、数量、单号吸进长期记忆。

### LangGraph Store

按 namespace 的 KV/向量存储，本身中性。

**后置**：若以后要多租户命名空间，可以借鉴 key 设计（`tenant/user/kind`），不必引入 LangGraph。

### DeepSeek Compaction

从 session log 派生、超长时修剪工具结果再摘要。

**思想吸收**：摘要是投影不是事实。本仓库已消毒；默认关。

### Graphiti

图谱记忆。询报价关系以后可能有用。

**第一阶段不采用。** `[DEFERRED]`

## 本项目方向

```text
Session Event / Messages
      ├── Context Surface     （当次发给模型）
      └── Session Summary     （消毒、默认关）

User Memory
      └── users.user_id + operator_memories
          只允许：用户说「记住/忘记」、管理员、经确认的提炼

Task / Episodic
      └── work_items / pending_actions / 业务单号
          不进长期记忆

Knowledge
      └── 未来 RAG（制度/SOP/附件）
          不查实时 ERP 数字
```

第一阶段目标：

- 上下文不无限膨胀（预算 + 可选摘要）
- 用户不串线（`user_id`）
- 重启可恢复（SQLite）
- 业务可追溯（审计表）
- 稳定偏好可保留（显式记忆）
- 动态 ERP 数字不进长期记忆

## 写入策略（已拍板）

允许：用户明确「记住…」「忘记…」、管理员配置、经过确认的系统提炼。

禁止：Agent 自己认为重要就永久写入、自动重写用户画像、自动删除旧业务事实。

每条记忆必须有：来源、`user_id`、时间、可删除。
