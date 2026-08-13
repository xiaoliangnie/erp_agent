# 采购数据项目设计（第一阶段）

## 目标

把采购看板和交期提醒台账统一接到 MySQL，同时保持两个页面现有的筛选、图表、抽屉和 CSV 导出能力。浏览器永远不接触数据库账号或密码。

## 当前架构

```text
供应链安全代理 API（订单 / 采购单 / 可选图片）
        │ 增量分页，modified 水位 + 5 分钟重叠
        ▼
本地 MySQL 镜像（hanli.env，可写）
        │ po_id / o_id 关联主表 + SKU 明细表
        │ std.samr.gov.cn 国标目录 → gb_standards
        ▼
server.py ── /api/dashboard          ── React SPA /dashboard
    │     ├─ /api/delivery           ── React SPA /ledger
    │     └─ /api/contracts/generate ── React SPA /contract ── Excel
```

- `realtime_mirror.py`：通过安全代理分页同步采购单和订单，幂等写入四张规范化业务表，并记录独立成功水位。
- `gb_standards.py`：从 std.samr.gov.cn 高级检索 JSON 同步国标目录元数据到 `gb_standards`。默认同范围由 `realtime_products.category` + `config/gb_category_map.json` 决定，按 SAMR id 幂等，内容哈希不变则不改行；`gb_standard_families` 记录标准属于哪个商品目录族。
- `database.py`：读取 `hanli.env`，按 `po_id` 关联 `realtime_purchase_orders` 与 `realtime_purchase_order_items`；默认只查本年度 1 月 1 日至今天。
- `sync_purchase_data.py`：按需把 CSV 幂等写入显式指定的测试数据库，不参与生产运行链路。
- `server.py`：只使用实时数据库，只开放业务接口和 `frontend/dist/` 里的前端产物，不会把 env 文件或源 CSV 发布出去。
- `procurement_data.py`：统一维护字段口径和前端数据编码。
- `frontend/src/`：Vite + React + TypeScript 单页应用，五个页面是五条路由；`data/payload.ts` 是位置数组 payload 的唯一解码点。页面只走同源 API，离线快照回退已下线（旧实现留在 `legacy/`）。

## 采购合同生成

采购合同以 `templates/采购合同模板.xlsx` 为业务和视觉基准。Agent、网页和命令行共用 `backend/contracts.py`：读取 ERP 单头/明细后，按供应商简称合并 `config/suppliers.json`，按 SKU/款号合并 `config/products.json`，再调用电子表格生成器输出公式驱动的 Excel。

票种为 `no_invoice`、`normal_invoice`、`special_invoice`。员工选择后，系统同步决定税率、商品单价、单价表头和合同第 4 条；配置缺失时必须停止生成。图片解析顺序为产品映射文件、API 图片缓存和 ERP Worker 缓存。

合同明细把 **国标码**（EAN/商品条码，来自 `products.json`）和 **执行标准**（`GB/T …`，来自 `gb_standards`）分成两列。选项接口按商品分类对应的目录族列出现行 / 即将实施标准，不自动勾选；员工选择按采购明细 `poi_id` 写入 `contract_line_gb`，预览和下载都会保存。未选执行标准仍可生成合同；填了不存在或已废止的标准号则中止。

## 下一阶段

0. 商品资料与图片 URL 已通过 `items.query` 收进 `realtime_products`，供应商完整资料已进入
   `realtime_suppliers`；订单/采购引用过的历史 SKU 优先回填，图片继续使用本地读穿缓存。
   后续逐步用主数据兜底替换 `config/products.json`，细节见 `docs/商品资料与图片入库.md`。
1. 增加同步延迟监控、失败告警、数据质量检查和操作日志。
2. 给催办动作增加独立状态表，记录每一波提醒的发送结果。
3. Agent 代码已落地、默认关闭。下一刀按 `docs/AGENT_ARCHITECTURE.md` §13 阶段 6：
   工具结果压缩、黄金对话回放、`staff_bindings` 当身份枢纽、主数据缺口 L0 汇总。
   对话不是主入口；催办推送可单独开，不依赖大模型。
4. 部署时把数据库配置写入服务器密钥，不提交任何 env 凭证文件。
5. 合同选国标已接入：`/contract` 按目录族列出执行标准，选中值写入 `contract_line_gb` 并进 Excel「执行标准」列。钉钉/网页助手用 L0 工具 `gb_catalog_status`、`lookup_gb_standards` 查目录水位和商品对应标准。目录同步仍见 `gb_standards.py`。
