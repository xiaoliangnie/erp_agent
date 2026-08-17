# 采购看板

采购数据服务、五个业务页面与采购助手 Agent。系统统一读取 `hanli.env` 指向的本地 MySQL
镜像库，由供应链安全代理 API 增量维护订单和采购单数据。
当前执行文档见 `docs/开发.md`。Agent 完成度见 `docs/Agent进度.md`。数据链路 / Agent / 钉钉见 `docs/architecture/`。
自训练预测模型的接入步骤见 `docs/预测.md`。
Agent 与钉钉密钥模板见根目录 `.env.example`。

## 订单 SKU 换货

访问 `http://127.0.0.1:8777/exchange`，输入源 SKU、目标 SKU 和明确的 ERP 内部
订单号 `o_id`，即可创建 dry-run 任务。第一版的订单数据不来自采购数据库：常开的、
已登录聚水潭订单列表页由 `frontend/js/exchange-worker.user.js` 读取真实订单明细，回传逐单
预演清单；员工在换货页确认后，worker 才调用 ERP 页面原生 `_ACP('ChangeItem')`。

安全约束：

- `.env` 必须配置彼此不同的 `EXCHANGE_API_TOKEN` 和 `EXCHANGE_WORKER_TOKEN`；前者给换货页，
  后者只写入油猴 worker 配置。
- 换货页的 Token 只存当前标签页 `sessionStorage`；数据库凭证、ERP Cookie 都不会进入页面。
- 任务必须明确列出全部 `o_id`，试算必须完整覆盖这些订单，缺单、取消/退款状态、找不到源 SKU
  都会作为跳过原因返回。
- 真实执行必须经过 dry-run 和创建人确认。执行任务只投递一次，断线不会自动重放 ERP 写操作。
  试算 / 订单搜索 / 只读探测领取超过 5 分钟会退回队列，最多回收 3 次后标失败。
  已经开始改 ERP 的任务超过 15 分钟标为 `stuck`（中断），钉钉告警，由人工核对后再决定是否另建任务；
  Worker 迟到的执行结果仍可凭原凭证回写，避免页面上丢掉已改过的订单。
- 多个已登录的 ERP 订单标签页会作为独立 Worker 槽位，同时领取不同订单的任务；同一订单同时只能有
  一个活动换货任务，防止并发重复修改。一个标签页内仍按顺序执行，避免 ERP 页面状态串单。
- 采购助手可以理解“把订单 A 的 SKU B 换成 SKU C”，以及“这批待发货异常单把 B 换成 C”：
  先按订单镜像收成明确 `o_id` 再生成待确认 dry-run。Agent 确认只登记试算，真实 ERP 写入
  仍需在换货页核对试算清单后二次确认。
- 抖音换鞋垫是单独一条固定流程：钉钉或对话里说「查询一下现在抖音需要更换的鞋垫订单」，
  会列出内部单号、平台单号、状态、店铺、鞋码和目标 SKU；加上「进行处理」会生成待确认动作。
  钉钉直接回复「确认」（不用带编号、不用去换货页）后，由后端 Playwright **串行**写入；
  先回「已开始写入」，写完再发一条【任务完成】结果日志（清单和结果都只展开 5 条：单号、状态、鞋码、目标鞋垫）。
  浏览器只打开一次订单页，后续按尺码试算/写入复用同一页，速度与本地批量脚本同一量级。半码按码数舍去小数再换算毫米（`40.5` → `40` → `250mm` → `09906`）；
  发货中只列出不写。写入成功后立刻按内部单号回写镜像，并把这批单记进已写入台账；增量同步还没跟上时，再查不会把同一批再列出来。
  同一会话重复「进行处理」会复用已有待确认，不另开一条。
  钉钉写操作必须已绑定；权限表尚未落地，先按 `viewer` / 绑定拦截，后续在同一检查点加 capabilities。
- 「异常订单」第一期只处理 SKU 替换（同款换规格 / 指定源→目标 / 已维护白名单跨款）。
  备注异常、超卖、地址错误没有规则，不会由 AI 自行定义。采购逾期走催办，不走换货。

安装 worker：在 Tampermonkey 新建脚本，粘贴
`frontend/js/exchange-worker.user.js`，通过脚本菜单配置服务地址和 `EXCHANGE_WORKER_TOKEN`，
然后保持 `/app/order/order/list.aspx` 标签页登录且打开。换货页顶端显示 Worker 在线后即可试算。
同一个 Worker 也负责合同商品图片的只读同步：合同页点击“从 ERP 同步图片”后，它会用浏览器
登录态读取 `purchaseitem.aspx` 的 `pic300` / `pic160` / `pic100`，不会调用换货写接口。
任务队列第一阶段使用 `files/data/exchange_jobs.sqlite3`，后续 Agent 业务 MySQL 到位时只替换
`backend/exchange/service.py` 的存储实现。
换货白名单在 `files/config/exchange-rules.json`，服务按文件修改时间重读，改完不必重启。

### 页内核心 / Codex 直接调用

ERP 写操作的真正逻辑在 `frontend/js/jst-order-exchange.core.js`（纯 JS，无 GM 依赖）。
油猴 worker 启动后会把它注入页面；Codex / Playwright 也可直接 `page.evaluate` 注入。
注入后页面上有：

```js
// dry-run，不写 ERP
const plan = await JstOrderExchange.plan({
  oIds: ['10001', '10002'],
  from: '源SKU',
  to: '目标SKU',
  sourceStyle: '款式编码',
  targetStyle: '款式编码',
  // exchangeType: 'special_mapping', // 鞋垫等白名单跨款时再开
});

// 核对 plan.plans 后才允许写；confirm 必须为 true
const result = await JstOrderExchange.execute({
  plans: plan.plans.filter((p) => p.ok),
  confirm: true,
});
```

辅助命令（生成 Codex/Playwright 片段；可选 CDP 实调）：

```bash
node scripts/jst_exchange_call.mjs print-inject
node scripts/jst_exchange_call.mjs plan-snippet \
  --from 源SKU --to 目标SKU --oids 10001,10002 \
  --source-style 款式 --target-style 款式
# 可选：Chrome 以 --remote-debugging-port=9222 启动后
# node scripts/jst_exchange_call.mjs plan --cdp http://127.0.0.1:9222 --from ... --to ... --oids ...
```

服务端也会托管核心文件：`http://127.0.0.1:8777/js/jst-order-exchange.core.js`。

五个页面（同一个 React 单页应用的五条路由）：

| 路由 | 看什么 | 日期口径 |
|---|---|---|
| `/dashboard` | 采购全景：金额、品类、尺码、入库进度 | `最早预计到货日期` |
| `/ledger` | 单号 / 日期 / 供应商 / 产品 / 交期 / 入库数量 / 采购员，按四波提醒催货 | `item_delivery_date`（交期） |
| `/contract` | 选单、选票种和单价、预览、下载采购合同 | — |
| `/exchange` | 从镜像订单选单和源 SKU → 选择规则允许的目标 SKU → dry-run + 人工确认 | — |
| `/chat` | 采购助手：查单、催办、生成合同、订货建议 | — |

路由用 ASCII，中文路径在地址栏和日志里会变成 percent-encoding，不好读也不好搜；页面
标题和导航仍是中文业务叫法。旧的 `/采购看板.html` 和更早那版中文路径（`/看板` 等）都以
302 指向现在的路由，书签不用改。

看板与台账两页顶栏可相互跳转，并通过 `?year=2026` 保留当前统计年度。默认读取本年度
1 月 1 日至服务器当天；用户主动切换历史年度时读取该自然年，再叠加页面其他筛选条件。

## 启动

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm install && npm run build        # 前端产物落 frontend/dist/
.venv/bin/python server.py
```

访问 `http://127.0.0.1:8777/`（会跳到 `/dashboard`）。页面只走同源 API，没有离线快照：
`server.py` 是唯一入口，`frontend/dist/` 不存在时页面返回 503，接口仍可用。

改前端时用 `npm run dev`（<http://127.0.0.1:5177>），`/api` 由 Vite 代理到 8777，
所以 `server.py` 要同时开着。提交前跑 `npm run build`，它会先核对 payload 宽度契约
（`scripts/check_payload_contract.mjs`），再做 `tsc --noEmit` 类型检查。

## 日志与健康巡检

日志走标准库 `logging`，时间固定东八区。`LOG_FILE` 默认 `files/data/app.log`（不进版本库）；
留空则只写 stderr，迁移后可直接接 systemd journal。`/api/health` 的访问记录是 DEBUG，
避免五分钟巡检把 INFO 刷满。

`scripts/health_watch.py` 单独进程拉 `/api/health`，**不 import** `backend.app`，
不会把催办调度带起来。发现以下情况时发钉钉（同一问题默认 60 分钟内不重复）：

- `ok=false`（库连不上）
- 实时镜像已启用且滞后超过 `HEALTH_WATCH_LAG_MINUTES`（默认 15），或同步 `lastError` 非空
- 钉钉 Stream `restartCount` **相对上次巡检增加**（第一次只记基数，避免进程刚起来就告警）
- 催办 `dingtalk.reminder.lastError` 非空

状态记在 `files/data/health_watch_state.json`。本机可用 cron / launchd 每 5 分钟跑一次：

```bash
.venv/bin/python scripts/health_watch.py
.venv/bin/python scripts/health_watch.py --dry-run   # 只打印，不写状态、不发钉钉
```

```
*/5 * * * * cd /path/to/Agent_demo && .venv/bin/python scripts/health_watch.py
```

发送通道与催办相同（Webhook 或应用机器人）。未配置钉钉时告警打到 stderr 并退出码 1，方便 cron 寄信。

## 数据源配置

`hanli.env` 只由服务端读取，保存可写镜像库的 MySQL 配置；供应链代理凭据保存在被
`.gitignore` 排除的 `.env`，不要写入源码、部署日志或提交记录。实时数据库连接失败时
接口会明确报错，不会回退到旧快照，避免员工误把历史数据当成实时数据。

`.env` 在进程导入时只读一次，改完必须重启服务才生效，且进程环境变量优先级高于 `.env`
（`setting()` 的口径）。启动日志会写出这个进程实际连的库
（`镜像库：hanli.env → 主机:端口/库名`）；如果某个键被 shell 里残留的 `export` 盖掉，
下一行会列出被覆盖的键名，用 `unset` 或换个新终端即可。握手阶段就失败时错误里会写明连的是
哪个地址和哪个 env 文件，与「查询中途断流」区分开——两者在 PyMySQL 里都是 2013。

实时镜像按主表/明细表/主数据表维护：

- `realtime_purchase_orders`：采购单号、采购日期、供应商名称、采购员、状态。
- `realtime_purchase_order_items`：产品名称、SKU、规格、交期、采购数量、已入库数和金额。
- `realtime_orders`：ERP 内部订单号、线上单号、订单日期、状态、店铺和买家标识。
- `realtime_order_items`：订单 SKU、款式、名称、规格、数量和订单侧图片地址。
- `realtime_products`：SKU/款式、名称、规格、分类、品牌、单位、价格、供应商、启停状态和大小图 URL。
- `realtime_suppliers`：供应商名称、启停、联系人、电话、地址、银行账户、税务与账期信息。
- `realtime_sync_state`：上述各数据源的增量水位、最近成功时间、请求 ID 和错误。
- `gb_standards`：全国标准信息公共服务平台（std.samr.gov.cn）的国标目录元数据，供合同选国标。

商品接口首次接入会优先批量补齐订单/采购明细引用过的 SKU，再按修改时间持续增量；接口未返回的
历史已删除/停用 SKU 仍由订单和采购明细兜底。供应商首次全量分页，后续按修改时间增量。
商品图片在主数据表保存 URL，并继续缓存到 `files/data/product-images/`；原始大图不作为 BLOB 写进高频业务表。

换货页订单选择器读取：

- `realtime_orders`：内部订单号、线上单号、日期、状态和店铺。
- `realtime_order_items`：订单 SKU、款式、名称、规格和数量。

`realtime_sync_state` 单独记录采购单、订单、商品和供应商的成功水位、最近 `request_id` 和错误。增量任务
默认每 60 秒运行一次，回看 5 分钟覆盖边界更新，API 调用不超过代理的 60 次/分钟限制。
服务启动后默认错峰 30 秒再执行首轮同步，避免与首次看板年度数据加载同时压远程镜像库。
修改 `.env` 中 `REALTIME_SYNC_*` 可调整周期、初次回溯天数、分页和窗口。订单接口每页最多
按 50 条请求；可用 `SUPPLY_API_CLIENT_SECRET_FILE` 引用被 `.gitignore` 排除的本地 Secret
文件，避免把凭据直接写进配置模板。

镜像库里的历史数据已经灌好，之后全部由 API 增量维护，没有第二个数据入口。要手工补一段
窗口（例如常驻服务停过一段时间，回溯超过 `REALTIME_SYNC_INITIAL_DAYS`）时不经 HTTP 跑：

```bash
.venv/bin/python scripts/sync_realtime_mirror.py --source all
.venv/bin/python scripts/sync_realtime_mirror.py --source purchase --since 2026-07-01T00:00:00+08:00
```

国标目录不走供应链代理，而是读 `https://std.samr.gov.cn/` 高级检索的公开 JSON
（`/gb/search/gbAdvancedSearchPage`），只入库标准号、名称、状态、ICS/CCS、发布/实施日期等
元数据，**不下载标准全文**。同一条以 SAMR `id` 为主键；`content_hash`（含平台
`OPEN_HASH_CODE` 与状态、名称、日期）不变则只刷新 `last_seen_at`，有变化才改行并记
`last_changed_at`。水位写在 `realtime_sync_state.source_name = gb_standards`。

默认范围由 **商品表 `realtime_products.category`** 决定：分类先映射到
`files/config/gb_category_map.json` 里的目录族（服装 Y76/Y75/W63、鞋类 Y78、玩具 Y57 等），再对
这些族的 CCS/ICS/关键字做并集。空分类、`其他`、一件代发、线下订单会忽略。未映射的分类会
打日志并跳过，不按分类名模糊检索（「毛绒」会误中毛纺纤维标准）。合同选国标时按目录族关联
表 `gb_standard_families` 过滤。钉钉机器人和网页助手共用只读工具 `gb_catalog_status`（目录
是否同步、各状态/目录族条数）和 `lookup_gb_standards`（按 SKU / 名称 / 分类 / 标准号给出
执行标准候选）；助手按分类映射查库，不编造标准号，也不把商品条码当成国家标准。全量国家
标准目录约 8 万条，用 `--scope all` 或 `GB_SYNC_SCOPE=all`。手工指定分类号用 `--scope filtered`。
`GB_SYNC_ENABLED=true` 时每天 `GB_SYNC_TIME`（默认 02:30）增量同步；失败按镜像同步同样的
指数退避，封顶 900 秒，不会每 30 秒打 SAMR。同步成功后若某条标准的状态变成
**即将实施 → 现行**或**任意 → 废止**，且该标准已写在 `contract_line_gb`（合同页选过），
会推一条钉钉 markdown（同一标准同一天只发一次）。名称改了但状态没变不推，避免误报。
合同页候选项旁有现行 / 即将实施 / 废止角标；已选标准被废止时仍会出现在下拉里并标红，
生成合同时仍会中止。

```bash
.venv/bin/python scripts/sync_gb_standards.py
.venv/bin/python scripts/sync_gb_standards.py --dry-run
.venv/bin/python scripts/sync_gb_standards.py --scope filtered --ccs Y57 --ics 97.200.50
.venv/bin/python scripts/sync_gb_standards.py --scope all
```

若同步返回“接口路由不存在或当前 Client 未授权”，需要在供应链代理后台为该 Client 开通
`orders.search` 和 `purchase.orders.query`；用响应中的 `request_id` 可定位审计日志。授权完成
后无需修改代码，常驻服务会自动重试。

订单镜像从 `orders.search` 的嵌套 `items` 同步商品明细和 `pic` 图片地址，并把有效图片缓存
到 `files/data/product-images/`。该接口当前不包含淘系和拼多多订单；如需覆盖这两类平台，需要代理
侧另行开放相应订单接口。

首次打开默认查询本年度 1 月 1 日至服务器当天，排除取消、删除和合并单；切换年度时才查询历史年度。
接口结果缓存 30 秒，避免两个页面在短时间内重复压库。

如需把 CSV 手工导入另一个测试数据库，可显式提供目标数据库配置：

```bash
.venv/bin/python scripts/sync_purchase_data.py --env /path/to/test-database.env
```

## 文件

| 文件 | 作用 |
|---|---|
| `frontend/src/` | React 单页应用：五个页面、共用 API 客户端、payload 解码与设计令牌 |
| `frontend/js/exchange-worker.user.js` | 聚水潭页面里的油猴 worker，不属于单页应用 |
| `frontend/js/jst-order-exchange.core.js` | 页内换货核心（plan/execute），油猴与 Codex 共用 |
| `scripts/jst_exchange_call.mjs` | 生成注入片段 / 可选 CDP 调用 plan |
| `backend/` | 实时数据库查询、看板 API 与采购合同服务 |
| `backend/agent/` | Agent Core：工具注册表、工具循环、确认状态机、会话与审计 |
| `backend/forecast/` | `Forecaster` 接口、Baseline 实现、模型工件版本与订货建议计算 |
| `backend/dingtalk/` | 钉钉发送、身份映射、Stream 客户端与每日催办推送 |
| `backend/delivery_reminders.py` | 四波催办口径，台账页 / Agent 工具 / 钉钉推送共用 |
| `backend/gb_standards.py` | 国标目录同步：std.samr.gov.cn → `gb_standards` 表 |
| `backend/logging_setup.py` | 统一日志：东八区时间 / 级别 / 模块，可选落 `files/data/app.log` |
| `backend/health_watch.py` | `/api/health` 评估与告警去重；CLI 在 `scripts/health_watch.py` |
| `scripts/` | 合同生成、模型训练、Agent 调试、国标同步与健康巡检 |
| `files/` | 本地文件根：config 主数据、templates 合同母版、data 运行时、outputs 生成物 |
| `files/data/snapshots/` | CSV 数据快照 |
| `files/templates/采购合同模板.xlsx` | 固定栏空白母版（需方 / 收货信息 / 包装 / 检验 / 条款） |
| `files/config/buyers.json` | 需方、仓库、送货与验收信息 |
| `files/config/供应商管理.xlsx` | 本机维护的供应商主数据（不上库，不进版本库）；合同按 ERP 简称匹配 |
| `files/config/internal_suppliers.json` | 公司内部户名单；合同不列收付款信息 |
| `files/config/contract_mappings.json` | 合同映射表：发票类型、付款方式条款与 ERP 预选 |
| `files/config/suppliers.json` | 没有供应商管理表时的回退；离线用例仍用它 |
| `files/config/gb_category_map.json` | 商品分类到国标目录族（CCS/ICS/关键字）的映射 |
| `docs/` | 架构、路线与外部接口参考；索引见 `docs/README.md` |
| `tests/fixtures/` | 聚焦测试夹具 |
| `files/outputs/` | 生成的合同文件，不提交版本库 |
| `hanli.env` | 本地可写实时镜像数据库配置（已忽略，不提交） |
| `legacy/` | 已下线的旧 HTML 页面与离线数据生成器，仅供对照，可删 |

## 采购合同生成

访问 `http://127.0.0.1:8777/contract`，可按采购单号、供应商或采购员搜索并选择实时采购单，选择后自动展示单头与商品明细。不开票和普票默认税率为 0%，专票默认 13%，员工仍可手动调整；确认税率、每个 SKU 的合同单价，以及可选的执行标准后，必须先生成真实合同预览，核对后才能下载 Excel。Excel 由服务端 openpyxl 直接写入；预览仍把真实 XLSX 交给办公套件渲染，以便显示嵌入的商品图片。

生成器自动完成：

- 从实时 ERP 主表填写下单日期、采购单号、供应商简称、采购员；表头日期、需方、供方、收货信息居中。需方侧 A6:A7 合并为「收货信息」，B6:H7 默认「鄂州仓：湖北省鄂州市华容区葛店镇电商大道8号蓝库电子商务有限公司1库1号4号门，收货人：蜀黍家收货组，13385711803」，合同页可手改；同一段文字写入第 14 行「送货地址」（一条明细时正好是 14A:14P）。
- 从实时明细表填写款式、SKU、品名、数量和交货日期；多交期时取最晚日期作为整单交货期限。Excel 列序是国标码 → 品名 → 执行标准。明细备注写在 Q 列（P 列表头保留但不写内容）；预览打印区仍是 A–P。
- 单价优先用员工手填，其次解析备注里**第一个数字**（如「包体32+2个魔术贴标3.45」→ 32），再才是 `products.json` 该票种价或 ERP 价（票种匹配 / 内部户）。
- 从本机 `files/config/供应商管理.xlsx`（`backend/supplier_master.py`）按供应商简称补齐供方全称、地址、联系人、票种税率和收款账户（付款账户名 / 开户行 / 账户）。同一简称多行时只留**创建时间最近**的一条。这张表是 ERP「供应商管理」导出，**不写入镜像库**；员工覆盖文件即可，服务按修改时间重读。没有该文件时回退 `files/config/suppliers.json`。冻结供应商、缺全称/地址/联系人电话时中止。发票类型只认「专用发票 / 普通发票 / 不开发票」等已映射原文，`(0%)` 不猜票种。
- 公司内部户（`files/config/internal_suppliers.json`，现为蜀黍家 / 蜀黍家毛绒组装加工 / 蜀黍家辅料供应商）单独一类：不要求 Excel 全称和收付款账户，合同付款方式写「内部往来，不列收付款信息」，单价可用 ERP 价。要增删内部户只改这份 JSON。
- 合同页给两处历史参考：每行商品显示**这家供应商这颗 SKU 的上次采购价 / 日期 / 单号**（取自镜像库采购明细，不含本单，点「采用」直接填进单价框）；付款方式按**这家供应商上次用过的条款**预选并标注来源。上次选择记在本机 `files/config/payment_history.json`（不进库、不进版本库），预览和下载都会更新。
- 付款方式（合同「付款方式」栏）由员工在合同页选，条款正文维护在 `files/config/contract_mappings.json` 的 `payment_options`：3/7、发货前付款、到仓后付款、月度结算，另有「手动输入」可自己写（上限 500 字）。**下拉里的括号标签只用于选择，写进合同的只有条款正文**，后面再跟采购单号，以及映射表里的付款账户名 / 开户行 / 账户（内部户不列）。ERP 单头 `payment_method` 只决定默认预选（`MonthlyStatement` → 月度结算，`CurrentSettlement` / `CashOnDelivery` → 到仓后付款），该字段为空时不预选，必须自己选一条才能预览。同一份文件还维护「发票类型」原文到票种的映射，改完不必重启。
- 检验标准默认两条（到仓无破损、数量一致）；合同页可加行，从 3 起编号，已带序号的不再加前缀。空白母版见 `files/templates/采购合同模板.xlsx`（只含固定栏，不含采购单内容）。
- 从 `files/config/products.json` 补齐**国标码（商品条码）**、分类、材质工艺、包装、单位、三类价格和商品图片。该文件没维护到的 SKU，**单位、分类、名称、国标码、虚拟分类**退到镜像库 `realtime_products`（条码取接口 `sku_code`，虚拟分类取 `vc_name`）；**单价不从商品主数据兜底**，顺序是手填 → 备注首个数字 → 配置价 → ERP 价，都没有就中止。单位在两处都查不到、或商品资料行在但单位为空时才报错。合同页会直接显示单位和条码，不必点预览才发现缺单位。
- 按商品表 `realtime_products.category`（缺则用产品配置分类）对照 `files/config/gb_category_map.json`，从 `gb_standards` 列出该目录族下现行 / 即将实施的**执行标准**（GB/T…）。合同页是一个合并输入框：点开下列出按商品信息排好的推荐（分类排序，配置了模型则再由 LLM 从候选里挑，**不能编造目录外编号**，也不自动选定）；输入 `GB/T 36` / `9832` 按标准号前缀查 `/api/contracts/gb/search`，输入「毛绒」按名称查。未选不阻止生成。选中结果写入镜像库 `contract_line_gb`（按采购明细 `poi_id`），Excel 单独占一列，不覆盖国标码。候选项带状态角标；已废止的已选标准会标出来，但不能再生成进合同。`CONTRACT_GB_AI` 默认开（复用 `AGENT_*` 模型配置，不依赖 `AGENT_ENABLED`）；`CONTRACT_GB_WEBSEARCH` 默认关，打开后会向国标平台按品名补检索，命中仍必须落在本地目录里。
- 根据员工选择更新单价表头、合同第 4 条票种/税率、商品小计、总金额及付款方式中的采购单号。

采购看板底部按采购单实际建立时间倒序展示最近 20 单，并提供“生成合同”入口；到货预警和完整交期清单统一放在交期提醒台账，避免两个页面重复。合同入口会携带采购单号打开合同页并自动载入对应订单。合同预览由真实 XLSX 经办公套件渲染，因此与下载文件使用相同数据，并能显示嵌入的商品图片。

系统先使用 `files/config/products.json` 的 `image_path`；API 采购/订单明细如果返回 `pic300` /
`pic160` / `pic100` / `pic` 等图片 URL，同步器会校验文件类型并按 SKU 缓存到
`files/data/product-images/`。如果代理接口没有返回图片字段，仍可在合同页创建图片同步任务，
由已登录聚水潭页面的 Worker 取图。ERP Cookie 始终留在浏览器，后端和合同文件都不会保存
Cookie；后续合同直接使用本地缓存。缺少供应商完整映射（含本机表未命中、已冻结、缺地址/联系人）、税率或所选票种价格时，系统会
阻止生成，避免带占位信息的合同流出。执行标准未选可以生成；填了目录里不存在或已废止的标准号
则会中止。国标码（商品条码）和执行标准（GB/T…）是两列，不要混用。

## Agent / 钉钉机器人合同接口

配置 `.env` 中的 `AGENT_API_TOKEN` 后，机器人或 Agent 使用 Bearer Token 调用：

- `GET /api/agent/contracts/orders?q=604264`：按单号、供应商或采购员查找订单。
- `POST /api/agent/contracts/generate`：传入 `purchaseOrderNo`、`invoiceType`、可选 `taxRate`、`priceOverrides` 与 `gbOverrides`（`poiId → 执行标准号`），返回合同 ID、预览地址和下载地址。
- `GET /api/agent/contracts/{contractId}/preview`：获取含商品图片的 PNG 预览。
- `GET /api/agent/contracts/{contractId}/file`：下载最终 Excel。

接口没有配置 Token 时保持关闭；Agent 应先查找并确认唯一订单，再生成合同，避免仅凭模糊名称直接落单。

Agent 或命令行也可调用同一能力：

```bash
.venv/bin/python scripts/generate_purchase_contract.py \
  --po-id 604264 \
  --invoice-type special_invoice \
  --output files/outputs/采购合同-604264.xlsx
```

## 采购助手（Agent）

访问 `http://127.0.0.1:8777/chat`，填入 `AGENT_API_TOKEN` 和与 `staff_bindings` 一致的
钉钉/采购员姓名即可对话。只读查询不校验姓名；生成合同、换货、发催办必须能对上绑定表。
模型只负责理解意图、补参数、选工具和组织话术；**查库、算数、生成文件、外发消息全部由
确定性代码完成**，模型不能生成 SQL，也不能改动工具返回的任何数字。

开关：`.env` 里 `AGENT_ENABLED=true`，再配模型。`AGENT_PROVIDER=openai_compatible`（缺省）要
`AGENT_API_BASE` / `AGENT_API_KEY` / `AGENT_MODEL`；`codex_oauth` 则复用本机 ChatGPT 登录，
见下方「模型」节。未启用时 `/api/agent/chat` 返回 503，其余页面不受影响。

### 工具与风险分级

| 工具 | 级别 | 做什么 |
|---|---|---|
| `search_purchase_orders` | L0 只读 | 按单号 / 供应商 / 采购员搜本年度采购单 |
| `get_purchase_order` | L0 只读 | 单头 + 全部明细：交期、数量、已入库、待入库、单价 |
| `delivery_reminders` | L0 只读 | 四波催办清单（口径同交期台账页） |
| `dashboard_summary` | L0 只读 | 金额、数量、入库率、采购员/供应商/品类 Top |
| `search_products` | L0 只读 | 商品主数据里的 SKU（含分类） |
| `gb_catalog_status` | L0 只读 | 国标目录库同步状态与条数 |
| `lookup_gb_standards` | L0 只读 | 按 SKU / 名称 / 分类 / 标准号查执行标准（GB/T…） |
| `master_data_gaps` | L0 只读 | 近 N 天采购的供应商未维护 / SKU 无图 / 票种缺价 / 分类未映射国标 |
| `forecast_demand` | L0 只读 | 逐日销量预测 p50 与 p10/p90 区间 |
| `order_suggestion` | L0 只读 | 订货建议（确定性公式，见下） |
| `generate_purchase_contract` | **L1 生成产物** | 生成合同 Excel + 预览，先给要点再确认 |
| `submit_exchange_dry_run` | **L1 生成产物** | 登记换货 dry-run 任务（真实换货仍需在换货页二次确认） |
| `locate_insole_orders` | L0 只读 | 定位抖音旧鞋垫订单；半码按码数舍去小数后映射目标 SKU |
| `process_insole_orders` | **L2 对外动作** | 按清单串行换鞋垫；确认前必须展示订单信息 |
| `send_delivery_reminder` | **L2 对外动作** | 催办清单发到钉钉群并 @ 采购员 |

`send_delivery_reminder` 只在钉钉发送可用时注册，所以关掉钉钉时 `/api/agent/status`
的工具清单里不会出现它。钉钉群里问国标目录或某商品对应标准，走的是同一套 L0 工具，不另开入口。

L0 直接执行；**L1/L2 一律不直接执行**：先落一条 `pending_actions`（默认 30 分钟有效），
渠道渲染要点，由**发起人本人**确认后以 `pending_action_id` 为幂等键执行且只执行一次。
重复确认回放已有结果，超时 / 取消后不可再执行。网页 `/chat` 和台账「发送提醒」的
`operator` 必须能对上 `staff_bindings` 里的钉钉/采购员姓名（花名或「真名（花名）」均可）；
对不上时只读不拦，L1/L2 拒绝登记和确认。钉钉渠道仍按 userId 识别，不走这道姓名校验。
对话层另有黄金回放夹具 `tests/fixtures/golden_dialogues.json`，CI 用假 LLM 按脚本跑
`tests/test_agent.py` 的 `GoldenReplayTests`。

`supplier_scorecard`（供应商绩效）在 `RESERVED_TOOLS` 占位，**迁专用机器之后**再注册实现，
不改 Agent Core。`price_watch` / `inventory_watch` / `create_purchase_draft` 已取消。

### 接口

全部使用 Bearer `AGENT_API_TOKEN`；未配置 Token 时返回 503（功能关闭），Token 不对返回 401。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/agent/chat` | `{message, sessionKey, operator}` → 回复 + 工具步骤 + 待确认动作 |
| POST | `/api/agent/actions/{id}/confirm` | 确认执行一条 L1/L2 动作 |
| POST | `/api/agent/actions/{id}/cancel` | 放弃 |
| GET | `/api/agent/actions?session_id=` | 当前待确认动作 |
| GET | `/api/agent/status` | 模型、工具清单、预测工件、钉钉状态 |
| GET | `/api/agent/reminders?bucket=&buyer=&limit=` | 催办清单（不经模型，可直接给别的系统用） |
| POST | `/api/agent/reminders/push` | 立即推送催办。`{today, buyer, buckets, operator}`；不带 buyer 与定时任务同一日幂等键，带采购员则另开 `-web-{buyer}`。网页操作人须在 `staff_bindings` 中 |
| GET | `/api/agent/audit/runs`、`/api/agent/audit/tools` | 对话与工具调用审计 |
| GET | `/api/agent/staff`、POST 同路径 | 采购员 ↔ 钉钉 userId / 手机号绑定 |
| POST | `/api/forecast/predict` | `{keys, horizonDays}` → 逐日 p50/p10/p90 |
| POST | `/api/forecast/order-suggestion` | `{keys, leadTimeDays, serviceLevel, inventory}` → 建议单 |
| GET | `/api/forecast/status`、POST `/api/forecast/reload` | 模型工件版本与重载 |

命令行调试（不经 HTTP，与网页共用同一个实例和同一套确认流）：

```bash
.venv/bin/python scripts/run_agent_cli.py --operator 张三
.venv/bin/python scripts/run_agent_cli.py --status
```

会话里 `/确认 <id>`、`/取消 <id>`、`/待确认`、`/工具`、`/退出`。

### 审计

每轮对话、每次工具调用、每条待确认动作、每次订货建议、每次外发通知都落在 Agent 业务库
（`AGENT_DATABASE_PATH`，默认 `files/data/agent.sqlite3`，已 gitignore）：
`agent_sessions` / `agent_messages` / `agent_runs` / `tool_executions` / `pending_actions` /
`forecast_runs` / `staff_bindings` / `notification_deliveries`。

## 销量预测与订货建议

预测数字由模型工件给出，**LLM 只解释不改数字**。建议是确定性公式：

```
建议下单量 = 交期内预测需求(∑p50) + 安全库存(z×√∑σ²，σ 由 p90−p10 折算)
           − 可用库存 − 在途待入库
建议下单日 = 需求缺口出现日 − 供应商交期 − 缓冲天数
```

三项输入的现状：

| 输入 | 来源 | 现状 |
|---|---|---|
| 预测需求与区间 | 模型工件 `FORECAST_MODEL_DIR/<version>/` | 仓库里只有占位的 `BaselineForecaster`（移动平均 + 星期季节因子） |
| 在途待入库 | 现有采购明细：数量 − 已入库，排除已取消单 | **已可用** |
| 可用库存 | 现势库存表 `FORECAST_INVENTORY_TABLE` | **还没有数据源**，缺失时报错说明缺哪些 SKU，不用 0 兜底 |

所以在销售表和库存表进实时库之前，订货建议只能带显式 `inventory` 参数做验证。
先用导出的 CSV 训练打通链路：

```bash
# CSV 至少要有 SKU、日期、数量三列，列名和默认不同时用 --key-field/--date-field/--qty-field 指定
.venv/bin/python scripts/train_forecast_model.py --csv 销量明细导出.csv --holdout-days 14
```

**接入自己训练好的模型**：继承 `Forecaster` 实现 `fit` / `predict`，训练时加
`--forecaster 模块:类名`。工件的 `metadata.json` 会记下这个引用，服务端据此加载，
调用方一行都不用改。完整说明和约束见 `docs/预测.md`。

## 钉钉机器人

默认全关。两个开关互不依赖：催办可以先上，对话等 Agent 开了再开。

### 今天要配的东西（企业内部应用 + Stream）

1. 钉钉开放平台建**企业内部应用**，开通「机器人」能力，消息接收模式选 **Stream**（不要用 HTTP 回调，本机没有公网）。
2. 把应用发布，把机器人拉进采购群。
3. 填 `.env`：

```bash
DINGTALK_ENABLED=true
DINGTALK_CLIENT_ID=         # AppKey
DINGTALK_CLIENT_SECRET=     # AppSecret
DINGTALK_ROBOT_CODE=        # 一般等于 AppKey，控制台里有单独的 RobotCode 就填那个
DINGTALK_GROUP_CONVERSATION_ID=   # 群的 openConversationId，催办 @ 人必须有
```

`dingtalk-stream` 已在 `requirements.txt`。重启 `server.py` 后日志应出现「钉钉 Stream 已启动」。
长连断开后监督线程会重建客户端（30 秒起指数退避，封顶 10 分钟），`/api/health` 里
`dingtalk.stream.restartCount` / `lastError` 能看到重连次数。进程退出时会 `stop()`。

4. 群里 @机器人 发 `绑定 利特`。ERP 里同一人经常还有「真名（花名）」，例如「李佳冬（利特）」：
   绑花名或全称任一即可，催办 @ 会视为同一个人；也可以一次 `绑定 利特、李佳冬（利特）`。
   命令行同样支持顿号分隔：

```bash
.venv/bin/python scripts/run_dingtalk_cli.py status
.venv/bin/python scripts/run_dingtalk_cli.py bind --buyer '利特、李佳冬（利特）' --mobile 13800000000
.venv/bin/python scripts/run_dingtalk_cli.py send-test
.venv/bin/python scripts/run_dingtalk_cli.py remind-now
```

手机号反查 userId（应用机器人）：`resolve-mobile --mobile 138...`。种子文件可从
`files/config/staff_bindings.example.json` 复制为 `files/config/staff_bindings.json`（不进版本库）。

未绑定的人可以问只读问题（含国标目录状态、某商品对应执行标准、主数据缺口），但网页 L1/L2
（生成合同、登记换货、发催办、确认 pending）对不上 `staff_bindings` 就拒绝。会话按
`conversationId + senderId` 隔离；同一条钉钉消息 ID 只处理一次。确认回复「确认 编号」。
群里可直接问「国标库同步了吗」「毛绒小熊用什么国标」；机器人走 `gb_catalog_status` /
`lookup_gb_standards`，不会编造标准号，也不把商品条码当成国家标准。

### 催办推送（不依赖大模型）

- `DINGTALK_REMINDER_ENABLED=true`：每天 `DINGTALK_REMINDER_TIME`（默认 08:30）把四波催办
  清单发到群里并 @ 对应采购员。@ 到人靠 `staff_bindings` 的 userId（应用机器人）或手机号
  （Webhook 机器人）。同一天**成功后**不再重发；失败会按 15 分钟间隔最多再试 3 次，
  仍失败则等次日，错误出现在 `/api/health` 的 `dingtalk.reminder.lastError`。
  手动 `POST /api/agent/reminders/push` 只认当日已成功记录，失败后仍可立刻再推。
  交期台账「发送提醒」走同一接口：确认弹窗后按当前采购员筛选和档位推送；操作人姓名须与
  `staff_bindings` 一致（与 `/chat` 共用 sessionStorage 里的 Token / 姓名）。全量已推过后
  会提示「当日已推」；只筛某个采购员时用独立幂等键，早上群发后仍可再催一个人。
- 只有 Webhook、没有应用机器人时，填 `DINGTALK_WEBHOOK_URL` / `DINGTALK_WEBHOOK_SECRET` 也能发催办，
  但不能 Stream 对话。

## 模型：DeepSeek 或 Codex OAuth

缺省 `AGENT_PROVIDER=openai_compatible`，用 `AGENT_API_BASE` + `AGENT_API_KEY`（现在 `.env` 里是 DeepSeek）。

也可以像 OpenClaw 一样吃 **ChatGPT 计划里的 Codex 额度**，不走 `api.openai.com` 按量 Key：

```bash
AGENT_ENABLED=true
AGENT_PROVIDER=codex_oauth
AGENT_MODEL=gpt-5.6-sol
# AGENT_API_KEY 可以留空。登录文件默认 ~/.codex/auth.json（本机已有 ChatGPT 登录即可）
# AGENT_CODEX_AUTH_FILE=   # 只有当文件不在默认位置时才填
```

实现与 OpenClaw 同通道：`chatgpt.com/backend-api/codex/responses` + `chatgpt-account-id`。
刷新 token 会写回同一个 `auth.json`（文件锁），避免和 Cursor / Codex 各刷一次把对方踢下线。
额度是订阅窗口（常见 5 小时 + 每周），用尽接口会 429，把 `AGENT_PROVIDER` 改回 `openai_compatible` 即可。
这不是官方「给任意业务系统用的 SLA」；生产对话量大时仍建议用 DeepSeek 这类按量接口。

## 页面集成接口

`window.ProcurementAdapters.dataSource` 提供 `getDashboardData(context)` 和
`getDeliveryData(context)`；优先读取同源后端 API，后端不可达时回退本地
`PO_DATA` / `DELIV`。`window.ProcurementAdapters.reminder.send(batch)` 接收结构化催办单；
接入 Agent 后设置 `configured: true`，由后端解析采购员和供应商的邮箱或群聊目标。
密钥、数据库凭证和邮件凭证必须保存在服务端，不得写入静态页面。

## 口径

所有指标都从明细行现算，筛选条件一变，上面的数字、图、表一起重算。

- **采购金额** = `基本金额`（= 数量 × 基本售价，已全量校验一致）
- **已入库** = `item_in_qty`（与 `已入库数量` 列完全一致，只是空值补 0）
- **待入库** = 数量 − 已入库，按行取正数后汇总
- **逾期** = `delivery_date` 早于服务器当天，并且该明细仍有待入库数量
- **到货期限（提醒口径）** = `最早预计到货日期` − 今天，只算**还有待入库数量**的明细行
  （已入库完的行没有期限可催）。档位互斥，正好对上四波提醒：

  | 档位 | 剩余天数 | 对应提醒 |
  |---|---|---|
  | 已逾期 | < 0 | 超时 |
  | 剩 0–1 天 | 0 ~ 1 | T-1 |
  | 剩 2–10 天 | 2 ~ 10 | T-10 |
  | 剩 11–20 天 | 11 ~ 20 | T-20 |
  | 剩 20 天以上 | > 20 | 暂不提醒 |
  | 未排期 | 无到货日 | 催不出期限，先补日期 |

  采购单的期限 = 该单所有待入库行里**最早**的到货日，档位取**最急**的一档。
- **尺码**：服装取号型的身高段（`175/92B` → 175）；鞋类统一折成鞋码
  （`42码` → 42，鞋垫毫米 `280(2.5)` → 46，按 码 = 毫米 ÷ 5 − 10）
- **主品类**：一张采购单里出现的第一个品类，多于一个时标 `+n`

数据本身的两个坑，看板里如实呈现、没有抹平：

1. **待入库数量大量没有预计到货日期**，所以"到货计划"做不成时间轴，
   改成了「已逾期 / 排期内 / 未排期」三段构成。
2. 供应商名称取实时主表 `seller`。
3. 预计到货日覆盖率和最远日期随镜像库变化；某档为 0 是数据里没有排到那么远的到货计划，不是算错。

## 看板结构

- **左侧台账栏**：采购金额（英雄数字）+ 近 12 个月走势 + 单量/行数/数量/入库率/待审核/供应商
- **采购金额走势**：日 / 周 / 月切换，十字准星读数
- **品类采购金额**、**各品类入库进度**（仪表条，直接标注入库率）、**采购员金额 Top 8**
- **尺码曲线**：行 = 采购量前 10 的商品，列 = 号型或鞋码，格子按该商品自身采购量归一，
  看的是码段结构。服装看身高段，鞋类看鞋码。
- **待入库构成**
- **最近采购单**：按聚水潭采购单建立时间倒序显示最近 20 单；点行查看商品明细，或直接
  携带单号进入合同生成页。到货预警、催办清单与完整采购单交期明细统一放在交期提醒台账。

每张图右上角都有「图 / 表」开关，表视图给的是同一份数据的精确数值。
顶部筛选行（采购日期 / **到货期限** / 状态 / 品类 / 采购员 / 搜索）统一约束下方所有内容。
两个日期筛选是两回事：**采购日期**筛的是下单时间，**到货期限**筛的是催货用的剩余天数。
所有图表和最近采购单都跟随当前筛选切片。

---

# 交期提醒台账

`/ledger`（`frontend/src/pages/ledger/`）—— 一页只干一件事：把每张采购单的
**单号 / 采购日期 / 供应商 / 产品信息 / 交期 / 入库数量 / 采购员** 摆平，
按交期分四波催。

## 交期取哪个字段

**这页和采购看板的日期口径不一样，是有意的。**

| 字段 | 含义 | 覆盖 | 最远排到 |
|---|---|---|---|
| `item_delivery_date` | 和供应商约定的**交期** | 69.1%（4290/6208 行） | 2026-09-30 |
| `最早预计到货日期` | 预计到货日 | 50.9%（3157/6208 行） | 2026-08-11 |

两列都有值的 2388 行里，只有 20 行相同 —— 它们本来就是两回事。
采购看板走后者，所以 README 上面那条「T-10 / T-20 三档现在都是 0」的坑成立；
这页走前者，四波就都填上了。

取值顺序：该行 `item_delivery_date` → 空则退到 `最早预计到货日期` → 都空算未排期。
表里凡是退过一档的，交期下面标一行小字 `预计到货`，不掺着糊弄。
401 单里 **214 单用交期，50 单回退，137 单两个都没有**。

## 四波提醒

一张单的交期 = 所有**待入库**行里最早的那个（已入库完的行没有期限可催）；
波次取最急的一档。第 n 波的触发日直接由交期倒推，表里那四个圆点就是这个：

| 波次 | 触发日 | 剩余天数 | 该干什么 |
|---|---|---|---|
| 第 1 次 · T-20 | 交期 − 20 天 | 11 ~ 20 | 确认排产进度 |
| 第 2 次 · T-10 | 交期 − 10 天 | 2 ~ 10 | 确认发货计划 |
| 第 3 次 · T-1 | 交期 − 1 天 | 0 ~ 1 | 核对物流单号 |
| 第 4 次 · 逾期催办 | 交期 + 1 天 | < 0 | 逐日追 |
| 暂不提醒 | — | > 20 | 还没进提醒窗 |
| 未排期 | — | 无交期 | 先补日期才催得动 |

圆点实心 = 那一波已到点，带圈 = 当前这一波。悬停看具体日期。

以 2026-08-10 为今天时：逾期 177 单 / T-1 8 单 / T-10 27 单 / T-20 13 单 /
暂不提醒 39 单 / 未排期 137 单，需催合计 **225 单 · 193,069 件**。

**「今天」可改**。默认取 payload `meta.today`（业务日，东八区）；
顶栏第一个日期框改一下，四波和整张表立刻跟着重排。

## 别的

- 档位卡点一下就是筛选，再点取消；卡片自己不受档位筛选约束，否则选中一档别的全归零
- 「按采购员的催办量」每人一条按波次堆叠，右端是需催量（前四波合计）
- 「导出催办清单」按当前切片导出，多给两列：**交期来源**（交期 / 预计到货）和
  **下次提醒日**，直接拿去发
- 「发送提醒」把当前采购员/档位的需催清单发到钉钉群（与定时催办同一口径）。需填写
  `AGENT_API_TOKEN` 和绑定过的姓名；当日已成功推过同一批会提示「当日已推」
- 点任意一行开抽屉：该单四波排期的具体日期 + 商品明细（颜色、规格、逐行交期与入库）
- 供应商名称取实时主表 `seller`

---

# 品控问题台账

钉钉群里用固定句式登记来货问题，当天 17:30（可配）把**当日登记**整理成 Excel 发回群里。
网页对话走同一套工具，不另开数据源。默认 `QUALITY_LEDGER_ENABLED=false`。

## 口径

- **字段可空**：供应商、采购单号、SKU 解析不出就留空，**不猜**。描述不能空。
  单号必须是 6 位以上数字且能在采购主表查到；SKU 必须像款号（字母+数字）；
  供应商必须是 `files/config/suppliers.json` 的键。
- **日报范围**：只含该自然日（东八区）登记、且状态不是「已撤销」的记录。
  已关闭的当日记录仍进表。历史未关闭问题只在摘要里报一个计数，不进当日 Excel。
- **指令**：`品控 …` / `品控关闭 <6位hex> [备注]` / `撤销品控 <6位hex>` /
  `品控查询 [今天|本周|未关闭|供应商]`。同一条钉钉 `message_id` 只登记一次。
- **发送**：应用机器人优先（markdown + 文件）；否则 webhook 带 7 天有效的签名下载链接
  （`/api/quality/reports/{YYYYMMDD}/{sig}.xlsx`，无 Bearer）。
- 空日默认 skip；`QUALITY_REPORT_EMPTY=notice` 才发「今日无登记」。
