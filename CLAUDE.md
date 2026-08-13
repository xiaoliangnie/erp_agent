# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

采购看板 / 交期提醒台账 / 采购合同生成 / 订单换货 / 采购助手对话。一个 Vite + React + TS
单页应用（五条路由）+ 一个 Python 标准库 HTTP 服务 + 一个 Node 电子表格生成器。数据源是
供应链安全代理 API 维护的本地可写 MySQL 镜像。业务术语、文件名、注释和文档一律用中文，改动时保持一致。

`AGENTS.md` 有提交与代码风格约定；`README.md` 记录全部业务口径；
`docs/PROJECT_DESIGN.md`、`docs/AGENT_ARCHITECTURE.md` 记录架构与后续路线。

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
.venv/bin/python -m unittest tests.test_agent tests.test_forecast tests.test_delivery_reminders tests.test_exchange tests.test_order_source tests.test_product_images tests.test_realtime_mirror tests.test_gb_standards tests.test_contract_gb tests.test_dingtalk tests.test_codex_oauth
.venv/bin/python -m unittest tests.test_contracts   # 需要 hanli.env 真连库
.venv/bin/python -m unittest tests.test_agent.ConfirmFlowTests   # 单个类

.venv/bin/python scripts/generate_purchase_contract.py \
 --po-id 604264 --invoice-type special_invoice --output outputs/采购合同-604264.xlsx

.venv/bin/python scripts/run_agent_cli.py --status          # Agent / 预测 / 钉钉 子系统状态
.venv/bin/python scripts/run_agent_cli.py --operator 张三   # 对话调试，不经 HTTP
.venv/bin/python scripts/run_dingtalk_cli.py status         # 钉钉 Stream / 发送通道 / 绑定

# 训练并落工件到 FORECAST_MODEL_DIR，--forecaster 缺省用仓库里的 Baseline
.venv/bin/python scripts/train_forecast_model.py --csv 销售明细.csv --forecaster mypkg.model:MyForecaster

# 国标目录元数据 → hanli.env 的 gb_standards 表（不下载标准全文；默认按商品表分类）
.venv/bin/python scripts/sync_gb_standards.py --dry-run
.venv/bin/python scripts/sync_gb_standards.py
.venv/bin/python scripts/sync_gb_standards.py --scope all
```

`tests/test_agent.py`、`tests/test_forecast.py`、`tests/test_delivery_reminders.py`、
`tests/test_exchange.py` 全部离线，不需要任何凭证。只有 `tests/test_contracts.py` 例外：
`build_contract_model` 会真连 `hanli.env` 指向的实时镜像库，并对真实采购单 604264 断言日期、
供应商和单价，没有凭证时整个文件跑不起来，也没有离线夹具路径。

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
没有 Web 框架，运行期依赖只有 PyMySQL（钉钉 Stream 才需要 `dingtalk-stream`）；新增接口
就是在 `Handler.do_GET/do_POST` 里加分支。`source_cache()` 按年度缓存 30 秒，页面和
Agent 工具（`agent_rows()`）共用这一份，短时间内不会重复压库。
实时库连不上就直接报错，**不回退旧库**——避免员工把历史快照当成实时数据。
国标目录由 `backend/gb_standards.py` 写入同一镜像库的 `gb_standards` 表，不经过供应链代理。

### 前端：一个单页应用，五条路由

```
frontend/index.html · frontend/src/main.tsx     Vite 入口，root 是 frontend/
  → src/App.tsx                                 五个页面按 React.lazy 分块
  → src/routes.ts                                /dashboard /ledger /contract /exchange /chat
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
新增页面文件不再需要在后端登记白名单，但新增油猴脚本一类的非 SPA 资源要加进 `STATIC_FILES`。

### 中文列名是内部契约

`fetch_realtime_purchase_rows` 用英文别名查 SQL，然后立刻用 `column_map` 改回
`采购单号` / `最早预计到货日期` / `item_in_qty` 这类 CSV 表头名。下游
（`procurement_data.py`、快照 CSV）全部只认这套中文/原始字段名。`fetch_purchase_rows`（旧
`purchase_order_lines` 表）产出同样的 dict 形状。加字段要一路改：SQL → `column_map` →
`procurement_data.py` 的 payload → `frontend/src/data/payload.ts` 的下标常量与解码函数。

### 位置数组 payload 与下标常量

payload 是 `{meta, dict, orders, lines}`，`orders`/`lines` 是纯位置数组，字典维度只存下标。
下标常量现在只有两处，改列顺序同步这两处即可：

- `backend/procurement_data.py:76` `build_dashboard_payload` / `:138` `build_delivery_payload`
- `frontend/src/data/payload.ts` 的 `O_*` / `L_*` 与 `EXPECTED_*` 宽度

`payload.ts` 的 `decodeDashboard` / `decodeDelivery` 会校验数组宽度并把位置数组转成命名字段，
页面组件拿到的是对象，不再各写一份下标。宽度不符会直接抛错，而不是静默错位。

离线快照链路（`frontend/data/*.js` + `scripts/build_*.py` + adapters 回退）已下线，
旧实现留在 `legacy/`。**现在只有一份转换实现**（`procurement_data.py`），
`server.py` 是页面数据的唯一来源。

### 两个页面的日期口径不同，是有意的

采购看板走 `最早预计到货日期`（预计到货），交期提醒台账走 `item_delivery_date`（与供应商
约定的交期），该行为空才退到预计到货日。两列覆盖率和最远日期都不一样，不要"统一"。
四波提醒（T-20 / T-10 / T-1 / 逾期）的完整口径见 `README.md`，改动要同步更新那里。

### 合同生成

`backend/contracts.py` 把实时 ERP 单头/明细与三份 JSON 主数据合并成合同模型：

- `config/suppliers.json`：键是 ERP 的 `seller`（供应商简称），给出全称、地址、联系人、
  `invoice_rates` 和 `erp_price_mode`
- `config/products.json`：先按 SKU 命中，再按款式编码，给出国标码（商品条码）、分类、包装、
  三类票种价格和 `image_path`。执行标准（GB/T…）不在这份文件里，来自 `gb_standards`，
  按采购明细 `poi_id` 写入 `contract_line_gb`。
- `config/buyers.json`：按 `send_address` 匹配 `warehouses`，未命中用 `default`

供应商未维护、字段缺失、该票种没有单价时**直接抛 `ValueError` 中止**，绝不生成带占位信息的
合同——这是刻意的，不要加兜底默认值。票种只有 `no_invoice` / `normal_invoice` /
`special_invoice`，默认税率 0 / 0 / 13，员工可覆盖。

模型写成临时 JSON 后交给 Node：

```
build_contract_model → *.contract-input.json → node scripts/generate_contract.mjs → .xlsx
                                                    → soffice --convert-to pdf → pdftoppm → .png 预览
```

`scripts/generate_contract.mjs` 依赖 `@oai/artifact-tool`，根目录 `package.json` 只管前端，
**没有声明这个包，当前 checkout 里也解析不到**——合同生成需要预置该模块的环境。
可执行文件可用环境变量覆盖：`CONTRACT_NODE`、`CONTRACT_SOFFICE`、`CONTRACT_PDFTOPPM`、
`CONTRACT_FONTCONFIG_FILE`。预览必须走真实办公套件渲染，否则嵌入的商品图片不会出现。

`generate_contract.mjs` 的整张表都是从 `itemStart = 9` 和明细条数算出来的行号
（合计行、条款行、签字行、合并区、`=N*L` 与 `=SUM` 公式全跟着偏移）。明细共 16 列 A–P，
**国标码**（E，商品条码）和 **执行标准**（F，GB/T…）不是同一列。增删表头行必须
同时改这些偏移量和 `merges` 列表。

### Agent Core

工具注册表（`backend/agent/tools.py`）是模型唯一能碰到的业务入口：声明名称、入参
JSON Schema、风险级和 handler。**禁止让模型生成 SQL 或改动工具返回的数字**，
每项查询对应一个固定参数化工具。新增能力就是加一条 `registry.register(...)`，
不改 Agent Core；§14 预留的工具位在 `RESERVED_TOOLS` 里只占位、不写实现。

L0 直接执行。**L1/L2 一律不直接执行**：`runner._invoke` 把它转成 `pending_actions`
一条记录（默认 30 分钟），带上 `preview` 的要点；`PendingActions.execute` 在
`BEGIN IMMEDIATE` 事务里把状态推到 `confirmed` 再执行，所以并发确认只有一个能拿到执行权，
重复确认回放已有结果。确认人必须是发起人。改这段逻辑要同时看 `tests/test_agent.py`
的 `ConfirmFlowTests`。

Agent 业务库是本地 SQLite（`AGENT_DATABASE_PATH`），表名与架构方案 §10 一致，
连接和建表集中在 `backend/agent/store.py` 一处，迁 MySQL 只换这一层。

### 预测子系统的边界

`backend/forecast/models.py` 的 `Forecaster` 是唯一接口，仓库里的 `BaselineForecaster`
只是占位实现。工件目录 `metadata.json` 里的 `forecaster` 字段（`模块:类名`）是服务端与
模型实现之间**唯一的耦合点**，换实现不改调用方。接入步骤见 `docs/预测模型接入.md`。

销售出库表和现势库存表还没进实时库，表名列名做成了 `FORECAST_SALES_*` /
`FORECAST_INVENTORY_*` 配置。**库存缺失时 `order_suggestion` 直接报错说明缺哪些 SKU，
不用 0 兜底**——与合同生成同一哲学。在途待入库已可用（采购明细 数量 − 已入库）。

### 四波催办口径只有一份实现

`backend/delivery_reminders.py` 被交期台账页口径、Agent 的 `delivery_reminders` 工具和
钉钉每日推送共用。改档位边界或日期回退顺序要同步 `README.md` 的口径章节和
`tests/test_delivery_reminders.py`。

### 配置与接口鉴权

- `hanli.env`：本地镜像数据库凭证，只由服务端读，`.gitignore` 里
- `.env` 中的 `SUPPLY_API_*`：供应链代理 Client 凭据，同样不提交
- `.env`：服务、Agent、钉钉配置，在 `backend/app.py` 导入时由 `load_all_env` 读入；
  `setting()` 让进程环境变量优先于 `.env`
- 页面走 `frontend/dist/` SPA 托管；`STATIC_FILES` 白名单只剩油猴 worker 脚本
- `/api/contracts/*`、`/api/exchange/*`（页面用）无鉴权；`/api/agent/*`、`/api/forecast/*`
  用 Bearer `AGENT_API_TOKEN` 常量时间比对，未配置 token 时返回 503 保持关闭
- `AGENT_ENABLED` 默认 `false`；关闭时对话接口返回 503，看板与合同链路不受影响
- `DINGTALK_ENABLED` 管发送与 Stream 交互，`DINGTALK_REMINDER_ENABLED` 单独管每日定时
  推送（不依赖大模型，可以只开这一个），默认都是 `false`
- `GB_SYNC_ENABLED` 管国标目录每日同步，默认 `false`；手工跑 `scripts/sync_gb_standards.py`
- 网页生成的合同落在 `outputs/generated/`，Agent 生成的落在 `outputs/agent/<24位hex>/`；
  `outputs/` 整个不进版本库

## 约定

- Python 四空格、PEP 8、中文 docstring；TS/TSX/CSS 两空格，`const` 优先，`camelCase`
- 前端组件用函数式 + hooks，页面级状态就用 `useState`，没有引入状态库；颜色只从
  `base.css` 的令牌取（`cssVar()`），不要在组件里写死色值
- 改前端必须能通过 `npm run build`（含 `tsc --noEmit`），`noUnusedLocals` 是开着的
- 改动日期或数量口径必须同步更新 `README.md` 的口径章节
- 新增 Agent 工具要同时补 `tests/test_agent.py`；L1/L2 工具必须给出 `preview`
- 不要提交凭证、供应商真实信息、训练好的模型工件（`data/models/`）或无关的 CSV 导出
