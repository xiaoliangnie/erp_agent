# MVP 优化 / 完善方案（v2，待审核）

> **已归档。** 未完项见 [`docs/开发.md`](../开发.md)，不要再往本文加内容。
>
> 2026-08-13 制定，v2 合并三路深度审查（前端五页 / 后端服务与安全 / 预测·换货·钉钉·测试）的发现。
> 前提约束（已确认）：**不引入 git 流程；暂不做备份**；后续整体移植到专门机器运行。
> **时区口径：系统统一东八区（Asia/Shanghai）**。后端已有 `backend/business_time.py`
> 统一实现；前端所有业务日期一律来自后端 payload（`meta.today` 等），
> 日期运算必须与浏览器/运行机器时区无关（见 P0-4）。

## 0. 现状与审查结论

134 个离线用例全部通过（11 个测试模块）。各子系统状态：

| 子系统 | 状态 | 差距 |
|---|---|---|
| 看板 / 台账 | 实时镜像同步已开，页面可用 | 换年度旧数据不清、图表时区错位（P0-4）；看板预警块是死代码（P1-3） |
| 合同生成 | 票种 / 单价 / 图片 / 执行标准全链路已实现 | 写入器与预览渲染依赖 codex 缓存（P0-1）；切单竞态（P1-3） |
| 国标目录 | 1098 条已入库，钉钉 / 网页可查 | 每日同步开关见 P1-1；合同已选用标准的状态跃迁提醒已落地（P1-6） |
| 换货 | dry-run + 二次确认已实现 | 任务无超时会僵死（P1-2）；缺真实 ERP 验收（P0-5） |
| 采购助手 | Agent 已开（DeepSeek），钉钉 Stream 已连 | Stream 自愈已做（P0-3）；P1-5 黄金回放 / operator 校验 / 主数据缺口 / 台账推送已落地 |
| 钉钉催办推送 | 代码就绪、未开 | **失败后当日永远无法重发的幂等缺陷**（P0-2），修完才能开 |
| 预测 | 接口齐，Baseline 占位 | 销售 / 库存表未进库，MVP 明确不含（P2） |

优先级：P0 = MVP 上线前必须；P1 = 上线后第一周；P2 = MVP 之后。

---

## 决策点（审核时请先拍板这四条）

| # | 事项 | 推荐 | 备选 |
|---|---|---|---|
| 1 | 看板「到货预警」死代码（`OrdersTable.tsx` / `AlertBars.tsx` / `exportAlerts` / `alertData`，均无引用） | **删除**。README 口径已定「预警和完整交期清单统一放台账，避免两页重复」，这批文件是旧版迁移残留 | 接回看板 UI（约 1 天，且与台账重复） |
| 2 | 台账「发送提醒」按钮（现在只弹文字指引，不真发） | **接真推送**：调 `/api/agent/reminders/push` 带 buyer 过滤 + 确认弹窗 + 「当日已推」提示 | 改成明确的「去采购助手发起」跳转按钮 |
| 3 | 合同 / 看板接口无鉴权（`/api/contracts/*`、`/api/dashboard` 等），迁移绑 `0.0.0.0` 后即内网可任意访问 | **迁移时用反向代理 + 内网 ACL 控制**，代码不动（内部工具定位） | 代码加一层页面 token（改动面大：前端五页 + 后端路由） |
| 4 | 换货「部分成功整单标 failed，已改 ERP 行无补偿」 | **先只做明细级结果展示**（页面能看清哪行成功哪行失败），不做自动补偿 | 自动反向补偿（风险高，不建议） |

---

## P0（MVP 上线前必须）

### P0-1 合同生成链路去 codex 化（约 1 天）

**问题**：链路两处依赖 `~/.cache/codex-runtimes/` 私有产物，一次缓存清理即整体瘫痪，
且无法在专用机器复现：

| 依赖 | 现指向 | 用途 |
|---|---|---|
| `@oai/artifact-tool`（Node ESM） | `CONTRACT_ARTIFACT_TOOL_PATH=~/.cache/codex-runtimes/.../artifact_tool.mjs` | `scripts/generate_contract.mjs` 写 XLSX |
| `soffice` | `CONTRACT_SOFFICE=~/.cache/codex-runtimes/.../bin/override/soffice` | 预览 XLSX → PDF（本机没有独立 LibreOffice） |

`pdftoppm` 来自 Homebrew poppler，不受影响。

**改法**：

1. XLSX 写入改用 Python **openpyxl**（新增依赖 `openpyxl>=3.1,<4`），彻底去掉 Node 运行期：
   - 新模块 `backend/contract_workbook.py`：入参为现有合同模型 dict，输出 `.xlsx`。
     `backend/contracts.py::generate_contract` 不再「临时 JSON → node 子进程」，
     改为进程内调用；模型结构、校验、`contract_line_gb` 持久化全部不动。
   - 选型：openpyxl 支持公式、合并区、列宽行高、边框字体、数字格式、锚定图片
     （`OneCellAnchor` + 像素偏移），且**能读回**——离线测试直接打开生成的工作簿断言布局。
     xlsxwriter 只写不读、exceljs 仍留 Node，均不取。
   - **布局契约不变**：明细 16 列 A–P、`itemStart = 9`、小计 `=N{row}*L{row}`、
     合计 `=SUM(O…)`、合并区与行高按 `generate_contract.mjs` 现行为一比一迁移。
     国标码（E 列条码）从 `=TEXT(…,"0")` 公式改为直接写文本 + 数字格式 `@`
     （显示与语义等价）；品名在 F 列，执行标准在 G 列，仍为文本。
   - 完成后删除 `scripts/generate_contract.mjs`；`.env` / `.env.example` 去掉
     `CONTRACT_NODE`、`CONTRACT_ARTIFACT_TOOL_PATH`；README / CLAUDE.md /
     PROJECT_DESIGN.md 同步。
2. 预览保持「真实办公套件」路线（否则嵌入图片不出现），只换二进制来源：
   - 本机 `brew install --cask libreoffice`，`CONTRACT_SOFFICE` 指向
     `/Applications/LibreOffice.app/Contents/MacOS/soffice`；
   - 专用机器（Linux）`apt install libreoffice-calc poppler-utils` + 中文字体（§迁移清单）；
   - `render_contract_preview`（soffice → PDF → pdftoppm → PNG）代码不动。

**验收**：

1. 离线测试用 openpyxl 读回工作簿，断言表头 16 列、E/F 两列内容分离、O 列公式与
   SUM、合并区、税率表头文案（替换 `tests/test_contract_gb.py` 里 grep mjs 源码的
   `ExcelLayoutTests`）。
2. PO 604264 三种票种各生成一次，与旧渲染 PNG 并排目检（字体、行高、图片位置、合计）。
3. 把 `~/.cache/codex-runtimes` 改名后，全链路仍可预览 + 下载。

**进度（2026-08-13）**：XLSX 已切到 `backend/contract_workbook.py`（openpyxl + Pillow 内嵌图片），
`scripts/generate_contract.mjs` 已删除。独立 LibreOffice 本机暂不装，预览继续用现有
`CONTRACT_SOFFICE`；P0-1 预览去缓存验收延后。

### P0-2 钉钉催办幂等修复（约 0.5 天）

**问题**（`backend/dingtalk/reminders.py:68`、`147`；`backend/agent/audit.py:88`）：
`record_delivery(..., status="sending", idempotency_key=daily-reminder-{日期})` 在
**发送前**就占了 UNIQUE 幂等键，且 `last_run = today` 写在 `run_once` 之前。
钉钉接口一旦失败：当日定时不再试；手动 `/api/agent/reminders/push` 也被幂等键
挡住、显示「已推送」。**失败当日不可恢复**，这是催办上线（P1-1）的前置阻塞。

**改法**：

1. 幂等键只在**发送成功后**写入（delivered 记录带 key）；失败写 `status=failed`
   记录**不带**该 key（或带 `-attempt-N` 后缀），不阻挡重试。
2. `last_run` 移到 `run_once` 成功之后；失败记 `last_error`，同日重试加约束防刷群：
   间隔 ≥ 15 分钟、同日最多 3 次，仍失败则等次日并保留错误供 `/api/health` 暴露。
3. 手动 push 的幂等检查只认「当日已成功」记录。

**涉及**：`reminders.py`、`audit.py`（`record_delivery` 拆预登记/成功登记）、
`tests/test_dingtalk.py` 补三条用例（成功后同日幂等、失败后可重试、重试次数上限）。

**验收**：单测通过；手动断网模拟一次失败，恢复后同日推送成功且不重复。

**进度（2026-08-13）**：幂等键只在 `sent` 后占用；失败写 `-attempt-N` 不挡重试；定时线程
15 分钟 / 3 次封顶；手动 push 只认当日已成功。旧的 `sending` 占键记录会在下次推送时释放。

### P0-3 钉钉 Stream 自愈（约 0.5 天）

**问题**（`backend/dingtalk/stream.py:126`）：`_serve` 线程异常退出后永不重启
（`start()` 只在进程启动时调一次），机器人静默失联。

**改法**：`start()` 改为启动**监督循环**线程：`while not stop:` 内起 `_serve`，
异常退出记 `last_error`、指数退避重连（30s → 60s → … 封顶 10 分钟）后重建客户端。
`status()` 增加 `restartCount` / `lastError`；`app.py` 停机钩子补 `DINGTALK_STREAM.stop()`。

**验收**：注入异常杀掉内部连接后 1 分钟内自动恢复；`/api/health` 可见重启计数。

**进度（2026-08-13）**：`start()` 改为监督循环；`_serve` 异常或退出后指数退避重连并重建客户端；
`status()` 含 `restartCount`；`app.py` 停机调用 `DINGTALK_STREAM.stop()`。

### P0-4 前端数据可信度：旧数据 + 时区（约 0.5 天）

**A. 换年度静默展示旧数据**（`frontend/src/hooks/usePayload.ts:25`）：
切年份或请求失败时不清 `data`，页面无 Loading 无报错，员工可能对着旧年份数据做判断。

改法：参数变化时立即清 `data` 进入 Loading；失败清 `data` 显示 LoadFailed +
重试按钮。看板 / 台账两页的「有旧数据就直接渲染」判断同步调整。

**B. 图表日期时区错位**（`frontend/src/pages/dashboard/charts/geometry.ts:34-45`）：
`isoMonday` / `shiftDays` 用本地午夜解析再 `toISOString()`（UTC），机器时区非 UTC
时「近 30/90/180 天」窗口与按周聚合会差一天。

改法：两函数改为 **UTC 锚定纯日历运算**（`new Date(iso + "T00:00:00Z")` +
`getUTCDay` / `setUTCDate` / `toISOString()`），与机器时区完全无关。
已核实：「今天」锚点已来自后端 `meta.today`（东八区业务日），无需改；
`lib/format.ts` 的 `dayIso`（天序数 × 86400000 → UTC）本就正确。
同时在 CLAUDE.md 约定节加一条：**前端业务日期禁止用本地时钟 `new Date()` 运算，
「今天」一律取 payload meta**。

**验收**：机器时区分别设为 UTC-4 与 UTC+8 各跑一次页面，近 30 天窗口、按周聚合、
四波分档结果一致。

**进度（2026-08-13）**：`usePayload` 换参/失败立即清 `data`；看板/台账 loading 优先于旧数据；
`isoMonday` / `shiftDays` 改为 UTC 日历运算。双时区目检取消：运行机器统一 `Asia/Shanghai`，
与业务口径一致；前端「今天」仍只认 payload `meta.today`，不读浏览器时区。

### P0-5 三条链路真人验收（半天，需采购员配合）

1. **合同**：搜单 → 选执行标准（验证「现行 / 即将实施」分组、同 SKU 记忆）→ 预览 →
   下载 → 复开同一单确认选择被带出。
2. **钉钉对话**：群里问「国标库同步了吗」「毛绒小熊用什么国标」「张三有哪些逾期单」，
   发起一次合同生成并走「确认 编号」流程。
3. **换货**：真实 ERP 测试环境 dry-run → 换货页二次确认 → 核对 ERP 结果。

---

## P1（上线后第一周）

### P1-1 打开两个定时任务 + 国标同步失败退避（0.5 天）

- P0-2 修完后 `DINGTALK_REMINDER_ENABLED=true`（每天 08:30 催办进群 @ 采购员）。
- `GB_SYNC_ENABLED=true`（每天 02:30 增量同步目录）。
- 顺带修（`backend/gb_standards.py` 调度循环）：同步失败后现在每 30 秒重打 SAMR
  外站，加指数退避封顶 900 秒（对齐镜像同步的策略）。

**进度（2026-08-13）**：失败退避已落地。本机 `.env` 已打开 `DINGTALK_REMINDER_ENABLED`
与 `GB_SYNC_ENABLED`，**需重启 server.py 才生效**。今天下午重启会立刻推今日催办
（已过 08:30），国标同步若今日水位已成功则跳过。

### P1-2 任务队列可靠性：超时回收（1 天）

**问题**：换货任务 `planning` / `searching` / `reading` 无超时，worker 掉线后任务
僵死且挡住同一订单的后续任务（`backend/exchange/service.py:435`）；图片同步
`syncing` 同病且 `create` 碰到旧任务直接复用（`backend/product_images.py:116`）。

**改法**：

- 领取任务时记 `claimed_at`；查询/领取路径惰性检查：`planning` 类超时（5 分钟）
  退回 `pending` 并计 `attempts`，超过 3 次标 `failed`。
- `executing` 保持**不重投**（防 ERP 双写），但超时（15 分钟）标记 `stuck` 并
  通过钉钉发送告警，人工处置。
- 图片任务同样加超时回收；`create` 碰到超时旧任务允许重建。
- 前端：换货页轮询失败显示「状态可能过时」；worker 离线时顶栏警示。
- 部分成功的任务按【决策点 4】做明细级结果展示。

**涉及**：`exchange/service.py`、`product_images.py`、`ExchangePage.tsx`、
`tests/test_exchange.py` / `test_product_images.py` 补超时用例。

### P1-3 合同页竞态 + 五页一致性 + 死代码（1 天）

- **合同页竞态**（`ContractPage.tsx:73`）：`loadOrder` / `loadChoices` 加
  AbortController（或请求序号比对），后发先至丢弃；`requestBody` 的
  `Number()` 结果校验 `NaN` 后再提交。
- **五页拉齐**（以台账页为基准）：看板搜索加防抖、换年度重置筛选、
  「查看交期台账」链接带 `?year=`；聊天 / 换货两页 token 自动连接策略统一；
  换货「取消任务」和聊天「清空会话」加确认；`chat.css` / `exchange.css`
  硬编码色值改用 `base.css` 令牌。
- **死代码**：按【决策点 1】处理看板预警模块四处文件。
- 换货页两处小修：策略文案改为从 `/api/exchange/policy` 动态渲染（去掉写死的
  「XZ25401308-101 / 14 个目标」）；`policy.py` 的 `lru_cache` 改为带 mtime 检查，
  改 `config/exchange-rules.json` 不再需要重启。

### P1-4 安全清理（0.5 天）

- `secrets.compare_digest` 两侧先做定长哈希（sha256）再比较，长度不等不再抛 500
  （`app.py:319`、`337`）。
- 换货 `cancel` 校验操作人与创建人一致（`exchange/service.py:523`）。
- `actions.py:125`：`operator` 为空时**拒绝确认**（现在是跳过校验）。
- 镜像图片下载加内网地址黑名单（127.0.0.0/8、10.0.0.0/8、172.16/12、192.168/16、
  169.254/16、localhost），防供应链 API 被污染时的 SSRF（`realtime_mirror.py:281`）。
- 无鉴权接口按【决策点 3】在迁移时落地。

### P1-5 对话硬化 + 台账按钮接线（已落地）

1. **黄金回放集**：`tests/fixtures/golden_dialogues.json`（22 条），CI 假 LLM 回放。
2. **operator 校验**：网页 `/chat` 与台账推送的 `operator` 对 `staff_bindings` 校验，
   对不上时 L1/L2 拒绝（只读不拦）；钉钉渠道按 userId，不走这道。
3. **`master_data_gaps` L0 工具**：供应商未维护 / 近 N 天 SKU 无图 / 票种缺价 /
   分类未映射国标；markdown 对齐催办模板。
4. **台账「发送提醒」**接 `/api/agent/reminders/push`（采购员 + 档位 + 确认弹窗 + 当日已推）。

### P1-6 国标变更提醒（已落地）

每日 GB 同步成功后 diff：本轮 `status` 跃迁（即将实施 → 现行、任意 → 废止），且
`samr_id` / `standard_no` 出现在 `contract_line_gb` 的，推钉钉 markdown
（`gb-contract-change-{today}-{samr_id}` 幂等）。合同页候选项加状态角标。
离线用例覆盖跃迁过滤、只用合同已选用的标准、同日不重发。

### P1-7 日志与轻监控（已落地）

- 后端 `print` 换成模块级 `logging`；`configure_logging` 在 `app.main` 里挂 stderr + 可选
  `LOG_FILE`（默认 `data/app.log`），时间固定东八区。CLI 脚本仍用 `print` 给人看。
- `scripts/health_watch.py`：cron / launchd 每 5 分钟拉 `/api/health`，
  `ok=false`、镜像滞后超阈值 / 同步 `lastError`、Stream `restartCount` 增长、催办
  `lastError` 非空时发钉钉。同一问题默认 60 分钟内不重复。不引入监控系统。
  巡检进程不 import `backend.app`。离线评估在 `tests/test_health_watch.py`。

### P1-8 测试补强（已落地）

- **payload 契约**：`tests/fixtures/payload_contract.json` 是宽度唯一来源。
  Python `tests.test_payload_contract` 断言编码器输出；`scripts/check_payload_contract.mjs`
  核对 `payload.ts` 的 `DASHBOARD_*` / `DELIVERY_*` WIDTH，挂在 `npm run build` 前置。
- **HTTP 鉴权矩阵**：`tests.test_http_auth` 起临时端口，断言 Agent / 预测 / 换货
  401 / 200 / 503，看板与合同接口无鉴权（不是 401）。
- **`test_contracts` 去生产依赖**：`fetch_contract_order(..., fixture_path=)`；
  离线夹具 `tests/fixtures/contract_order_604264.json`。Live 断言保留，
  `CONTRACT_LIVE_TESTS=1` 才连 `hanli.env`。
- 催办幂等用例在 P0-2 内完成。

---

## 迁移准备清单（专用机器）

| 项 | 内容 |
|---|---|
| 系统依赖 | Python 3.11+、Node（仅构建前端）、LibreOffice（calc）、poppler-utils |
| 中文字体 | 合同用 Microsoft YaHei / SimSun；Linux 装字体文件或换 Noto Sans CJK 并目检样张 |
| Python 环境 | 重建 `.venv`：`python3 -m venv .venv && pip install -r requirements.txt` |
| 配置 | 拷贝 `.env`、`hanli.env`、`config/staff_bindings.json`（如有）；核对 `CONTRACT_SOFFICE` 等路径 |
| 数据 | 拷贝 `data/*.sqlite3`、`data/product-images/` |
| 前端 | `npm install && npm run build` |
| 常驻 | systemd unit 托管 `server.py`（`Restart=always`），日志落 journal 或文件；cron / systemd timer 每 5 分钟跑 `scripts/health_watch.py` |
| 鉴权 | 按【决策点 3】落地反代 + 内网 ACL（或代码 token） |
| 时区 | 机器时区设 `Asia/Shanghai`；应用层业务日已固定东八区，不跟浏览器走 |
| 验收 | P0-1 §验收 + P0-5 全套重跑 |
| 之后 | P2（合同历史、供应商绩效、主数据兜底、Agent 库迁 MySQL），见下一节 |

（git 与备份按当前约束不在本方案内，迁移后如需再议。）

---

## P2（迁到专用机器之后再做）

本机不再做这些。MVP 代码与验收（P0-1 预览 / P0-5）在新机器落地后，再按下面做。
预测模型继续占位，不换真实现。

| 事项 | 做什么 | 口径 |
|---|---|---|
| 合同历史 | 落表：谁 / 何时 / 哪单 / 票种 / 文件路径；合同页可翻历史 | 现在 `outputs/generated/` 只是文件堆 |
| 供应商绩效 | L0 工具 `supplier_scorecard` + 看板卡片 | 交期达成 / 逾期 / 入库速度，数据已在采购明细；口径先写 README。`RESERVED_TOOLS` 已占位 |
| 主数据迁移 | 合同补全：分类 / 包装 / 单位改读 `realtime_products` 兜底，少维护 `config/products.json` | **单价仍只认 products.json 或供应商 ERP 价模式，不从商品主数据取价**。国标码、合同图路径仍可留在 JSON |
| Agent 业务库迁 MySQL | 只改 `backend/agent/store.py`：会话 / 待确认 / 审计从 `data/agent.sqlite3` 换到独立 MySQL schema | 表名和 `sessions` / `actions` / `audit` 调用面不变。单机 SQLite 能跑；迁机器后为了和镜像库同一套备份再换 |

**明确不做 / 不改口径**

- **批量合同**：一张采购单 → 一份合同；单内多种商品是明细行（几个实例），不是多张采购单一键出多份 Excel。
- **预测**：销售 / 库存表进库之前 Baseline 占位，不换真模型。
- **`price_watch` / `inventory_watch` / `create_purchase_draft`**：已从 `RESERVED_TOOLS` 删除。

---

## 执行顺序与工作量

| 序 | 事项 | 规模 |
|---|---|---|
| 1 | P0-1 合同去 codex 化 + LibreOffice + 样张验收 | 1 天 |
| 2 | P0-2 催办幂等 + P0-3 Stream 自愈 | 1 天 |
| 3 | P0-4 前端旧数据 / 时区 | 0.5 天 |
| 4 | P0-5 三链路真人验收 | 0.5 天（需采购员） |
| 5 | P1-1 开定时任务 + 退避 | 0.5 天 + 次日观察 |
| 6 | P1-2 任务超时回收 | 1 天 |
| 7 | P1-3 合同竞态 + 五页一致性 + 死代码 | 1 天 |
| 8 | P1-4 安全清理 | 0.5 天 |
| 9 | P1-5 对话硬化 + 台账按钮 | **已落地** |
| 10 | P1-6 国标变更提醒 | **已落地** |
| 11 | P1-7 日志与轻监控 | **已落地** |
| 12 | P1-8 测试补强 | **已落地** |
| — | 迁移 | 按清单执行（专用机器、东八区、LibreOffice、health_watch cron） |
| — | P2 | **迁专用机器之后**：合同历史、供应商绩效、主数据 JSON 兜底、Agent 库迁 MySQL。预测继续占位；无批量合同；三个预留工具已删 |

P0 合计约 3 天；P1 合计约 6–8 天。
