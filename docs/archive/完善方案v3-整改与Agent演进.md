# 完善方案 v3：审查整改 · 品控台账 · Agent 记忆与会话

> **已归档。** 当前执行文档改为 [`docs/开发.md`](../开发.md)，不要再往本文加内容。
>
> 2026-08-13 制定，承接《MVP完善方案.md》（v2，其 P0 与 P1-5~P1-8 已落地）。
> 输入是当日 7 路子代理全量代码审查（约 4 万行逐行 + 实测复现），
> 存档在 `outputs/审查报告-子代理汇总-20260813.md`（outputs 不进版本库，阅后自取要点）。
> 约束沿用 v2：不引入 git 流程、暂不做备份、迁专用机器前单机运行；时区统一东八区。
>
> 本文四个部分互相独立可分头执行：§1 整改路线（含决策点）、§2 品控问题台账（新能力）、
> §3 Agent 上下文压缩 / 记忆 / 会话管理、§4 docs 目录归纳。总执行顺序见 §5。

---

## 0. 审查结果分析

### 0.1 数字总览

| 审查路 | 范围 | P0 | P1 | P2 | P3 |
|---|---|---|---|---|---|
| agent-core | `backend/agent/` 全部 + 确认流实测 | 1 | 3 | 10 | 4 |
| http | `backend/app.py` 路由 / 鉴权 / 静态托管，临时端口实测 | 1 | 5 | 7 | 3 |
| contract | 合同 / 国标 / 换货三模块 | 1 | 6 | 9 | 5 |
| data | 镜像同步 / 查询 / payload 契约 | 0 | 2 | 6 | 4 |
| frontend | `frontend/src/` 全部 49 文件 | 0 | 3 | 8 | 7 |
| periph | 催办 / 钉钉 / 预测 / 测试 / 文档一致性 | 0 | 2 | 7 | 8 |

各报告间有交叉命中（同一处问题从不同侧面确认），按主题去重后真正要动手的约 40 项，
已全部编入 §1 的 R0 / R1 / R2 批次。**多数 P1 以上结论带实测复现**，不是静态猜测。

### 0.2 六个横向根因（比逐条清单更重要）

1. **无鉴权面 × 无输入校验的叠加**。`/api/contracts/*` 六条路由全部无鉴权（v2 决策点 3
   的既定选择），单看可以接受；但审查证明它们同时具备：缓存键放大（`?year=0000`~`9999`
   → 每键一份全年明细副本，直通 OOM，`app.py:149`）、`priceOverrides` 负数 / `1e308` /
   NaN 税率直进合同（`contracts.py:406,380`）、`invoiceType` 未验证进文件路径
   （`app.py:331-336`）、`soffice` 无超时无隔离无清理（`contracts.py:507-519`）。
   「无鉴权」被三路审查独立确认为放大器：**修的不是鉴权，是把每个无鉴权入口自身做硬**。
2. **fail-fast 哲学执行不一致**。CLAUDE.md 承诺「缺字段抛 ValueError，绝不出带占位信息的
   合同」，实际有四处兜底（单位兜底"个" `contracts.py:435`、"待维护付款方式" `:447`、
   包装/检验条款空串 `:466-468`、商品档案整条缺失被 `erp_price_mode` 绕过 `:405-412`）；
   数据链路 `number()/integer()` 静默归零（`procurement_data.py:33-41`）；预测 `predict()`
   三条文档硬约束零校验（`forecast/service.py:175-181`）。
3. **「唯一实现」承诺失守**。四波催办口径实际两份实现且混合行结果相反（前端
   `ledger/model.ts:86-98` 整单优先 vs 后端 `delivery_reminders.py:72-99` 逐行回退，
   后端与 README 一致、是对的一方）；payload 宽度校验挡不住等宽列顺序调换；
   `waveOfDays` 是 `classify` 的手抄副本无测试守护。
4. **可用性单点**。`source_cache` 全局锁内做整年度查库（`app.py:150-187`，库不可达时全服务
   串行等 90 秒）；HTTP 连接无任何超时（半截请求永久占线程）；两处异常穿透 `do_GET`
   回空响应；`soffice` 无超时。这四条叠加是「一个慢查询 / 一个坏请求拖死整个服务」的现实路径。
5. **静默失败面**。催办调度线程遇 SQLite 异常直接死亡且不写 `lastError`、巡检不查
   `running`（`reminders.py:162-176`）——每日推送会静默永久停摆；`/api/health` 任一子系统
   `status()` 抛异常则整个接口无响应（`app.py:777-791`）；`DINGTALK_ENABLED=false`
   实际关不掉发送（只管 Stream）。
6. **确认流完整性缺口**（Agent 侧唯一 P0）。pending_action 落库的是模型给的**完整**入参，
   员工看到的 preview 是另一函数渲染的**子集**，无一致性约束——`generate_purchase_contract`
   的 `price_overrides` 既不在 schema 也不在 preview，**员工确认的合同和实际生成的可以不同**
   （`tools.py:612,889-905,575-601`）。配套缺口：schema 服务端零校验、L3 反而绕过确认、
   钉钉渠道不查绑定表、`cancel` 空署名绕过发起人校验、无 `confirmed_by` 留痕。

### 0.3 实测站得住的部分（不要顺手重构）

- 并发确认防护真实有效：5 线程并发确认只执行 1 次，重复确认回放结果（`BEGIN IMMEDIATE`）。
- SQLite 多线程 16×6 压测零 `database is locked`（WAL + RLock + 短连接）。
- 静态目录穿越防护、token 常量时间比对、鉴权先于读 body 的顺序，全部实测扛住。
- 催办幂等三件套（成功才占键 / 失败可重试 / 不锁死当天）及其测试是全仓质量最高的一组。
- 镜像 upsert 事务边界、水位失败不推进、SSRF 首跳黑名单、payload 四表逐列契约全部核对一致。

### 0.4 文档失准清单（低成本高价值，全部并入 R2-文档批次）

CLAUDE.md：`/api/exchange/*` 无鉴权的说法已过时（实际有双 token）；`DINGTALK_ENABLED`
管"发送"的表述与代码不符；四波口径"只有一份实现"不成立；`procurement_data.py` 行号漂移；
health_watch "不要 import backend.app" 理由需改写。README：三处仍描述已下线的快照降级；
重试次数措辞差一次。AGENTS.md：测试清单比 CLAUDE.md 少 3 个模块。`.env.example`：
4 个死键、缺 `HEALTH_WATCH_URL`。`requirements.txt` 的 PyMySQL 版本声明与实装（2.2.8）脱节。

---

## 1. 整改路线

### 1.1 决策点（动手前先拍板）

2026-08-13 按推荐项拍板，后续按此执行（决策点 2 的代码改动排在 R1，本批只改文档口径）。

| # | 事项 | 结论 | 说明 |
|---|---|---|---|
| 1 | 合同数量出现小数（`qty DECIMAL(18,4)`）时怎么办 | **已拍板：按 Decimal 原值写入单元格**（`=N*L` 公式自然正确） | 禁止 `int()` 静默截断；负数拒绝。若日后业务确认采购数量必为整数，再改为非整数抛 ValueError |
| 2 | `DINGTALK_ENABLED` 语义 | **已拍板：改成总闸**（R1 落地）：`build_dingtalk` 读它，false 时 sender 视为未配置 | 运维直觉是"关掉钉钉"，语义跟直觉走。本批 R0 不改发送代码 |
| 3 | 无鉴权合同接口 | **已拍板：维持 v2 决策（迁移时反代 + ACL），本批先做资源加固** | 缓存键修复（R0-3）、soffice 超时+信号量、outputs 清理、异常收口（后两项在 R1） |
| 4 | 品控登记要不要人工确认 | **已拍板：正则直登不确认（同"绑定"先例）+ LLM 抽取路径给 preview 确认（L1）** | 见 §2.2。登记只写本地台账、可撤销；模型抽取字段可能错，走确认 |
| 5 | 品控日报发送形态 | **已拍板：企业机器人真文件进群**；webhook-only 时降级为摘要+签名内网下载链接 | 见 §2.5 |

### 1.2 R0 立即修（1~1.5 天，数据正确性与生产静默故障）

每项都要带回归用例；不改口径、不动接口形状。

2026-08-13 R0 已落地（离线回归见各模块用例）。

| # | 修什么 | 位置 | 要点 |
|---|---|---|---|
| R0-1 | 合同数量 `int()` 截断少算金额 | `contracts.py` `parse_quantity`、`contract_workbook.py` | **已落地**：按决策点 1 写入 Decimal 原值；数量格式 `#,##0.####`；qty=100.6 离线用例 |
| R0-2 | preview ≠ 执行入参 | `runner.py`、`tools.py` `declared_arguments` | **已落地**：建 pending_action 前按 schema 白名单过滤；`generate_purchase_contract` 不再转发 `price_overrides`；preview 强制含 `arguments` |
| R0-3 | 缓存键 OOM | `app.py` `resolve_source_year` / `trim_source_cache` | **已落地**：键用解析后年份；最多 3 个年份，先逐出过期再逐出最旧 |
| R0-4 | 催办调度线程死亡 | `dingtalk/reminders.py`、`health_watch.py` | **已落地**：`_loop`/`tick` 包 try；`status.enabled`；`reminder_dead` 告警；线程存活用例 |
| R0-5 | 四波口径前端对齐后端 | `ledger/model.ts` `earliestDueDate` | **已落地**：逐行 `deliveryDate \|\| eta` 再取最小；`tests/fixtures/delivery_waves.json` 由 Python 与 `scripts/check_delivery_waves.mjs` 共同断言 |

### 1.3 R1 第一批（约一周，输入校验 / 身份 / 可用性）

**合同与输入校验**

- 单价覆盖校验：`isinstance` + `>0` + `math.isfinite`，非 dict 的 `priceOverrides` 拒绝
  （`contracts.py:386-436`）；税率加 `isfinite`，`json.loads` 传 `parse_constant` 拒 NaN/Infinity
  （全部 `read_json_body` 统一加，`app.py`）。
- fail-fast 补齐四处兜底：`unit`、`payment_method`、buyer 必填字段缺失抛 ValueError；
  `erp_price_mode` 命中取 ERP 价前先断言 `product` 非空（`contracts.py:405-447,466-468`）。
- `invoice_type` 在拼路径前就校验 `in INVOICE_LABELS`；`generate_contract` 断言输出路径
  位于 `ROOT/outputs` 下（沿用 `resolve_asset` 的包含性检查范式）。
- `po_id` 三处 `isdigit()` 换 `isascii() and isdecimal()`。

**可用性**

- `soffice` / `pdftoppm` 加 `timeout=`（建议 120s/30s）、每请求独立
  `-env:UserInstallation=file://<临时目录>`、预览接口加 `BoundedSemaphore(2)`。
- `Handler.timeout = 30`；两处异常穿透收口（图片任务路由映射 `exc.status`、
  `resolve_asset` 对 `ValueError/OSError` 返回 None）。
- `source_cache` 改按 `cache_key` 分键锁（或锁内登记占位、锁外查库），不同年度互不阻塞。
- `outputs/generated/`、`outputs/agent/` 加按 mtime 的每日清理（保留 30 天，走 §3 的维护线程）。

**身份与确认流**

- `needs_confirm` 改 `risk != "L0"`（堵 L3 绕过）；`register()` 强制 L1/L2 必须有 preview。
- 钉钉渠道 L1/L2 要求 `get_by_dingtalk_user_id(sender_id)` 命中，未绑定回复引导绑定；
  确认/取消的发起人比对用 `sender_id` 不用显示名。
- `cancel` 与 `execute` 用同一套校验（拒空署名 + `_web_staff_allowed`），修 `actions.py:154`
  的 `and operator and` 短路；`pending_actions` 加 `confirmed_by` 列并落审计。
- 花名互认收窄：单独括号内花名不作为独立匹配键（`staff_names.py:31-45`）。
- 工具入参轻量校验器（type / required / enum / additionalProperties，不引 jsonschema），
  失败作为 `ToolError` 回模型重填；单轮 tool_calls 数量上限（建议 5）。
- 悬空 `tool_calls` 配对校验进 `history()`（与 §3-S1 一起做）。

**数据与前端**

- SSRF 重定向绕过：图片下载装自定义 `HTTPRedirectHandler` 对每跳跑
  `blocked_image_url(resolve=True)`，或直接禁跟随（`realtime_mirror.py:1153-1156`）。
- `fetch_realtime_years` 与明细窗口同一上界；截断行数进 `meta.warning`（`database.py:211-226`）。
- `crypto.randomUUID` 降级提为 `lib/` 共用（`ExchangePage.tsx:235`）；前端年度改读
  `meta.selectedYear`；尺码曲线补字母码模式（固定码段序）。
- `/api/health`：六个 `status()` 各包 try/except 回错误类型名；`lastError` 只回类型；
  去掉绝对路径字段；探活结果缓存数秒。

### 1.4 R2 第二批（结构与文档，可穿插）

- 换货 `targetStyle` 服务端从 `realtime_products` 查真实 `i_id` 比对，不信客户端自报
  （`exchange/policy.py:60-66`）——涉及 worker 协议，单独排期。
- `standard_no_compact` 写入改存紧凑值并回填存量；`lookup_standard_by_no` 加确定性
  ORDER BY（`gb_standards.py:1035,334-341`）。
- payload 契约从"宽度"升级为"有序列名数组"双向断言，堵列换序。
- 台账 CSV 导出：公式注入前缀防护、`\r` 引号、单号按文本导出（`ledger/csv.ts:12-15`）。
- forecast 五处 `date.today()` 换 `business_today()`；表名过 `IDENTIFIER` 校验；
  `predict()` 返回值排序与 p10≤p50≤p90 校验，违约抛 `ForecastError`。
- LLM 调用对 429/5xx 加一次退避重试；错误文案脱敏（细节进日志）；工具结果截断改成
  合法 JSON 信封（并入 §3-S2）。
- Agent 库保留策略（并入 §3-S1 维护线程）；`LOG_FILE` 换 `RotatingFileHandler`。
- 文档批次：§0.4 清单一次清完；决策点 2 的结论同步 CLAUDE.md / `.env.example`。

### 1.5 与 v2 的衔接

v2 未完项继续有效，不在本文重复：P0-1 预览去缓存验收（迁移时）、P0-5 三链路真人验收、
P1-2 任务超时回收、P1-3 五页一致性、P1-4 剩余项（其中"cancel 校验发起人"已升级进 R1）、
迁移清单与 P2（Agent 库迁 MySQL、合同历史、供应商绩效）。审查证明 v2 已落地项
（催办幂等、Stream 自愈、鉴权矩阵、payload 契约）质量过硬，路线不变。

---

## 2. 品控问题台账（钉钉机器人新能力）

### 2.1 需求与边界

采购群里有人提到品控问题（某供应商来货开胶、色差、少件…）→ 机器人记下来 →
**当天定时把问题整理成 Excel 发回群里**。补充能力：随时口头查询、撤销、关闭。

边界（沿用架构方案 §5.1「工作流优先」）：

- **登记和日报是确定性工作流，不经 LLM**；只有"自然语言一句话登记"这一条辅路径经模型抽取字段。
- 品控台账是**独立新表**，不碰镜像库、不写 ERP。ERP 上游本有质检字段（`qc_qty` /
  `item_is_quality_inspection` 等，见《聚水潭数据接口记录》），当前未进镜像——留作 P2
  对接口（把台账记录与 ERP 质检数量互相印证），第一期不做。
- 钉钉群聊里机器人**只收得到 @ 它的消息**（平台侧行为，代码对收到的每条回调都处理，
  `stream.py:201-214`）。所以"记录群聊中提到的品控问题"落地为：**员工 @ 机器人说品控问题**
  即登记；不做全群旁听（拿不到消息，也不该拿）。

### 2.2 采集：两条登记路径

**主路径：正则前置直登（不经 LLM、不需确认——与「绑定」同先例，`stream.py:255` 分流处加一条）**

```text
@机器人 品控 佰特 604264 鞋垫开胶 3 双            → 登记，回显编号
@机器人 品控关闭 a1b2c3 已与供应商确认补发          → 关闭并记处理备注
@机器人 撤销品控 a1b2c3                            → 撤销（误登记）
@机器人 品控查询 [今天|本周|供应商名]               → 文本摘要（复用日报的汇总函数）
```

确定性解析规则（解析不出就留空，**不猜**）：`\d{6,}` 且能命中采购单表 → `po_id`；
`[A-Z]{1,4}\d{5,}[A-Z0-9-]*` 形态 → `sku`；首词命中本机供应商管理表的简称
→ `supplier`；其余全部进 `description`。回显把解析结果亮给员工：
「已登记品控 #a1b2c3：供应商=佰特 单号=604264 描述=鞋垫开胶 3 双。有误可"撤销品控 a1b2c3"」。

**辅路径：L1 工具 `record_quality_issue`（自然语言进 Agent 工具循环时）**

员工不带"品控"前缀、在对话里顺口说"帮我记一下昨天佳裕来的货有色差"时，模型调用该工具。
因字段是模型抽取的，风险级 L1：preview 展示将登记的全部字段，员工"确认 编号"后落库。
schema 声明 `description`（必填）、`supplier` / `po_id` / `sku` / `severity`（可选，
`additionalProperties: false`，配合 R0-2 的白名单过滤）。

网页 `/chat` 走同一工具；不给品控做第六个页面（与架构方案「不新开页面」一致）。

### 2.3 数据模型（`AGENT_DATABASE_PATH` 的 SQLite，随 store 一起迁 MySQL）

```sql
CREATE TABLE IF NOT EXISTS quality_issues (
    id TEXT PRIMARY KEY,                       -- token_hex(3)，6 位好念好回复
    report_date TEXT NOT NULL,                 -- 业务日 YYYY-MM-DD（business_today）
    source_channel TEXT NOT NULL,              -- dingtalk / web
    conversation_id TEXT NOT NULL DEFAULT '',
    message_id TEXT UNIQUE,                    -- 钉钉消息 ID，入站幂等（断线重连不重复登记）
    reporter TEXT NOT NULL DEFAULT '',         -- 绑定后的采购员姓名，未绑定存显示名
    reporter_user_id TEXT NOT NULL DEFAULT '',
    supplier TEXT NOT NULL DEFAULT '',
    po_id TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',         -- 一般 / 严重（可空）
    description TEXT NOT NULL,                 -- 必填，唯一不允许为空的业务字段
    raw_text TEXT NOT NULL DEFAULT '',         -- 原话，供日报核对
    status TEXT NOT NULL DEFAULT 'open',       -- open / resolved / cancelled
    resolution TEXT NOT NULL DEFAULT '',
    run_id TEXT,                               -- 走工具路径时关联 agent_runs
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_issues_date ON quality_issues(report_date, status);
```

结构化字段全部可空是**有意的**：台账的价值在"记下来了"，供应商/单号补不上不阻塞登记
（与合同的 fail-fast 不同——那是法律文书，这是工作台账，口径写进 README）。

### 2.4 每日汇总与 Excel

新包 `backend/quality/`：

```text
backend/quality/
  __init__.py     # build_quality(setting, store, sender, directory) 装配
  service.py      # QualityLedger：record / cancel / resolve / query / summary
  report.py       # build_quality_workbook(issues, path)（openpyxl，先例 contract_workbook）
  scheduler.py    # DailyQualityReportScheduler（照抄催办调度骨架，含 R0-4 的抗错修正）
```

- 调度：`_loop` 30 秒轮询 + `tick` 到点判断 + 幂等键 `quality-report-{date}` +
  当日失败最多 3 次 / 间隔 15 分钟——骨架与 `DailyReminderScheduler` 相同
  （`reminders.py:103-186`），**必须带上 R0-4 修好的抗错写法**。默认 `17:30` 收盘汇总
  （催办是早 08:30，两者错开）。
- 内容：当日 `report_date = today` 的全部记录（含已关闭，撤销的不进）；工作簿单 sheet：
  `编号 / 登记时间 / 登记人 / 供应商 / 采购单号 / SKU / 严重度 / 问题描述 / 状态 / 处理备注`，
  末尾统计行（按供应商计数、open 计数）。样式从简，不做合同级排版；长单号列设文本格式
  `@`（避开科学计数法，教训来自台账 CSV 的审查发现）。
- 当日零记录：默认不发（`QUALITY_REPORT_EMPTY=skip`，可配 `notice` 发一句"今日无品控登记"）。
- 产物落 `outputs/quality/品控台账-YYYYMMDD.xlsx`，与合同产物同款保留策略（30 天清理）。
- 历史未关闭问题不进当日 sheet（避免天天重复刷同一批），改为 markdown 摘要里带一句
  "另有 N 条历史问题未关闭，回复「品控查询 未关闭」查看"。

### 2.5 发送通道（本功能唯一的硬技术点）

**现状：`DingTalkSender` 只有 `sampleMarkdown` / `sampleText` / `sampleActionCard6`
三种 msgKey（`sender.py:152,170,184`），发不了文件；webhook 机器人平台层面就不支持文件。**

按决策点 5 分两档：

1. **企业内部应用机器人（推荐，已是催办/对话的主通道）**：给 `DingTalkSender` 补两个方法——
   - `upload_media(path, filetype="file")`：POST `oapi.dingtalk.com/media/upload`
     （multipart，标准库手拼 boundary，约 30 行；已装的 `dingtalk-stream==0.24.3` 里
     `upload_to_dingtalk` 就是同一接口的现成参考实现），返回 `media_id`；
   - `send_file(conversation_id, media_id, file_name, file_type)`：走既有
     `groupMessages/send`，`msgKey="sampleFile"`，
     `msgParam={"mediaId":…, "fileName":"品控台账-20260813.xlsx", "fileType":"xlsx"}`。
   日报 = 一条 markdown 摘要（条数、按供应商计数、@ 相关采购员）+ 一条真文件。
2. **仅配置了 webhook 时的降级**：markdown 摘要 + 内网下载链接。链接用**能力 URL**而非
   Bearer（群成员点击无法带头）：`GET /api/quality/reports/{YYYYMMDD}/{sig}.xlsx`，
   `sig = HMAC-SHA256(QUALITY_REPORT_LINK_SECRET, date)[:16]`，服务端重算比对 +
   仅放行 7 天内日期。符合架构方案 §8「文件只通过有时效的下载地址发送」。局域网 http
   环境链接可直接点开。

发送成功才写幂等键（沿用催办的"成功后占键"审计语义，`audit.record_delivery`）。

### 2.6 工具与接口注册（全部走注册表，不改 Agent Core）

| 名称 | 风险级 | 说明 |
|---|---|---|
| `record_quality_issue` | L1 | 模型抽取字段登记，preview 展示全部将落库字段 |
| `list_quality_issues` | L0 | 按日期 / 供应商 / 状态查询，走 §5.2 信封（summary + 截断明细） |
| `push_quality_report` | L2 | 手动触发当日日报（对外发群），preview 含条数与目标群 |

HTTP：`POST /api/agent/quality/report`（Bearer，手动补发）、能力 URL 下载路由（无 Bearer，见上）。
正则指令（品控 / 品控关闭 / 撤销品控 / 品控查询）在 `stream.py` 前置分流，位于幂等去重之后、
`runner.chat` 之前。

### 2.7 配置项（`.env.example` 同步）

```bash
QUALITY_LEDGER_ENABLED=false        # 登记指令与工具总开关
QUALITY_REPORT_ENABLED=false        # 每日日报单独开关（同催办的拆分逻辑）
QUALITY_REPORT_TIME=17:30
QUALITY_REPORT_EMPTY=skip           # skip | notice
QUALITY_REPORT_LINK_SECRET=         # 降级链接签名密钥；留空则禁用降级链接
```

### 2.8 测试与验收

- `tests/test_quality.py`：正则解析（单号/SKU/供应商命中与不猜）、幂等（同 message_id
  重复回调只登记一次）、状态机（open→resolved / cancelled 不进日报）、日报聚合、
  openpyxl 读回断言列头与文本格式、调度器抗错与幂等键、空日 skip。
- 发送侧用假 sender（催办测试同款 mock 深度）；`upload_media` 的 multipart 拼装
  离线断言请求体结构。
- 真人验收：群里 @ 机器人登记 2 条 → 17:30 收到摘要 + Excel → 打开核对 → 关闭 1 条 →
  次日日报不再包含。
- README 增补口径章节：字段可空的口径、日报范围（当日登记、撤销不进、历史未关闭只报计数）。

### 2.9 实施步骤与工作量（合计约 2.5 天）

1. 表 + `QualityLedger` + 正则指令 + 撤销/关闭/查询（0.5 天）
2. `report.py` Excel + 汇总函数（0.5 天）
3. sender 文件能力 + 能力 URL 降级（0.5 天）
4. 调度器 + 三个工具注册 + 配置（0.5 天）
5. 测试 + 真人验收 + README 口径（0.5 天）

前置依赖：R0-4（调度骨架抗错）先行；R0-2（schema 白名单）让 L1 工具的 preview 可信。

---

## 3. Agent 上下文压缩 · 记忆 · 会话管理

### 3.1 现状（全部为实测事实）

- 上下文组装只有两行：system prompt + `history()` 最近 **20 条消息**（`runner.py:130-131`，
  `AGENT_HISTORY_LIMIT`）。20 是**消息条数**不是轮数——一轮带 3 个工具调用吃掉 7 条，
  实际记忆常常只有两三轮。
- **无任何压缩 / token 预算 / 重试**：工具结果 `json.dumps(...)[:20000]` 硬截断（截完不是
  合法 JSON），请求不带 `max_tokens`，`usage` 取了不存；实测单次 `chat()` 可发出 562KB，
  次轮开场即 159KB——费用放大与 502 超限的现实来源（审查 agent-core P2）。
- **会话永不过期、永不清理**：web 按浏览器标签页一会话（sessionStorage），钉钉按
  `conversation_id:sender_id` 一人一会话且**没有任何重置指令**，一旦建立永久累积。
  唯一清理是 web 的 reset（DELETE 消息）。
- 悬空 `assistant.tool_calls`（写入中途崩溃）会让该会话永久 400，只能 reset（审查 P2）。
- **没有任何记忆机制**；架构方案 §5.2 的工具结果信封已设计未实现。

### 3.2 设计原则

1. **不引框架、不上向量库**（对照架构 §2.1 "不做 RAG"）：压缩 = 预算 + 分层 + 摘要，
   记忆 = 结构化小表 + 关键词注入，全部标准库。
2. **数字不进记忆**。记忆只存偏好、指代、未完成事项（"利特负责佰特和佳裕"、"上次在办
   604264 的专票合同"）；采购数字必须每次走工具重查——否则模型会引用过期数字，
   直接违反「工具返回的数字不要再算一遍」的第一戒律。摘要同理（见 S2 约束）。
3. **审计全量保留，上下文分层瘦身**。`agent_messages` 落库的永远是完整内容；
   压缩只发生在组装 `messages` 时。事后追查不受影响。
4. 分三步落地（S1 会话 → S2 压缩 → S3 记忆），每步独立可用、可单独关闭。

### 3.3 S1 会话管理（0.5~1 天）

**epoch 机制**：`agent_sessions`、`agent_messages` 各加 `epoch INTEGER NOT NULL DEFAULT 0`
（SQLite `executescript` 不会补列，`AgentStore.__init__` 加一步 `PRAGMA table_info`
迁移检查）。"新会话"= `epoch += 1`，**不删消息**；`history()` 只取当前 epoch，
`transcript()` 页面仍可跨 epoch 展示全史。

- **空闲自动翻篇**：`ensure()` 里 `updated_at` 距今超过 `AGENT_SESSION_IDLE_MINUTES`
  （默认 120）即 `epoch += 1`。钉钉场景收益最大：昨天问合同、今天问催办，本就不该共享上下文。
- **显式指令**：钉钉正则加「新话题|重置会话」→ epoch+1，回复"已开新话题，历史在网页端可查"。
  web 的"清空会话"按钮改调同一语义（保留审计）。
- **悬空 tool_calls 修复**：`history()` 返回前配对校验，丢弃无 `tool` 回复的
  `assistant.tool_calls` 消息（修审查 P2 的会话卡死）。
- **保留策略**：新增每日维护 tick（挂独立低频线程，骨架同调度器）：删 `AGENT_RETENTION_DAYS`
  （默认 90）天前的 `agent_messages` / `agent_runs` / `tool_executions` /
  `notification_deliveries`，`forecast_runs` 只留 30 天的 `output_json`（置空列不删行）；
  顺带清 `outputs/generated|agent|quality` 30 天前文件（R1 的清理落点在这里）。

### 3.4 S2 上下文预算与压缩（1~1.5 天）

**第一层：字符预算取代固定条数。** `AGENT_CONTEXT_CHAR_BUDGET`（默认 60000 字符，
约对应 DeepSeek 64k 窗口的三分之一，给回复和工具留余量）。组装从最新往旧累计：

```text
1. system prompt（固定开销）
2. [S3] 操作员记忆段 ≤ 500 字
3. [若有] 最近一条会话摘要（见下）
4. 消息从新到旧装入，直到预算耗尽：
   - user / assistant 全文；
   - tool 消息分级：本轮（最近一次 user 之后）的全文保留；
     更早轮次的替换为 {"summary": 首 300 字, "truncated": true,
     "note": "历史工具结果已压缩，需要请重新调用工具"}；
   - 停点沿用现有规则回退到最近一条 user 起头 + S1 的配对校验。
```

**第二层：工具结果信封（架构 §5.2 落地 + 审查 P3 修正）。** `runner.py:189` 的
`[:20000]` 改为合法结构：超限时 `{"truncated": true, "preview": 前 N 字,
"hint": "结果过长已截断，请用更精确的参数重查"}`；同时把单条上限从 20000 降到 8000
（与 `history_limit` × 预算相称；全量数据本就该走页面，`AGENT_ARCHITECTURE.md:180-195`）。

**第三层：滚动摘要（唯一用 LLM 的环节，可独立关闭 `AGENT_SUMMARY_ENABLED`）。**
当前 epoch 消息数超过 `AGENT_SUMMARY_TRIGGER_MESSAGES`（默认 40）时，`chat()` 收尾后
把最老的一段（保留最近 20 条不动）交给同一 LLM 压成 ≤800 字，写入：

```sql
CREATE TABLE IF NOT EXISTS agent_session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    upto_message_id INTEGER NOT NULL,   -- 覆盖到哪条消息，增量续写
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

摘要 prompt 硬约束：只记**单号、SKU、供应商、已确认的结论、未完成事项**；
**禁止记录金额、数量、日期等易过期数字**（防陈旧数字被当作事实引用）；失败只记日志、
不影响本轮回复（下轮退回硬截断）。`history()` 遇到摘要覆盖范围内的消息直接跳过。

**配套**：`agent_runs` 加 `prompt_tokens` / `completion_tokens` 列，把 `llm.py:121,187`
已取出的 `usage` 落库——预算参数靠真实数据调，不靠拍脑袋。

### 3.5 S3 记忆管理（1 天，`AGENT_MEMORY_ENABLED` 默认关）

只做**操作员记忆**一层。不做全局业务记忆（业务口径属于 README 和工具，不该进自由文本）。

```sql
CREATE TABLE IF NOT EXISTS operator_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator TEXT NOT NULL,             -- 绑定后的采购员姓名
    kind TEXT NOT NULL DEFAULT 'preference',   -- preference / context
    content TEXT NOT NULL,              -- ≤120 字一条
    source TEXT NOT NULL DEFAULT '',    -- explicit（员工说"记住"）/ summary（摘要提炼）
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_memories ON operator_memories(operator, active, updated_at);
```

- **写入**：① 显式——钉钉/网页正则「记住 …」直登（同"绑定"先例，回显 + 「忘记 …」可删）；
  ② 摘要提炼——S2 摘要生成时顺带输出 0~2 条候选偏好，`source='summary'`，同员工同内容去重。
  每员工上限 20 条，超出按 `updated_at` 逐出。
- **注入**：`system_prompt()` 加一段「关于当前员工的已知信息（可说"忘记 xx"修改）：…」，
  取该 operator 最近 5 条 active，总长 ≤500 字。**只在 operator 已绑定时注入**
  （匿名显示名不积累记忆，也堵住用记忆做注入的口子——记忆内容进 prompt 前过
  R1 的字符白名单）。
- **修正**：「忘记 <关键词>」按 LIKE 匹配置 `active=0` 并回显删了哪几条。

### 3.6 配置项汇总（`.env.example` 同步，全部有默认值可不配）

```bash
AGENT_SESSION_IDLE_MINUTES=120      # S1 空闲翻篇；0 = 关闭
AGENT_RETENTION_DAYS=90             # S1 审计保留
AGENT_CONTEXT_CHAR_BUDGET=60000     # S2 上下文字符预算
AGENT_TOOL_RESULT_LIMIT=8000        # S2 单条工具结果上限（原硬编码 20000）
AGENT_SUMMARY_ENABLED=false         # S2 滚动摘要
AGENT_SUMMARY_TRIGGER_MESSAGES=40
AGENT_MEMORY_ENABLED=false          # S3 操作员记忆
```

### 3.7 测试

`tests/test_agent.py` 补：epoch 翻篇（空闲/指令/history 只取当前 epoch）、悬空 tool_calls
被剔除、预算裁剪（构造超长历史断言旧 tool 消息被换成压缩结构、user/assistant 保留）、
信封合法 JSON、摘要触发与增量续写（假 LLM 回放）、记忆写入/注入/忘记/未绑定不注入、
保留策略删旧留新。黄金回放集加一条"跨 epoch 不残留上文指代"的用例。

### 3.8 落地顺序

S1（会话 + 保留 + 悬空修复）→ S2 预算与信封 → S2 摘要（可后置观察）→ S3 记忆。
S1/S2 不依赖模型行为，先行；S3 等品控与 R1 身份校验落地后再开（记忆注入依赖"绑定"身份）。

---

## 4. docs 目录归纳

### 4.1 文档清单与定位（本次整理后）

| 文档 | 定位 | 状态 |
|---|---|---|
| `README.md`（仓库根） | 全部业务口径的唯一权威 | 有 3 处快照降级残留待删（R2-文档） |
| `docs/PROJECT_DESIGN.md` | 第一阶段数据链路设计 | 与现状一致，保留 |
| `docs/AGENT_ARCHITECTURE.md` | Agent 总架构与路线 | 现行；§10 与实现差异已在文内声明；§5.2 信封由本文 §3-S2 落地 |
| `docs/MVP完善方案.md` | v2 整改方案 | 大部分已落地，未完项由本文 §1.5 接管；**归档态，不再往里加内容** |
| `docs/完善方案v3-整改与Agent演进.md` | **本文，当前执行文档** | 现行 |
| `docs/预测模型接入.md` | Forecaster 接入指南 | 与代码高度一致，保留 |
| `docs/商品资料与图片入库.md` | 商品主数据现状 + 未来 BLOB 入库设计参考 | 保留 |
| `docs/聚水潭数据接口记录.md` | ERP 上游接口参考（含品控可用的 `qc_*` 字段） | 外部参考，保留 |
| `docs/供应链项目-API调用说明(1).md` | 供应链代理 API 使用说明 | 外部参考，保留；文件名里的"(1)"是下载产物痕迹，建议改名去掉（无任何引用，改名安全） |
| `docs/assets/` | 截图素材（实时库字段等） | 保留 |
| `docs/api.txt` | **一行疑似 Client Secret 的明文凭证** | ⚠️ 该值不在 `.env` / `hanli.env` 中、全仓无引用。若已作废请直接删除；若仍有效，立即移入 `.env`（`SUPPLY_API_*` 体系）并删除此文件——凭证不进 docs，与 CLAUDE.md「不要提交凭证」一致 |

### 4.2 整理动作

1. 新增 `docs/README.md` 索引：阅读顺序（README 口径 → PROJECT_DESIGN →
   AGENT_ARCHITECTURE → 本文）+ 上表。
2. 本文落位 `docs/完善方案v3-整改与Agent演进.md`。
3. `docs/api.txt` 按上表处置（**需人工确认凭证是否作废，本次不代删**）。
4. CLAUDE.md 开头"文档指引"一句建议补上本文与 `docs/README.md`（属 R2-文档批次，
   与 §0.4 的失准修正一并提交）。

---

## 5. 总执行顺序

| 序 | 内容 | 规模 | 前置 |
|---|---|---|---|
| 1 | §1.1 五个决策点拍板 | 半小时 | — |
| 2 | R0-1~R0-5 | 1~1.5 天 | 决策点 1 |
| 3 | 品控台账 v1（§2） | 2.5 天 | R0-2 / R0-4；决策点 4、5 |
| 4 | S1 会话管理（§3.3） | 0.5~1 天 | — |
| 5 | R1 第一批 | 约 1 周（可与 3、4 穿插） | 决策点 2、3 |
| 6 | S2 预算与压缩（§3.4） | 1~1.5 天 | S1 |
| 7 | S3 记忆（§3.5） | 1 天 | S1、R1 身份校验 |
| 8 | R2 第二批 + 文档批次（§1.4、§0.4、§4.2-4） | 穿插进行 | — |

品控台账与 S1/S2 互不依赖，可并行；全部落地后按 v2 的迁移清单择机迁专用机器。
