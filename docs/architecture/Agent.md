# 采购供应链 Agent 架构方案

> 2026-08 修订：在原「薄业务 Agent + 确定性业务工具」结论之上，扩展为四项能力的完整框架
> ——销量预测与订货建议、数据看板、自动换货、钉钉机器人双通道。
> 2026-08-13 再修订：对照业界做法后，主路径仍是确定性工作流 + 确认卡片；
> **订单异常第一期 = SKU 换货**（与采购催办分开）；对话负责把范围收成 `o_id` 并登记 dry-run。
> 明确不做 RAG / MCP 重写运行时 / 通用电脑操控。
> 本文是分阶段实现的依据。**完成度以 [`docs/Agent进度.md`](../Agent进度.md) 为准**，落地或新增能力时改那一份，不要只改本节状态表。

## 1. 需求与子系统对应

| # | 需求 | 子系统 | 状态 |
|---|---|---|---|
| 1 | 用数据库历史数据训练预测模型，LLM 结合预测给订货建议 | `backend/forecast/` + Agent 工具 | **接口与链路已实现**；等销售/库存表与训练好的模型接入（见 `docs/预测.md`） |
| 2 | 数据看板 | 现有 `backend/` + `frontend/` | 已完成，Agent 复用同一查询层 |
| 3 | 订单异常处理（SKU 换货） | `/exchange` + `backend/exchange/` 任务队列 + ERP 页内 worker；对话可定位并登记 dry-run | 第一版已实现；缺的是「异常范围 → 明确 o_id 清单」的只读候选工具 |
| 4 | 钉钉机器人 + HTTP 服务双通道 | `backend/dingtalk/` + Channel Gateway | **已实现，默认关闭**；Stream 需装 `dingtalk-stream`，催办推送可单独开 |
| 5 | 采购主数据缺口（供应商未维护、缺图、合同不可生成） | 只读工具 + 催办通道复用 | **未实现**；与订单换货不是同一类异常，不新开第六页 |

## 2. 设计结论（沿用并扩展）

采用**薄业务 Agent + 确定性业务工具**：模型只负责理解意图、补齐参数、选择工具和组织话术；
查库、预测计算、合同生成、换货执行、钉钉发送全部由普通代码完成。不采用能任意操作电脑或
执行 Shell 的通用 Agent，不绑定 Agent 框架，模型供应商保持中立（`openai_compatible`）。

新增能力同样遵守这条线：

- **预测数字由模型工件 + 确定性公式给出，LLM 不改数字**，只解释、追问缺参、汇总话术。
- **换货执行必须 dry-run 先行**，人工确认后才真正执行。
- 缺数据、缺映射就明确报错停止，不出带占位信息的结果——与合同生成同一哲学。
- **页面和定时任务不经过 LLM。** 看板、台账、合同页、换货页直接调同一批确定性函数；
  每日催办也不经过模型。只有员工用一句话、路径不确定时才进 Agent Core。
- **ERP 数字禁止 RAG。** 采购单、库存、预测全部走固定参数化工具；向量检索只留给以后
  可能出现的非结构化材料（合同条款说明、操作手册），不索引业务表。

### 2.1 业界对照：采纳与明确不做（2026-08-13）

对照来源是近期可核对的做法，不是产品宣传：Anthropic《Building effective agents》（工作流 vs
Agent、工具接口比 prompt 更重要）、企业 ERP 集成共识（结构化数据用 tool calling 不用 RAG）、
SAP/Microsoft 的「异常处理坐在 ERP 旁边、写回走已发布接口、身份跟登录人走」、以及采购 Agent
开源工作流里「人闸 + 幂等键、Agent 永不自行下 PO」。

**采纳、且与现有代码对齐或补强：**

| 做法 | 落到本仓库 |
|---|---|
| 能写成固定步骤的不要做成开放 Agent | 催办、合同、换货、看板统计走页面/定时任务；`runner.py` 只服务对话溢出 |
| `propose_*` / `execute_*` 拆开 | 已有：L1/L2 → `pending_actions`，确认后才执行 |
| 风险分级治理，不是「全自动 / 全人工」二选一 | 已有 L0–L3；本期**不**按金额自动放行 L1 |
| 异常驱动、双车道 | **采购异常**（逾期催办、主数据缺口）走台账/钉钉，不经换货；**订单异常**（要改 SKU 才能履约）走换货工作流。对话只负责把范围收成明确 `o_id` 并登记 dry-run |
| 工具是资产，Agent 是薄包装 | 继续只在 `tools.py` 注册表加工具；不引入 LangGraph / Copilot Studio |
| 给模型的工具结果要像给初级同事的接口 | 见 §5.2：摘要信封进上下文，全量明细只给页面 |
| 写操作前/后都留痕 | 已有 `tool_executions`；确认执行后要把「预览 vs 实绩」差记进同一条审计 |
| 身份跟具体员工走，不跟共享机器人走 | `staff_bindings` 升为身份枢纽，见 §9；共享 `AGENT_API_TOKEN` 只做传输鉴权 |
| 上线靠黄金对话，不靠感觉 | 见 §5.3；工具层继续 unittest，对话层补回放集 |

**明确不做（写在这里是为了挡住下一轮「要不要上 MCP / 要不要向量库」）：**

- **不把 MCP 当运行时。** 现在的工具注册表已经是模型唯一入口。MCP 只是同一张表的可选适配器
  （给 Cursor / Claude Desktop 用），不改 `runner.py`，不重写 handler。见 §14。
- **不把采购明细向量化。** 问「604264 还有多少没入库」必须命中 `get_purchase_order`，
  不允许从昨天的切片里语义检索一个近似数字。
- **不让模型写 ERP、不让模型开浏览器。** 换货继续走已登录标签页里的 userscript；没有开放写接口
  就不做「Agent 创建采购单」。Playwright 全自动仍是 §7 的后续可选项，不进本期。
- **不上多 Agent / A2A。** 一个采购助手、一张注册表、一个确认状态机。拆成「查单 Agent +
  合同 Agent + 催办 Agent」只会增加会话和权限缝。
- **不按金额做有界自治。** 业界常说「阈值以下自动过」。我们人少、合同主数据还是空的，
  L1/L2 继续谁发起谁确认。催办定时推送已经是无 LLM 的确定性任务，不要再套一层模型审批。

## 3. 总体架构

```text
 看板 / 台账 / 合同 / 换货（页面，不经 LLM）
 每日催办 · 主数据缺口推送（定时，不经 LLM）
 对话溢出：网页 /chat · 钉钉 Stream
          │                    │
     HTTP API (Bearer)    DingTalk 适配器
          └──────────┬─────────┘
              Channel Gateway
     身份映射 · 会话 · 消息幂等 · 限流
                     │
                Agent Core          ← 仅对话需要
      工具循环 · pending-action · 审计
                     │
    ┌─────────┬──────┴──────┬──────────┐
  采购查询    预测/订货建议   合同生成    换货执行
  （只读）    forecast/     contracts.py exchange/
    │            │              │           │
 镜像库只读   模型工件        模板+config  ERP 页内 worker
 （hanli.env）                 主数据
                     │
            Agent 业务库（现为本地 SQLite）
   sessions / runs / tool_executions / pending_actions / audit
```

页面、Agent 工具、钉钉催办共用 `source_cache()` 和同一套口径函数。Agent **不另开数据源**，
也 **不直连聚水潭 OpenAPI**——写路径只有换货 worker 这一条，读路径只打本地镜像。

## 4. 代码布局

```text
backend/
  app.py                # 现有 HTTP 服务，挂新路由 /api/agent/chat、/api/forecast/*
  database.py           # 现有只读查询；每个查询就是一个固定参数化工具的实现
  contracts.py          # 现有合同生成，挂入 L1 确认流
  agent/
    llm.py              # openai_compatible 客户端（AGENT_API_BASE，标准库 HTTP 即可）
    tools.py            # 工具注册表：名称 + 入参 JSON Schema + 风险级 + handler
    runner.py           # 工具循环，步数上限 AGENT_MAX_TOOL_STEPS
    sessions.py         # 会话与消息持久化（渠道 + 会话键唯一）
    actions.py          # pending-action 确认状态机（见 §5）
    audit.py            # agent_runs / tool_executions 落库
  forecast/
    dataset.py          # 从实时数据库抽取训练/推理特征
    models.py           # Forecaster 接口 + Baseline 实现（见 §6）
    store.py            # 模型工件读写与版本管理
    service.py          # 预测查询 + 订货建议的确定性计算
  exchange/
    service.py          # 换货任务登记、规则管理与结果归档（执行在浏览器端，见 §7）
  dingtalk/
    stream.py           # Stream 客户端（后台线程，自带事件循环）
    sender.py           # 消息 / ActionCard / 时效文件链接发送
    identity.py         # 钉钉用户 ↔ 员工/角色映射
scripts/
  train_forecast_model.py   # 离线训练入口（定时跑，服务端只读工件）
  run_agent_cli.py          # 命令行对话调试入口，不经 HTTP
```

现状衔接：`/api/agent/contracts/*` 三个直连 REST 接口保留，它们就是确定性工具层的 HTTP
形态；对话 Agent 在进程内直接调用同一批函数，不绕 HTTP。

## 5. Agent Core：工具循环与确认状态机

工具注册表是唯一入口：每个工具声明名称、入参 JSON Schema、风险级（§9）和 handler。
LLM 通过 function calling 选工具；L0 工具直接执行，L1/L2 工具走确认状态机：

```text
L1/L2 工具被选中
  → 创建 pending_action（工具名 + 参数 + 发起人 + 渠道，状态 pending，有效期 30 分钟）
  → 渠道渲染确认：网页按钮 / 钉钉 ActionCard 或回复「确认 <编号>」
  → 确认：以 pending_action_id 为幂等键执行一次，状态 executed，结果回贴原会话
  → 超时 / 取消：expired / cancelled，不可再执行
```

这套机制是「网页和钉钉同一能力、同一确认流」的关键，所有 L1/L2 工具不允许绕过。
幂等分两层：入口层用钉钉消息 ID 去重（断线重连不重复触发），执行层用 pending_action_id
保证确认只生效一次。

System prompt 注入：业务日期、当前员工身份与角色、可用工具清单；会话上下文取该会话
最近若干条消息。`AGENT_ENABLED=false` 或未配置密钥时 `/api/agent/chat` 返回 503，
与现有 `AGENT_API_TOKEN` 未配置的行为一致。

对话页已经把每步工具名和待确认卡片画出来——这是透明度要求，不要改成「只给一段自然语言」。

### 5.1 工作流优先，Agent 只覆盖路径不确定的问句

Anthropic 把系统分成两类：**工作流**（代码规定步骤，中间可插 LLM）和 **Agent**（模型自己选工具、
自己决定何时停）。本项目里四条主路径都是工作流，不要再包一层工具循环：

| 路径 | 编排在哪 | LLM 是否参与 |
|---|---|---|
| 看看板 / 导出台账 CSV | 前端直接打 `/api/dashboard` `/api/delivery` | 否 |
| 生成合同 | `/contract` → `contracts.py` → openpyxl 写表 | 否（对话里走同一函数，但是 L1 确认） |
| 订单异常 / 换货 | `/exchange` + userscript 队列；对话可定位候选并登记 dry-run | 定位与话术可以走 LLM；筛单规则和 ERP 写入不行 |
| 每日催办 | `DINGTALK_REMINDER_ENABLED` 定时任务 | 否 |
| 「张三名下逾期还有哪些」「把这张单做成专票合同」 | `AgentRunner` 工具循环 | 是 |

新增能力时先问：步骤能不能写死。能写死就加页面或定时任务，只把同一 handler 挂进注册表给对话复用。
不要为了「更像 Agent」把催办改成模型每天自己决定催谁。

### 5.2 工具的 Agent-Computer Interface（给模型的返回值）

L0 工具经 `as_tool_envelope` 后进模型，形状是信封；超长时 `encode_tool_result`
丢掉 `data` 只留 `summary`。`get_purchase_order` 明细超过 30 行只留 SKU/数量/待入库/交期。
审计仍用 `summarize()`，不把信封之前的全量 JSON 再写一遍。

给模型的返回：

```text
{
  "ok": true,
  "summary": "采购单 604264，供应商甲，待入库 120，明细 8 行（已截断展示前 8 行）",
  "data": { ...截断后的结构化字段... },
  "truncated": false,
  "hint": "需要某一行的单价请再查，不要口算合计"
}
```

约束：

- `summary` 是短中文，数字只来自 handler 算好的字段，模型只许引用不许重算。
- 列表类工具继续用现有 `_limit`（默认 20，封顶 200）；超过 `returned` 的部分只报总数。
- 明细超过约 30 行时 `truncated=true`，只留 SKU / 数量 / 待入库 / 交期，规格长文本丢掉。
- 审计表继续用现有 `summarize()` 截断，不把信封之前的全量 JSON 再写一遍。

工具描述按「给初级同事的 docstring」写：何时用、何时不用、和相邻工具的边界。
换货相关三个工具已经这样写了（必须先解析 `o_id`）；查采购单 / 催办 / 看板也要补「不要用 A 代替 B」。

### 5.3 评测：工具用例不够，对话要有黄金回放

`tests/test_agent.py` 覆盖注册表、确认流、换货消歧，**不调用真模型**，这层必须保住。
缺的是「员工原话 → 期望选哪个工具、入参是什么、缺参时必须追问」的回放集。夹具在
`tests/fixtures/golden_dialogues.json`（约 20 条），CI 用假 LLM 按脚本吐 `tool_calls`
跑 `GoldenReplayTests`；真模型联调后再加一个可关的夜间集。

最低要覆盖的场景（与当前 system prompt 里的硬约束对应）：

1. 查一张纯数字采购单 → `get_purchase_order`
2. 「我名下逾期」→ `delivery_reminders` 带 `buyer` + `overdue`
3. 生成合同但供应商未维护 → 工具报错，回复说明缺什么，不编造
4. 「把 A 换成 B」但没说 `o_id` → 先按待发货 + 含源 SKU 查订单镜像，不直接 `submit_exchange_dry_run`
5. 「异常订单」且给了源/目标 SKU → 走换货，不走催办；没给 SKU 就追问类型（同款换码 / 指定替换 / 白名单）
6. 「异常订单」但说的是备注、超卖、地址 → 说明第一期做不了，不得自行定义规则
7. 催办发送 → 只登记 L2 pending，话术不得声称已经发到群里

没有这几条回放之前，不扩大 L1/L2 工具面。订单异常候选清单是 L0，可以先做。

## 6. 预测与订货建议

### Forecaster 接口（先定接口，实现可替换）

```python
class Forecaster:
    """预测模型统一接口；调用方只依赖这里，换实现不改调用方。"""
    name: str        # 如 baseline-seasonal / lgbm-v1
    version: str     # 训练产物版本，写入每次建议的审计记录

    def fit(self, dataset): ...
    def predict(self, keys, horizon_days):
        """keys 为 SKU 或款式编码；返回逐日点预测与分位区间
        [{key, date, p50, p10, p90}, ...]"""
    def save(self, path): ...
    @classmethod
    def load(cls, path): ...
```

- 第一版 `BaselineForecaster`：移动平均 + 季节因子，纯 pandas，先把链路打通；
  之后可替换为 LightGBM / 统计模型，接口不变。
- 预测粒度（SKU/款式 × 日/周）与预测范围尚未敲定（§14 待定 1）；接口按
  `keys + horizon_days` 设计，本身不预设粒度，粒度定了只改 `dataset.py` 和训练脚本。
- 训练完全离线：`scripts/train_forecast_model.py` 从实时数据库抽历史 → 工件存
  `data/models/<version>/`（gitignored），带训练窗口、评估指标、特征清单元数据。
- 服务端启动时加载最新版本；`/api/forecast/*` 提供预测查询。

### 订货建议 = 确定性计算，LLM 只解释

```text
建议下单量 = 交期内预测需求（按服务水平取 p50 或更高分位）
           + 安全库存（由预测区间宽度或历史波动折算）
           − 可用库存 − 在途待入库
建议下单日 = 需求缺口出现日 − 供应商交期 − 缓冲天数
```

LLM 的职责：引用预测区间、库存、在途、交期解释这份建议；追问缺参（预测周期、服务水平）；
把多 SKU 结果汇总成一页话术。员工确认后才形成正式订货建议单（L1，走确认流）。
每次建议在 `forecast_runs` 记录模型版本 + 输入快照，事后可复现。

### 数据前提（该子系统的第一步）

实时镜像（`hanli.env` 指向的 MySQL）已有 API 维护的采购主表/明细和订单主表/明细；
销量预测仍至少需要**现势库存表**，可选退货表。订单可作为需求来源，但在库存数据到位、
状态口径核验和退货扣减规则明确前不启用正式订货建议。

## 7. 订单异常处理：手段就是换货

销售侧「异常订单」第一期**只指要改 SKU 才能继续履约的订单**。处理手段就是现有换货工作流，
不是另做一套异常检测模型，也不是采购催办。

两类异常不要混：

| 侧 | 典型情况 | 处理 |
|---|---|---|
| 订单（履约） | 待发货缺码、错挂 SKU、活动款替换、鞋垫等白名单跨款 | **换货**：定位 → dry-run → `/exchange` 二次确认 → worker 写 ERP |
| 采购（到货） | 逾期、供应商未维护、合同缺图 | 催办 / 主数据缺口，**不换货** |

第一期锁定三种替换规则（换货页已经在用，不是新发明）：

1. **同款换规格**：源、目标 `i_id` 相同，改颜色尺码。
2. **指定源 → 目标**：员工给出两个 SKU，仍校验同款（除非走下一条）。
3. **特殊白名单跨款**：目前仅 `XZ25401308-101` 那组已维护映射。

对话里员工说「异常订单」时：有源/目标 SKU 就按待发货含该 SKU 收 `o_id` 再登记 dry-run；
没给 SKU 就追问属于上面哪一种。备注异常、超卖、地址错误第一期明确不做，不准模型自造规则。

缺的能力不是写 ERP，而是 **L0 候选清单**：按镜像把「待发货 ∩ 含源 SKU ∩ 店铺/日期」收成
明确 `o_id` 列表（可截断、返回总数）。现有 `search_sales_orders` 只做关键词搜索，撑不起
「把这批待发货里的 A 换成 B」。确认和执行仍然必须落在这份 `o_id` 清单上，不允许「大概换一批」。

业务与工具已确认：换货执行走 `jst-order-exchange/` 规则模型（direct / map / map_table）+
页面内 `_ACP('ChangeItem'/'Change')`，默认 dry-run。原形态是 Python CLI 经 macOS AppleScript
驱动 Chrome，在 Linux 服务器上跑不了，且核心逻辑本来就是页面内 JS。

**交互模型（已确认）：真实写入仍在换货页二次确认后由已登录 ERP 标签页里的 JS worker 执行；
对话只负责定位候选和登记 dry-run，员工可以不自己去 ERP 点换货，但必须在 `/exchange` 看过试算清单。**

```text
换货配置页（前端 /exchange 路由）
  商品数据选 源 SKU / 目标 SKU（或规则预设）+ 订单定位参数
        │ 提交 → exchange_jobs(pending)
        ▼
ERP 订单页里的后台 worker（userscript，长轮询领任务）
  ① 按定位参数解析目标订单 → ② 逐单试算（plan）→ ③ 回报试算清单
        ▼
配置页展示 dry-run 清单（逐单：o_id / 源→目标 / 数量 / 执行模式 / 跳过原因）
  员工核对 → 确认（L2，pending-action 发放一次性 confirm_token）
        ▼
worker 拿 token 执行：_ACP 接口级调用，逐单回报进度 → 最终结果归档
```

### 订单定位参数（要求精确命中）

任务的 `targets` 支持显式清单和组合筛选，同时给取交集；**试算结果永远落到明确的
o_id 清单**，确认针对的就是这份清单，不存在"大概换一批"：

```json
{
  "o_ids": ["10012345"],            // 显式内部订单号，最高优先
  "so_ids": ["平台单号"],            // 线上单号，worker 反查 o_id
  "filters": {                       // 组合筛选，均可选
    "shop": "抖音旗舰店",
    "date_from": "2026-08-01", "date_to": "2026-08-11",
    "status_include": ["待发货"],    // 默认排除 取消/退款/关闭
    "labels_include": [], "labels_exclude": [],
    "contain_sku": null,             // 默认 = 规则源 SKU
    "qty_equals": null               // 如：源 SKU 数量 = 1 才换
  },
  "limit": 500                       // 单任务上限，超出分批
}
```

解析路径：现阶段由 worker 在页面内用列表查询接口（`LoadDataToJSON`，见《聚水潭数据
接口记录》）带条件查单，可翻页、不受当前页显示限制；实时订单库到位后改为**服务端直接
解析出 o_id 清单**（更准），worker 只按清单干活。

### 效率

- 写入是接口级 `_ACP` 调用（官方前端同款），单均几十～几百 ms；顺序执行 + 可配间隔
  （默认 100–250ms，防触发 ERP 风控），千单量级约十几分钟。
- 比原 CLI 快在：去掉每次 osascript 进程往返和 0.5s 轮询，worker 在页内原生跑循环，
  进度直接回报服务端（油猴 `GM_xmlhttpRequest` 可同时绕开 CORS 与 https→http 混合
  内容限制）。

### 分工与前提

| 环节 | 在哪 | 说明 |
|---|---|---|
| 配置界面 | Agent 系统 | 商品数据先用采购明细出现过的 SKU + `config/products.json`；商品主数据表进实时库后切换 |
| 任务队列 / 确认 / 审计 | 服务端 | 状态机 pending → planning → awaiting_confirm → executing → done/failed/`stuck`；试算领取超时退回 pending，执行超时标 `stuck` 不重投；确认走 §5 pending-action |
| 订单解析 / 试算 / 执行 | ERP 页内 worker | `plan.py`/`rules.py` 规则匹配移植成 JS；`engine.py` 的页面 JS 片段直接复用 |
| 全自动（无人值守） | 后续可选 | Playwright 只当浏览器宿主：`page.evaluate()` 注入同一段 worker JS，仍走 `_ACP` 接口级调用，**速度与油猴相同、不是模拟点击**；代价是要维护专用账号登录态（扫码、续期、风控），不进第一期 |

**「后台执行」的前提：有一个已登录 ERP 的浏览器标签页开着**——部署那台常开台式机上
挂一个登录好的 Chrome 即可充当执行器；标签页不在线时任务停在队列，配置页提示 worker
离线。服务端始终不碰 ERP 登录态。

```python
class ExchangeService:
    def submit(self, rules, targets, operator): ...  # 校验规则/SKU/定位参数，建任务
    def next_job(self, worker_id): ...               # worker 长轮询领任务
    def report_dry_run(self, job_id, plan): ...      # 回报试算清单 → awaiting_confirm
    def confirm(self, job_id, operator): ...         # 界面确认，发一次性 confirm_token
    def report_progress(self, job_id, batch): ...    # 执行中逐单进度
    def report_result(self, job_id, result): ...     # 最终结果，任务收口
```

约束：缺试算回报不发 `confirm_token`；`job_id` 幂等；worker 用独立 token 认证；
全程落 `exchange_jobs`。

## 8. 通道层：页面与推送是主路径，对话是溢出

两个渠道对 **Agent Core** 仍然等价（同一注册表、同一确认流），但对员工来说主入口不是聊天：

| 员工要做的事 | 走哪 |
|---|---|
| 看进度、导出、筛采购员 | `/dashboard` `/ledger` |
| 出合同 | `/contract`（不经模型） |
| 处理要换 SKU 的异常订单 | `/exchange` 为主；对话可按待发货+源 SKU 定位并登记 dry-run，写入仍要换货页二次确认 |
| 每天被提醒谁逾期 | 钉钉群 @，定时任务，不经模型 |
| 临时问一句、或在群里确认一条 pending | `/chat` 或钉钉对话 |

- **Web 对话**：`POST /api/agent/chat`（Bearer `AGENT_API_TOKEN`），非流式即可；确认走
  `POST /api/agent/actions/{id}/confirm`。共享 token 只证明「能打到这台服务」，**不证明是谁**；
  请求里的 `operator` 必须能对上 `staff_bindings` / ERP 采购员姓名，对不上就只读或拒绝 L1/L2。
- **钉钉**：Stream 模式（服务进程主动长连，无需公网 IP / 回调地址）。
  `conversationId + senderId` 建立隔离会话；未绑定用户只给只读或拒绝。确认用 ActionCard
  或回复「确认 <编号>」；合同等文件只通过有时效的下载地址发送，不暴露服务器文件路径。
- **定时主动推送（已确认）**：每天早上按台账页同一套四波口径（T-20 / T-10 / T-1 / 逾期）
  生成催办清单，**发到采购群并 @ 对应采购员**。后续主数据缺口（供应商未维护、近期采购 SKU 无图）
  走同一发送服务、另一类模板，仍然不经 LLM。频率默认每天一次，可配。
- `DINGTALK_ENABLED=false` 时钉钉线程不启动，网页链路不受影响；
  `DINGTALK_REMINDER_ENABLED` 可单独开催办，不依赖对话。

## 9. 权限与风险分级

| 级别 | 动作 | 要求 |
|---|---|---|
| L0 只读 | 查采购单、交期与待入库、看板统计、预测查询 | 直接执行，记审计 |
| L1 生成产物 | 合同预览/正式合同、催办清单、订货建议单 | 预览 → pending-action 确认 → 生成 |
| L2 对外动作 | 发钉钉催办、执行换货 | dry-run/预览 + 确认 + 幂等 + 审计 |
| L3 改主数据 | 价格、税率、供应商映射 | 审批流，第一阶段不开放 |

已确认：第一期只有 `viewer` / `operator` 两档，确认动作谁发起谁确认。
**不按金额或「模型历史准确率」自动放行 L1/L2**——人少、主数据缺口大，误放行的成本高于多点一次确认。
viewer 只能跑 L0；拒绝时工具返回结构化 `permission`，不靠 prompt 拦。

`staff_bindings` 是身份枢纽，不是权限矩阵：把 ERP 采购员姓名、网页对话署名、钉钉 userId/手机号
收成同一条员工记录。同一人在 ERP 里可能同时有花名和「真名（花名）」（如「利特」与「李佳冬（利特）」），
绑定任一署名即可：群内 @、确认人校验都视为同一个人。未绑定的钉钉用户只读；
网页若只带共享 token、署了一个对不上的名字，L1/L2 直接拒绝。

ERP 镜像库只读；禁止模型生成或执行任意 SQL，每项查询对应固定参数化工具。
服务端始终不持有 ERP 登录 cookie——换货执行态只存在员工（或那台常开台式机）的浏览器里。

## 10. Agent 业务库

与实时数据库同一 MySQL 实例即可，但用独立 schema 和独立账号（如 `procurement_agent`），
绝不写 ERP 原表。

| 表 | 用途 |
|---|---|
| `agent_sessions` / `agent_messages` | 会话与消息（渠道 + 会话键唯一） |
| `agent_runs` / `tool_executions` | 每轮运行与每次工具调用（入参、结果摘要、耗时） |
| `pending_actions` | 确认状态机与幂等执行结果 |
| `staff_bindings` | 采购员姓名 ↔ 网页署名 ↔ 钉钉 userId/手机号（身份枢纽；群内 @ 与 L1/L2 发起人校验都读这里） |
| `forecast_runs` | 每次订货建议引用的模型版本与输入快照 |
| `exchange_jobs` | 换货任务全生命周期 |
| `generated_contracts` / `notification_deliveries` | 合同产物与通知投递结果 |
| `approval_requests` | L3 审批（后期） |

供应商/商品/人员映射后续从 JSON 迁移为带版本与修改人的业务表（P2，迁专用机器后；
单价仍只认配置或 ERP）；历史合同保留生成时的映射快照。

## 11. 部署与运行形态

第一阶段仍是**单进程**，三类线程：

1. HTTP 主服务（现有 `ThreadingHTTPServer`）；
2. 钉钉 Stream 客户端线程（自带事件循环，断线重连不影响网页链路）；
3. 任务工作线程：执行确认后的长任务（合同渲染、换货执行），带失败重试。

模型训练离线跑（cron / 定时任务），服务端只读工件。`/api/health` 给出库连通、镜像滞后、
国标同步、换货、Agent、预测工件和钉钉 Stream / 催办状态。`scripts/health_watch.py`
每 5 分钟拉一次，异常发钉钉，不另建监控系统。DB、LLM、钉钉调用全部设超时。
部署已确认：**先跑在一台常开的办公台式机上**，后续可能迁服务器——配置全走 `.env`，
不写死路径和地址，保证可迁移。固定内网 IP，systemd（Linux）或注册系统服务（Windows）
自启，防火墙只放办公网段；钉钉 Stream 模式不需要公网回调，台式机部署即可用。

## 12. 配置与依赖

`.env` 已预留 `AGENT_*` 与 `DINGTALK_*`，直接启用；新增两项：

```bash
AGENT_DATABASE_ENV_FILE=agent-db.env   # Agent 业务库（独立 schema/账号）
FORECAST_MODEL_DIR=files/data/models   # 模型工件目录（gitignored）
```

数据库凭证由 `hanli.env` 单独管理，供应链代理 Client 凭据放在 `.env`；真实文件全部在
`.gitignore`，仓库只保留无凭据的 `*.example`。依赖按阶段引入，保持最小：

- LLM 调用保持供应商中立：缺省 `openai_compatible`（chat/completions + API Key）；
  `AGENT_PROVIDER=codex_oauth` 复用本机 `~/.codex/auth.json`，走 ChatGPT Codex 订阅额度
  （与 OpenClaw 同通道，见 README）。两种都是标准库 HTTP，零新增依赖；
- 钉钉：`dingtalk-stream` 已在 `requirements.txt`。Stream 对话要 `DINGTALK_ENABLED` +
  AppKey/Secret；催办可单独开。群内「绑定 姓名」写入 `staff_bindings`；
- 换更强预测模型时：再加对应库（如 lightgbm）。

## 13. 实施路线

| 阶段 | 内容 | 交付 / 验收 |
|---|---|---|
| 1. Agent Core + Web 通道 | `backend/agent/` 全套 + `/api/agent/chat` + 只读工具（查单、交期与待入库、看板统计）+ CLI 调试入口 | **已实现**：前端 /chat 路由、`scripts/run_agent_cli.py`，全部调用落审计；**待接真模型联调** |
| 2. 确认状态机 + 业务库 | Agent 业务库建表；把现有合同生成挂入 L1 确认流 | **已实现**：`pending_actions` 状态机 + `/api/agent/actions/{id}/confirm`，合同生成为 L1；待走一次真实「查单 → 预览 → 确认 → 下载」验收 |
| 3. 预测子系统 | 销售/库存表进实时数据库 → `dataset` → Baseline Forecaster + 训练脚本 + `/api/forecast/*` + 订货建议工具 | **接口与链路已实现**；**等销售/库存表与训练好的模型**，接入方式见 `docs/预测.md` |
| 4. 钉钉接入 | Stream 客户端、`staff_bindings`、消息幂等；先开只读、合同预览与定时催办推送，再放开确认类动作 | **代码已就绪**：`scripts/run_dingtalk_cli.py`、群内「绑定 姓名」、启动日志。待填 AppKey/Secret 并拉机器人进群后验收 |
| 5. 换货接入 | 换货配置页 + SQLite 任务队列 + ERP userscript。组合筛选待销售订单库接入后开放 | **第一版已实现**；待在真实 ERP 测试环境完成一次人工确认验收 |
| 6. 对话硬化（本轮补进路线） | 工具结果信封（§5.2）；黄金回放（§5.3）；`staff_bindings` 校验网页 `operator`；**订单异常候选**：给 `search_sales_orders` 补 status/date/shop + 源 SKU 组合筛选（默认待发货），把范围收成 `o_id` 清单；L0 `master_data_gaps` | **黄金回放 / 网页 operator 校验 / `master_data_gaps` / 台账发送提醒已落地**。换货写入仍是 L1 dry-run + 换货页二次确认。工具结果信封、订单候选筛选的 status/date/shop 仍待做 |
| 7. 运维硬化 | 监控告警、失败重试、备份、L3 审批流 | 按需 |

阶段 3 的数据同步与阶段 1–2 无依赖，可并行准备；阶段 4 的定时催办推送不依赖 LLM 和
Agent 核心，`DINGTALK_REMINDER_ENABLED=true` 即可单独上线（§15 待定 4）。

### 与本文档的两处实现差异

1. **Agent 业务库第一阶段用本地 SQLite**（`AGENT_DATABASE_PATH`，默认 `data/agent.sqlite3`），
   不是 §10 写的 MySQL 独立 schema。表名、字段和状态机与 §10 一致，连接与建表集中在
   `backend/agent/store.py` 一处，迁到 MySQL 只换这一层，`sessions.py` / `actions.py` /
   `audit.py` 的调用面不变。**换 MySQL 是 P2，迁到专用机器之后再做**（与镜像库同一套备份）。
   理由：与已上线的换货任务队列保持一致，且离线可测。
2. **`backend/agent/store.py` 是本文档 §4 代码布局之外新增的模块**，承担三个模块共享的
   连接与 schema，避免同一套建表语句抄三遍。

## 14. 可扩展功能预留（迁专用机器之后的 P2；本期不实现）

| 功能 | 数据来源 | 预留的口 |
|---|---|---|
| 供应商绩效评价 | 现有采购主表/明细（交期达成率、逾期率、入库速度） | 工具注册表留 `supplier_scorecard` 只读工具位；指标口径届时在 README 口径章节定义 |
| 库存预警与滞销分析 | — | **取消**，不再占位 |
| 价格异常监控 | — | **取消**，不再占位 |
| 自动创建采购单草稿 | — | **取消**，不再占位 |
| 主数据缺口汇总 | 现有采购单 + `config/suppliers.json` / 图片缓存 | L0 工具 `master_data_gaps`：最近 N 天采购涉及的供应商未维护、SKU 无图、票种缺价、分类未映射国标目录族。输出催办风格 markdown |
| MCP 适配器 | 现有 `ToolRegistry.schemas()` | 可选：把同一张注册表暴露成 MCP server，供 Cursor 等客户端发现。handler、风险级、确认流全部不改。**不作为运行时替换** |

预留原则：只在工具注册表、表结构和风险分级里占位，不提前写实现；上线任何一项都是按
§5 的注册表加工具，不改 Agent Core。`RESERVED_TOOLS` 目前只留 `supplier_scorecard`。
`master_data_gaps` 已在阶段 6 注册为 L0。

## 15. 待定问题（grillme 拷问记录，敲定后移入正文）

| # | 待定 | 影响范围 | 记录日期 |
|---|---|---|---|
| 1 | 预测粒度（SKU/款式 × 日/周）与预测范围 | 只影响 `dataset.py` 的抽取和训练脚本参数；`Forecaster` 接口按 `keys + horizon_days` 设计，粒度定了不改调用方 | 2026-08-11 |
| 2 | 实时销售/库存表的到位时间与表清单（主路径）；兜底采集的频率 | 表名列名已做成 `FORECAST_SALES_*` / `FORECAST_INVENTORY_*` 配置，表到位只改 `.env`；在此之前可用 `--csv` 离线训练 | 2026-08-11 |
| 3 | LLM 具体供应商 | 已接 `openai_compatible`（DeepSeek 已配）和 `codex_oauth`（本机 ChatGPT 登录）。默认仍用 DeepSeek；Codex 吃订阅额度，有 5h/周窗口，对话量大改回按量接口 | 2026-08-13 |
| 4 | 首个上线里程碑（催办推送可独立提前上线） | 代码已就绪，两个开关分别控制：`DINGTALK_REMINDER_ENABLED`（推送）、`AGENT_ENABLED`（对话） | 2026-08-11 |
| 5 | 网页对话的 `session_key` 是否按员工拆开（现在客户端自带、token 共享） | 确认人已经是「谁发起谁确认」；未绑定时 L1/L2 拒绝即可，不必先做登录系统 | 2026-08-13 |
| 6 | 黄金回放集由谁出原话（建议采购员抽 20 条真实问句） | 只影响 `tests/` 里的对话夹具，不改工具 handler | 2026-08-13 |
| 7 | 订单镜像里「待发货」等状态的实际取值；备注 / 旗帜 / 超卖类异常要不要做 | 第一期 SKU 替换已敲定（§7）。状态枚举只影响候选工具的默认 `status_include`；其它异常类型未做之前继续拒绝 | 2026-08-13 |

已敲定并移入正文：换货运行载体（浏览器 userscript，§7）；催办推送形态（群内 @ 对应
采购员、每天一次，§8）；LLM 部署形态（云端 API 起步，§12）；权限（员工同权、不按金额
自动放行，§9）；部署位置（内网常开台式机、可迁服务器，§11）；扩展功能清单（§14）；
主交互不是聊天（§2.1 / §8）；ERP 数字不做 RAG；MCP 不当运行时；
**订单异常第一期 = SKU 换货（§7）**。
