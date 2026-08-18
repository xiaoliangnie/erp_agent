# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

采购看板 / 交期提醒台账 / 采购合同生成 / 订单换货 / 代发订单 / 采购助手对话 / 工作台。一个 Vite + React + TS
单页应用（六条路由）+ 一个 Python 标准库 HTTP 服务。数据源是
供应链安全代理 API 维护的本地可写 MySQL 镜像。业务术语、文件名、注释和文档一律用中文，改动时保持一致。

`AGENTS.md` 有提交与代码风格约定；`README.md` 记录全部业务口径；
`docs/README.md` 是文档索引。现行文档四份：`docs/开发.md`（执行、已拍板、Agent 完成度）、
`docs/架构.md`（当前系统）、`docs/预测.md`、`docs/接口参考.md`。旧方案在 `docs/archive/`。
完成、部分完成、新增或取消一项 Agent 能力时必须改 `docs/开发.md` 的完成度表。
本地文件（配置 / 模板 / 运行时 / 生成物）在 `files/` 下。

## 常用命令

所有命令都在仓库根目录执行（生成器的输入输出路径都是相对路径）。

```bash
# 环境（仓库里的 .venv 是 macOS 上建的，在 Linux 上要重建）
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt

npm install && npm run build                     # 前端产物落 frontend/dist/，server.py 直接托管
npm run dev                                      # http://127.0.0.1:5177，/api 代理到 8777
npm run typecheck                                # tsc --noEmit，build 里也会先跑一遍

.venv/bin/python server.py                       # http://127.0.0.1:8777/ → /dashboard
.venv/bin/python server.py --host 0.0.0.0 --port 8777

python3 -m py_compile backend/*.py backend/*/*.py scripts/*.py server.py   # 快速语法检查

# 离线用例。`discover` 用不了：tests/ 不是包，要按模块名列出
.venv/bin/python -m unittest tests.test_agent tests.test_identity tests.test_forecast tests.test_delivery_reminders tests.test_exchange tests.test_order_source tests.test_product_images tests.test_realtime_mirror tests.test_gb_standards tests.test_contract_gb tests.test_dingtalk tests.test_codex_oauth tests.test_health_watch tests.test_payload_contract tests.test_http_auth tests.test_contracts tests.test_source_cache tests.test_quality tests.test_supplier_master tests.test_erp tests.test_insole tests.test_dropship
.venv/bin/python -m unittest tests.test_contracts   # 离线夹具；CONTRACT_LIVE_TESTS=1 才连库
.venv/bin/python -m unittest tests.test_agent.ConfirmFlowTests   # 单个类

.venv/bin/python scripts/generate_purchase_contract.py \
 --po-id 604264 --invoice-type special_invoice --output files/outputs/采购合同-604264.xlsx

.venv/bin/python scripts/generate_dropship_workbook.py      # 只刷新 代发订单模板.xlsx，不覆盖当日已填表

.venv/bin/python scripts/seed_users.py                      # 扫描采购员，写分析报告
.venv/bin/python scripts/seed_users.py --live --seed        # 从镜像库种子 users
.venv/bin/python scripts/run_agent_cli.py --status          # Agent / 预测 / 钉钉 子系统状态
.venv/bin/python scripts/run_erp_worker.py status           # Digital Worker 配置（不启动浏览器）
.venv/bin/python scripts/run_erp_worker.py login            # 有头登录并保存 storage_state
.venv/bin/python scripts/run_erp_worker.py ping             # 打开订单列表，检查 _ACP
.venv/bin/python scripts/run_agent_cli.py --operator 张三   # 对话调试，不经 HTTP
.venv/bin/python scripts/run_dingtalk_cli.py status         # 钉钉 Stream / 发送通道 / 绑定
.venv/bin/python scripts/health_watch.py                    # 拉 /api/health，异常发钉钉
.venv/bin/python scripts/health_watch.py --dry-run          # 只评估，不发钉钉

# 训练并落工件到 FORECAST_MODEL_DIR，--forecaster 缺省用仓库里的 Baseline
.venv/bin/python scripts/train_forecast_model.py --csv 销售明细.csv --forecaster mypkg.model:MyForecaster

# 国标目录元数据 → hanli.env 的 gb_standards 表（不下载标准全文；默认按商品表分类）
.venv/bin/python scripts/sync_gb_standards.py --dry-run
.venv/bin/python scripts/sync_gb_standards.py
.venv/bin/python scripts/sync_gb_standards.py --scope all
```

`tests/test_agent.py`、`tests/test_forecast.py`、`tests/test_delivery_reminders.py`、
`tests/test_exchange.py` 全部离线，不需要任何凭证。`tests/test_contracts.py` 的合同模型
离线走夹具；对真实采购单 604264 的 live 断言要设 `CONTRACT_LIVE_TESTS=1` 且有 `hanli.env`。

`scripts/sync_purchase_data.py --env /path/to/test.env` 只用于把 CSV 幂等导入**另一个测试库**，
`--env` 是必填的，不在生产链路上。

## 架构

### 请求链路

```
供应链安全代理 API
  → backend/realtime_mirror.py  增量同步订单 / 采购单 / 可选商品图片
  → hanli.env（本地可写镜像库）
  → backend/database.py     按 po_id 关联规范化采购主表与明细表
  → backend/procurement_data.py  转成字典编码 payload
  → backend/app.py          /api/dashboard · /api/delivery · /api/contracts/*
  → frontend/src/hooks/usePayload.ts → frontend/src/data/payload.ts 解码 → 页面组件
```

Agent 链路复用同一份查询和同一份缓存，不另开数据源：

```
前端 /chat 页 · 钉钉 Stream
  → backend/app.py            /api/agent/chat（Bearer AGENT_API_TOKEN）
  → backend/agent/runner.py   工具循环，上限 AGENT_MAX_TOOL_STEPS
  → backend/agent/tools.py    工具注册表：L0 直接执行，L1/L2 转 pending_action 等人工确认
  → 确定性实现               database.py · delivery_reminders.py · contracts.py · exchange/ · forecast/
  → backend/agent/store.py    SQLite：会话 / 运行 / 工具调用 / 待确认动作 / 审计
```

`server.py` 只是 `backend.app.main` 的入口。服务用标准库 `ThreadingHTTPServer` 手写，
没有 Web 框架，运行期依赖是 PyMySQL、openpyxl、Pillow（钉钉 Stream 才需要 `dingtalk-stream`）；新增接口
就是在 `Handler.do_GET/do_POST` 里加分支。`source_cache()` 按年度缓存 30 秒，页面和
Agent 工具（`agent_rows()`）共用这一份，短时间内不会重复压库。
实时库连不上就直接报错，**不回退旧库**——避免员工把历史快照当成实时数据。
国标目录由 `backend/gb_standards.py` 写入同一镜像库的 `gb_standards` 表，不经过供应链代理。

### 前端：一个单页应用，六条路由

```
frontend/index.html · frontend/src/main.tsx     Vite 入口，root 是 frontend/
  → src/App.tsx                                 六个页面按 React.lazy 分块
  → src/routes.ts                                /dashboard /ledger /contract /exchange /chat /workbench
                                                 路径用 ASCII 且只写这一处，标题仍是中文
  → src/pages/<page>/                            每页一个目录：Page + 视图模型 + 局部 CSS
  → src/api/client.ts                            publicApi / exchangeApi / agentApi
  → src/data/payload.ts                          位置数组 payload 的唯一解码点
  → src/styles/base.css                          设计令牌与共用原语，只在这里定义颜色
```

`backend/app.py` 托管 `frontend/dist/`：`/assets/*` 带内容哈希，长缓存；其余非 `/api/`
路径统一回 `index.html`（SPA 回退）；旧的 `/采购看板.html` 和更早那版中文路径（`/看板` 等）
在 `LEGACY_PAGES` 里 302 到新路由。`dist/` 不存在时页面返回 503 并提示先构建，接口不受影响。

改前端用 `npm run dev`（Vite 把 `/api` 代理到 8777），**不要**直接改 `frontend/dist/`。
新增页面文件不再需要在后端登记白名单，但新增非 SPA 静态资源（如换货核心 JS）要加进 `STATIC_FILES`。

### 中文列名是内部契约

`fetch_realtime_purchase_rows` 用英文别名查 SQL，然后立刻用 `column_map` 改回
`采购单号` / `最早预计到货日期` / `item_in_qty` 这类 CSV 表头名。下游
（`procurement_data.py`、快照 CSV）全部只认这套中文/原始字段名。`fetch_purchase_rows`（旧
`purchase_order_lines` 表）产出同样的 dict 形状。加字段要一路改：SQL → `column_map` →
`procurement_data.py` 的 payload → `frontend/src/data/payload.ts` 的下标常量与解码函数。

### 位置数组 payload 与下标常量

payload 是 `{meta, dict, orders, lines}`，`orders`/`lines` 是纯位置数组，字典维度只存下标。
列名常量现在只有两处，改列顺序同步这两处即可：

- `backend/procurement_data.py` 的 `DASHBOARD_*_COLUMNS` / `DELIVERY_*_COLUMNS`（响应带 `columns`）
- `frontend/src/data/payload.ts` 的同名列数组

`payload.ts` 的 `decodeDashboard` / `decodeDelivery` 按列名解码；后端下发的 `columns` 与前端不一致、
或旧缓存列数不符，都会直接抛错，而不是静默错位。

离线快照链路（`frontend/data/*.js` + `scripts/build_*.py` + adapters 回退）已下线，
旧实现留在 `legacy/`。**现在只有一份转换实现**（`procurement_data.py`），
`server.py` 是页面数据的唯一来源。

### 两个页面的日期口径不同，是有意的

采购看板走 `最早预计到货日期`（预计到货），交期提醒台账走 `item_delivery_date`（与供应商
约定的交期），该行为空才退到预计到货日。两列覆盖率和最远日期都不一样，不要"统一"。
交期台账与钉钉催办走跟单三档（≤10 天 / ≤3 天 / 已逾期）；采购看板的到货档位仍按预计到货日分档。完整口径见 `README.md`，改动要同步更新那里。

### 合同生成

`backend/contracts.py` 把实时 ERP 单头/明细与本机供应商表、两份 JSON 主数据合并成合同模型：

- `files/config/供应商管理.xlsx`：ERP 导出的供应商管理表，**不上库**，本机覆盖维护。
  `backend/supplier_master.py` 按修改时间重读。匹配键是 ERP `seller` ↔ 表内「简称」
  （也可命中全称 / 编码；带 `&` 前缀仅在不冲突时互认）。给出全称、地址、联系人、
  由「发票类型」推出的 `invoice_rates` / `erp_price_mode`。冻结或字段缺失则中止。
  没有这张表时回退 `files/config/suppliers.json`。路径可用 `SUPPLIER_MASTER_XLSX` 覆盖。
  公司内部户见 `files/config/internal_suppliers.json`：不列收付款信息，不要求 Excel 全称。
- `files/config/contract_mappings.json`（`backend/contract_mappings.py`）：合同生成的唯一映射表。
  `invoice_types` 是 Excel「发票类型」原文 → 票种；`payment_options` 是付款方式条款
  （`label` 只给页面下拉用，**写进合同的只有 `text`**）；`erp_payment_defaults` 是 ERP
  `payment_method` → 默认预选哪一条。员工可选「手动输入」自己写条款。
  解析顺序见 `resolve_payment_terms`：手输 > 选项 > 内部往来 > ERP 预选 > 报错要求先选。
  预选顺序另见 `get_contract_options`：**上次用过的**（`backend/contract_history.py`，
  本机 `files/config/payment_history.json`，原子写、不进库）优先于 ERP 预选。
  单价参考走 `fetch_supplier_price_history`（同供应商同 SKU 最近 3 次，排除本单），
  查失败只是没有参考，不能影响合同生成。
- `files/config/products.json`：先按 SKU 命中，再按款式编码，给出国标码（商品条码）、分类、包装、
  三类票种价格和 `image_path`。没维护到的 SKU 用 `fetch_product_master`（镜像库
  `realtime_products`）兜底**单位 / 分类 / 名称**；**单价永远不兜底**。执行标准（GB/T…）
  不在这份文件里，来自 `gb_standards`，按采购明细 `poi_id` 写入 `contract_line_gb`。
- `files/config/buyers.json`：按 `send_address` 匹配 `warehouses`，未命中用 `default`

供应商未维护、字段缺失、该票种没有单价时**直接抛 `ValueError` 中止**，绝不生成带占位信息的
合同——这是刻意的，不要加兜底默认值。票种只有 `no_invoice` / `normal_invoice` /
`special_invoice`，默认税率 0 / 0 / 13，员工可覆盖。

模型由 `backend/contract_workbook.py`（openpyxl）在进程内写成 `.xlsx`：

```
build_contract_model → write_contract_workbook → .xlsx
                                              → soffice --convert-to pdf → pdftoppm → .png 预览
```

根目录 `package.json` 只管前端。预览必须走真实办公套件渲染，否则嵌入的商品图片不会出现。
可执行文件可用环境变量覆盖：`CONTRACT_SOFFICE`、`CONTRACT_PDFTOPPM`、`CONTRACT_FONTCONFIG_FILE`。

整张表都从 `itemStart = 9` 和明细条数算行号（合计行、条款行、签字行、合并区、
`=N*L` 与 `=SUM` 公式全跟着偏移）。明细共 16 列 A–P：**国标码**（E，商品条码）、**品名**（F）、
**执行标准**（G，GB/T…）。明细备注写在 Q 列，打印区 A–P。需方收货信息合并 A6:A7 / B6:H7；一条明细时
送货地址在第 14 行。增删表头行必须同时改这些偏移量和 `merges` 列表。空白母版由
`write_blank_contract_template()` 写入 `files/templates/采购合同模板.xlsx`。

### Agent Core

工具注册表（`backend/agent/tools.py`）是模型唯一能碰到的业务入口：声明名称、入参
JSON Schema、风险级和 handler。**禁止让模型生成 SQL 或改动工具返回的数字**，
每项查询对应一个固定参数化工具。新增能力就是加一条 `registry.register(...)`，
不改 Agent Core；`RESERVED_TOOLS` 目前只留 `supplier_scorecard` 占位
（`master_data_gaps` 已注册为 L0；价格盯盘 / 库存预警 / 采购草稿已取消）。

L0 直接执行。**L1/L2 一律不直接执行**：`runner._invoke` 把它转成 `pending_actions`
一条记录（默认 30 分钟），带上 `preview` 的要点；`PendingActions.execute` 在
`BEGIN IMMEDIATE` 事务里把状态推到 `confirmed` 再执行，所以并发确认只有一个能拿到执行权，
重复确认回放已有结果。确认人必须是发起人。改这段逻辑要同时看 `tests/test_agent.py`
的 `ConfirmFlowTests`。

发给模型的上下文在 `sessions.context_messages` 里按层组装：系统规则 → 身份/渠道 →
操作员记忆 → 消毒后的摘要 → 当前业务对象快照（只含单号/SKU/待确认编号）→ 近讯。
「新话题 / 记住 / 忘记」走 `runner.handle_session_command`，网页和钉钉同一套。
固定业务原话先走 `intents.classify_intent`（抽槽后拒答 / 追问 / 调工具），未识别再进
LLM 工具循环。L1/L2 仍经 `pending_actions`。不要在意图层猜 `o_id` 或改工具数字。
能写死的原话先走 `intents.classify_intent`（抽槽后拒答 / 追问 / 调已注册工具），
未识别再进 LLM 工具循环。L1/L2 仍进 `pending_actions`，禁止猜 `o_id`。
记忆按 `user_id` 存，注入前扫描控制字符和注入句；`AGENT_MEMORY_ENABLED` /
`AGENT_SUMMARY_ENABLED` 默认关。同会话忙时回「上一条还在处理」。

Agent 业务库是本地 SQLite（`AGENT_DATABASE_PATH`），表名与架构方案 §10 一致，
连接和建表集中在 `backend/agent/store.py` 一处。迁 MySQL 只换这一层，**P2、专用机器落地后再做**。

ERP 写入走 `DigitalRuntime.run("erp.exchange_items")`：写入前快照、写入后
`loadOrder` 回读 SKU，对不上不记成功；已经是目标 SKU 则跳过改单。JSON 证据落
`files/data/erp-evidence/`（`ERP_EVIDENCE_DIR`）。结果未知抛 `ErpUnknownResult`，不得重试。
正式建单和其他出库仍关闭。

### 预测子系统的边界

`backend/forecast/models.py` 的 `Forecaster` 是唯一接口，仓库里的 `BaselineForecaster`
只是占位实现。工件目录 `metadata.json` 里的 `forecaster` 字段（`模块:类名`）是服务端与
模型实现之间**唯一的耦合点**，换实现不改调用方。接入步骤见 `docs/预测.md`。

销售出库表和现势库存表还没进实时库，表名列名做成了 `FORECAST_SALES_*` /
`FORECAST_INVENTORY_*` 配置。**库存缺失时 `order_suggestion` 直接报错说明缺哪些 SKU，
不用 0 兜底**——与合同生成同一哲学。在途待入库已可用（采购明细 数量 − 已入库）。

### 跟单三档催办口径只有一份实现

`backend/delivery_reminders.py` 的 `profile=followup` 被交期台账页、Agent 的
`delivery_reminders` 工具和钉钉每日推送共用。改档位边界或日期回退顺序要同步
`README.md` 的口径章节、`frontend/src/pages/ledger/waves.ts` 和
`tests/test_delivery_reminders.py`。旧四波留在 `profile=ledger`，不要接到台账页上。

### 配置与接口鉴权

- `hanli.env`：本地镜像数据库凭证，只由服务端读，`.gitignore` 里
- `.env` 中的 `SUPPLY_API_*`：供应链代理 Client 凭据，同样不提交
- `.env`：服务、Agent、钉钉配置，在 `backend/app.py` 导入时由 `load_all_env` 读入；
  `setting()` 让进程环境变量优先于 `.env`
- 页面走 `frontend/dist/` SPA 托管；`STATIC_FILES` 白名单只剩换货核心 JS（油猴脚本已退役，路径仍提供）
- `/api/contracts/*` 页面用、无鉴权（资源路径限制在 `files/outputs/` 下）；`/api/exchange/*`
  双 token（页面 `EXCHANGE_API_TOKEN` / worker `EXCHANGE_WORKER_TOKEN`）；
  `/api/agent/*`、`/api/forecast/*` 用 Bearer `AGENT_API_TOKEN` 常量时间比对，
  未配置 token 时返回 503 保持关闭
- `AGENT_ENABLED` 默认 `false`；关闭时对话接口返回 503，看板与合同链路不受影响
- `DINGTALK_ENABLED` 是总闸：关闭则不装配发送通道，催办/品控日报也发不出去。
  `DINGTALK_REMINDER_ENABLED` 另管每日定时催办（不依赖大模型）。
  应用机器人 `groupMessages/send` 官方不支持 @。催办已绑定员工走
  `oToMessages/batchSend` 单聊；群里要点到人仍走自定义 Webhook
  或 Stream 缓存的入站 `sessionWebhook`（`files/data/dingtalk_session_webhooks.json`）
- `GB_SYNC_ENABLED` 管国标目录每日同步；失败按指数退避封顶 900 秒。同步成功后若合同已选用
  的执行标准变成现行或废止，会推钉钉（同一标准同一天只发一次）。手工跑
  `scripts/sync_gb_standards.py`
- `LOG_FILE` 默认 `files/data/app.log`，空则只写 stderr；时间固定东八区，文件用 RotatingFileHandler。
  `scripts/health_watch.py` 每 5 分钟拉 `/api/health` 发钉钉，不要 import `backend.app`
  （会把 HTTP 服务、催办调度、品控调度和镜像线程全部装配进来）
- 网页生成的合同落在 `files/outputs/generated/`，Agent 生成的落在 `files/outputs/agent/<24位hex>/`；
  `files/outputs/` 整个不进版本库

## 约定

- Python 四空格、PEP 8、中文 docstring；TS/TSX/CSS 两空格，`const` 优先，`camelCase`
- 前端组件用函数式 + hooks，页面级状态就用 `useState`，没有引入状态库；颜色只从
  `base.css` 的令牌取（`cssVar()`），不要在组件里写死色值
- 改前端必须能通过 `npm run build`（含 `tsc --noEmit`），`noUnusedLocals` 是开着的
- 前端业务日期禁止用本地时钟 `new Date()` 运算，「今天」一律取 payload `meta.today`；
  日历加减用 UTC（`T00:00:00Z` + `getUTC*` / `setUTCDate`），不要 `toISOString()` 去本地午夜
- 改动日期或数量口径必须同步更新 `README.md` 的口径章节
- 新增 Agent 工具要同时补 `tests/test_agent.py`；L1/L2 工具必须给出 `preview`
- 完成、部分完成、新增或取消 Agent 能力时，同步改 `docs/开发.md` 的完成度表（条目、分组小计、总进度、最近变更）
- 不要提交凭证、供应商真实信息、训练好的模型工件（`files/data/models/`）或无关的 CSV 导出
