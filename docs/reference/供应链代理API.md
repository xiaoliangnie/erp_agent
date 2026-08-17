# 供应链项目 的 AI agent 接口使用说明

- 生成时间：2026/8/17 14:07:59
- 所属租户：公司内部（wjyf-inner）
- Client ID：`app_2fa315c7dff24df59b36`
- 代理基础地址：`https://api.wjyfek.com`

## 使用边界

- 只使用管理员发给你的 Client ID 和 Client Secret。
- 不要索要或填写数据库密码、服务器密码、第三方平台密钥。
- 请求 body 字段名按第三方平台开放文档填写，代理不会把业务字段改名。
- 代理负责鉴权、授权校验、签名、限流、审计和日志，业务人员不用接触平台密钥。
- 本文只维护常用/关键参数和可运行示例；完整字段、枚举、嵌套对象、条件必填规则以三方官方文档为准。

## 通用请求头

```text
X-Client-Id: app_2fa315c7dff24df59b36
Authorization: Bearer 你的 Client Secret
Content-Type: application/json
```

## 当前可调用接口

### 聚水潭（JUSHUITAN）

#### 聚水潭售后退货退款查询

- 接口编码：`jushuitan.aftersales.query`
- 权限标识：`aftersales.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/aftersales/query`
- 三方官方方法：`refund.single.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/refund/single/query 配置。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "start_time": "",
  "end_time": "",
  "date_type": "0",
  "wms_co_id": "",
  "shop_id": "",
  "as_ids": "",
  "so_ids": "",
  "is_get_total": "",
  "start_ts": 1,
  "end_ts": "",
  "is_offline_shop": "",
  "o_ids": "",
  "status": "",
  "good_status": "",
  "type": "",
  "archive": true,
  "order_types": "",
  "owner_co_id": "",
  "is_get_promotion": "false"
}
```

常用/关键参数说明：

| 参数 | 类型 | 是否必填 | 说明 | 示例 |
| --- | --- | --- | --- | --- |
| `page_index` | int | 必填 | 页索引，从 1 开始；用于分页查询售后单列表。；限制：聚水潭官方文档标记为必填。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `1` |
| `page_size` | int | 必填 | 页大小；每页返回数量。；限制：聚水潭官方文档标记为必填，最大 100。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `20` |
| `start_time` | string | 必填 | 开始时间；通常配合 end_time 按时间范围查询售后单。；限制：生产接口真实校验要求必填；时间格式建议 yyyy-MM-dd HH:mm:ss。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `end_time` | string | 必填 | 结束时间；通常配合 start_time 按时间范围查询售后单。；限制：生产接口真实校验要求必填；时间格式建议 yyyy-MM-dd HH:mm:ss。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `date_type` | int | 可选 | 时间类型：0=修改时间 modified，1=创建时间 created，2=确认时间 confirm_date；不填默认 0。；限制：官方文档：非必填，默认 0。；可选值：0、1、2；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `0` |
| `wms_co_id` | int | 可选 | 分仓 ID。；限制：可选过滤条件。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `shop_id` | int | 可选 | 店铺 ID。；限制：可选过滤条件；shop_id 为 0 且 is_offline_shop=true 时查询线下店铺单据。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `as_ids` | string | 可选 | 售后单号列表；多个编号按聚水潭文档示例用逗号分隔。；限制：可选过滤条件。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `so_ids` | string | 可选 | 线上单号列表；多个线上单号以逗号隔开。；限制：可选过滤条件。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `is_get_total` | bool | 可选 | 是否查询总条数。；限制：可选；大批量同步时可按性能需要关闭。；可选值：true、false；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `start_ts` | int | 可选 | ts 开始值；用于基于数据库行版本号增量同步。；限制：官方说明：使用 ts 查询可避免分页过程中因数据变动导致漏单。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `1` |
| `end_ts` | string | 可选 | ts 结束值。；限制：可选过滤条件。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `is_offline_shop` | bool | 可选 | 是否查询线下店铺单据。；限制：shop_id 为 0 且 is_offline_shop=true 时查询线下店铺单据。；可选值：true、false；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `o_ids` | string | 可选 | 内部单号列表；多个内部单号以逗号隔开。；限制：可选过滤条件。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `status` | string | 可选 | 售后单状态。；限制：WaitConfirm=待确认，Confirmed=已确认，Cancelled=作废，Merged=被合并。；可选值：WaitConfirm、Confirmed、Cancelled、Merged；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `good_status` | string | 可选 | 货物状态。；限制：BUYER_NOT_RECEIVED=买家未收到货，BUYER_RECEIVED=买家已收到货，BUYER_RETURNED_GOODS=买家已退货，SELLER_RECEIVED=卖家已收到退货。；可选值：BUYER_NOT_RECEIVED、BUYER_RECEIVED、BUYER_RETURNED_GOODS、SELLER_RECEIVED；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `type` | string | 可选 | 售后类型。；限制：按聚水潭售后类型枚举填写。；可选值：普通退货、其它、拒收退货、仅退款、投诉、补发、换货、维修；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `archive` | bool | 可选 | 是否查询归档订单。；限制：官方示例为 true。；可选值：true、false；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `true` |
| `order_types` | string | 可选 | 原订单类型；多个订单类型以逗号隔开。；限制：支持普通订单、补发订单、换货订单、天猫分销、天猫供销、协同订单、亚马逊货件单、唯品会 JITX 订单等，完整枚举以官方文档为准。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `owner_co_id` | int | 可选 | 货主编码。；限制：可选过滤条件。；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `` |
| `is_get_promotion` | bool | 可选 | 是否查询售后营收小计金额。；限制：官方文档：默认 false。；可选值：true、false；来源：https://open.jushuitan.com/document.aspx?doc_id=2356；核验时间：2026/7/30 | `false` |

> 说明：上表只覆盖已维护的常用/关键参数，不代表三方接口全部参数。完整参数请查看官方文档。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/aftersales/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"start_time":"","end_time":"","date_type":"0","wms_co_id":"","shop_id":"","as_ids":"","so_ids":"","is_get_total":"","start_ts":1,"end_ts":"","is_offline_shop":"","o_ids":"","status":"","good_status":"","type":"","archive":true,"order_types":"","owner_co_id":"","is_get_promotion":"false"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/aftersales/query
接口用途：聚水潭售后退货退款查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "start_time": "",
  "end_time": "",
  "date_type": "0",
  "wms_co_id": "",
  "shop_id": "",
  "as_ids": "",
  "so_ids": "",
  "is_get_total": "",
  "start_ts": 1,
  "end_ts": "",
  "is_offline_shop": "",
  "o_ids": "",
  "status": "",
  "good_status": "",
  "type": "",
  "archive": true,
  "order_types": "",
  "owner_co_id": "",
  "is_get_promotion": "false"
}
```

#### 聚水潭商品库存查询

- 接口编码：`jushuitan.inventory.query`
- 权限标识：`inventory.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/inventory/query`
- 三方官方方法：`inventory.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/inventory/query 配置。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "",
  "modified_end": "",
  "sku_id": "",
  "i_id": ""
}
```

常用/关键参数说明：

| 参数 | 类型 | 是否必填 | 说明 | 示例 |
| --- | --- | --- | --- | --- |
| `page_index` | number | 建议 | 页码，从 1 开始；用于分页查询。；限制：建议分页查询时填写。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `1` |
| `page_size` | number | 建议 | 每页数量；不要超过平台限制和后台授权范围。；限制：建议不超过 100。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `20` |
| `modified_begin` | string | 条件必填 | 修改开始时间；库存查询通常需要与 modified_end 配合，或填写商品编码/款式编码等过滤条件。；限制：时间格式通常为 yyyy-MM-dd HH:mm:ss。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |
| `modified_end` | string | 条件必填 | 修改结束时间；库存查询通常需要与 modified_begin 配合，或填写商品编码/款式编码等过滤条件。；限制：时间格式通常为 yyyy-MM-dd HH:mm:ss。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |
| `sku_id` | string | 条件必填 | 商品编码/SKU ID；用于按具体商品查询库存。；限制：与时间范围等查询条件至少满足平台要求之一。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |
| `i_id` | string | 条件必填 | 款式编码；用于按款式查询库存。；限制：与时间范围等查询条件至少满足平台要求之一。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |

> 说明：上表只覆盖已维护的常用/关键参数，不代表三方接口全部参数。完整参数请查看官方文档。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/inventory/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"","modified_end":"","sku_id":"","i_id":""}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/inventory/query
接口用途：聚水潭商品库存查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "",
  "modified_end": "",
  "sku_id": "",
  "i_id": ""
}
```

#### 聚水潭调拨单查询

- 接口编码：`jushuitan.inventory.transfer.query`
- 权限标识：`inventory.transfer.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/inventory/transfer/query`
- 三方官方方法：`allocate.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/allocate/query 配置。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/inventory/transfer/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/inventory/transfer/query
接口用途：聚水潭调拨单查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭商品查询

- 接口编码：`jushuitan.items.query`
- 权限标识：`items.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/items/query`
- 三方官方方法：`sku.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按当前聚水潭 RDS 凭证网关配置：请求入口 /api/open/query.aspx，系统 method=sku.query。该代理接口用于查询聚水潭普通商品/SKU 资料；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：默认范围
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 10,
  "modified_begin": "",
  "modified_end": "",
  "sku_id": "",
  "sku_ids": ""
}
```

常用/关键参数说明：

| 参数 | 类型 | 是否必填 | 说明 | 示例 |
| --- | --- | --- | --- | --- |
| `page_index` | number | 建议 | 页码，从 1 开始；用于分页查询。；限制：建议分页查询时填写。；来源：聚水潭开放平台文档；核验时间：2026/8/13 | `1` |
| `page_size` | number | 建议 | 每页数量。；限制：建议不超过 100，具体限制以聚水潭官方文档为准。；来源：聚水潭开放平台文档；核验时间：2026/8/13 | `10` |
| `modified_begin` | string | 条件必填 | 修改开始时间；与 modified_end 同时填写用于增量查询商品/SKU 资料。；限制：时间格式 yyyy-MM-dd HH:mm:ss。；来源：聚水潭开放平台文档；核验时间：2026/8/13 | `` |
| `modified_end` | string | 条件必填 | 修改结束时间；与 modified_begin 同时填写用于增量查询商品/SKU 资料。；限制：时间格式 yyyy-MM-dd HH:mm:ss。；来源：聚水潭开放平台文档；核验时间：2026/8/13 | `` |
| `sku_id` | string | 条件必填 | 商品 SKU 编码；用于按 SKU 精确查询。；限制：与时间范围等查询条件至少满足平台要求之一。；来源：聚水潭开放平台文档；核验时间：2026/8/13 | `` |
| `sku_ids` | string | 条件必填 | 多个商品 SKU 编码；用于批量精确查询。；限制：多个值的格式以聚水潭官方文档为准。；来源：聚水潭开放平台文档；核验时间：2026/8/13 | `` |

> 说明：上表只覆盖已维护的常用/关键参数，不代表三方接口全部参数。完整参数请查看官方文档。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/items/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":10,"modified_begin":"","modified_end":"","sku_id":"","sku_ids":""}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/items/query
接口用途：聚水潭商品查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 10,
  "modified_begin": "",
  "modified_end": "",
  "sku_id": "",
  "sku_ids": ""
}
```

#### 聚水潭订单详情

- 接口编码：`jushuitan.orders.detail`
- 权限标识：`orders.detail`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/orders/detail`
- 官方文档：暂未维护，请以三方开放平台实际文档为准
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：待补官方文档
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：默认范围
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/orders/detail' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/orders/detail
接口用途：聚水潭订单详情
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭订单查询

- 接口编码：`jushuitan.orders.search`
- 权限标识：`orders.search`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/orders/search`
- 三方官方方法：`orders.single.query`
- 官方文档：https://open.jushuitan.com/document/2125.html
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放平台“订单查询”配置：请求入口 /api/open/query.aspx，系统 method=orders.single.query。此接口不包含淘系和拼多多订单；body 字段名、类型、是否必填以官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：默认范围
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 10,
  "modified_begin": "",
  "modified_end": "",
  "shop_id": 1,
  "status": "",
  "so_ids": ""
}
```

常用/关键参数说明：

| 参数 | 类型 | 是否必填 | 说明 | 示例 |
| --- | --- | --- | --- | --- |
| `page_index` | number | 建议 | 页码，从 1 开始。；限制：分页查询建议填写。；来源：https://open.jushuitan.com/document/2125.html；核验时间：2026/8/12 | `1` |
| `page_size` | number | 建议 | 每页数量。；限制：建议不超过 50，具体限制以聚水潭官方文档为准。；来源：https://open.jushuitan.com/document/2125.html；核验时间：2026/8/12 | `10` |
| `modified_begin` | string | 条件必填 | 修改开始时间；与 modified_end 同时填写用于增量查询。；限制：时间格式 yyyy-MM-dd HH:mm:ss；时间跨度以聚水潭官方文档为准。；来源：https://open.jushuitan.com/document/2125.html；核验时间：2026/8/12 | `` |
| `modified_end` | string | 条件必填 | 修改结束时间；与 modified_begin 同时填写用于增量查询。；限制：时间格式 yyyy-MM-dd HH:mm:ss；时间跨度以聚水潭官方文档为准。；来源：https://open.jushuitan.com/document/2125.html；核验时间：2026/8/12 | `` |
| `shop_id` | number | 可选 | 店铺编号。；来源：https://open.jushuitan.com/document/2125.html；核验时间：2026/8/12 | `1` |
| `status` | string | 可选 | 订单状态过滤。；限制：状态枚举以聚水潭官方文档为准。；来源：https://open.jushuitan.com/document/2125.html；核验时间：2026/8/12 | `` |
| `so_ids` | string | 条件必填 | 线上单号；多个单号按聚水潭官方格式传入。；限制：用于按线上单号精确查询；最大数量以官方文档为准。；来源：https://open.jushuitan.com/document/2125.html；核验时间：2026/8/12 | `` |

> 说明：上表只覆盖已维护的常用/关键参数，不代表三方接口全部参数。完整参数请查看官方文档。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/orders/search' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":10,"modified_begin":"","modified_end":"","shop_id":1,"status":"","so_ids":""}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/orders/search
接口用途：聚水潭订单查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 10,
  "modified_begin": "",
  "modified_end": "",
  "shop_id": 1,
  "status": "",
  "so_ids": ""
}
```

#### 聚水潭其他出入库查询

- 接口编码：`jushuitan.other.inout.query`
- 权限标识：`other.inout.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/other/inout/query`
- 三方官方方法：`other.inout.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/other/inout/query 配置。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/other/inout/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/other/inout/query
接口用途：聚水潭其他出入库查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭采购入库查询

- 接口编码：`jushuitan.purchase.inbound.query`
- 权限标识：`purchase.inbound.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/inbound/query`
- 三方官方方法：`purchasein.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/purchasein/query 配置；用户提供的 /open/webapi/wmsapi/purchasein/purchaseinquery 在当前网关 method 直测返回接口不存在。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/inbound/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/inbound/query
接口用途：聚水潭采购入库查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭采购单查询

- 接口编码：`jushuitan.purchase.orders.query`
- 权限标识：`purchase.orders.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/orders/query`
- 三方官方方法：`purchase.query`
- 官方文档：https://open.jushuitan.com/document/2042.html
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按当前聚水潭 RDS 凭证网关修正：请求入口 /api/open/query.aspx，系统 method=purchase.query。业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：默认范围
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "",
  "modified_end": "",
  "po_id": ""
}
```

常用/关键参数说明：

| 参数 | 类型 | 是否必填 | 说明 | 示例 |
| --- | --- | --- | --- | --- |
| `page_index` | number | 建议 | 页码，从 1 开始；用于分页查询。；限制：建议分页查询时填写。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `1` |
| `page_size` | number | 建议 | 每页数量；不要超过平台限制和后台授权范围。；限制：建议不超过 100。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `20` |
| `modified_begin` | string | 条件必填 | 修改开始时间；用于按时间范围查询采购单。；限制：时间格式通常为 yyyy-MM-dd HH:mm:ss。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |
| `modified_end` | string | 条件必填 | 修改结束时间；用于按时间范围查询采购单。；限制：时间格式通常为 yyyy-MM-dd HH:mm:ss。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |
| `po_id` | string | 条件必填 | 采购单号或采购单 ID；用于精确查询采购单。；限制：与时间范围等查询条件至少满足平台要求之一。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |

> 说明：上表只覆盖已维护的常用/关键参数，不代表三方接口全部参数。完整参数请查看官方文档。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/orders/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"","modified_end":"","po_id":""}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/orders/query
接口用途：聚水潭采购单查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "",
  "modified_end": "",
  "po_id": ""
}
```

#### 聚水潭采购退货查询

- 接口编码：`jushuitan.purchase.out.query`
- 权限标识：`purchase.out.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/out/query`
- 三方官方方法：`purchaseout.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/purchaseout/query 配置：请求入口 /api/open/query.aspx，系统 method=purchaseout.query。业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/out/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/purchase/out/query
接口用途：聚水潭采购退货查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭销售出库查询

- 接口编码：`jushuitan.sales.out.query`
- 权限标识：`sales.out.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/sales/out/query`
- 三方官方方法：`orders.out.simple.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/orders/out/simple/query 配置。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/sales/out/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/sales/out/query
接口用途：聚水潭销售出库查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭店铺查询

- 接口编码：`jushuitan.shops.query`
- 权限标识：`shops.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/shops/query`
- 三方官方方法：`shops.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/shops/query 配置。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/shops/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/shops/query
接口用途：聚水潭店铺查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭供应商查询

- 接口编码：`jushuitan.suppliers.query`
- 权限标识：`suppliers.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/suppliers/query`
- 三方官方方法：`supplier.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按当前聚水潭 RDS 凭证网关配置：请求入口 /api/open/query.aspx，系统 method=supplier.query。业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：默认范围
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "supplier_id": ""
}
```

常用/关键参数说明：

| 参数 | 类型 | 是否必填 | 说明 | 示例 |
| --- | --- | --- | --- | --- |
| `page_index` | number | 建议 | 页码，从 1 开始；用于分页查询。；限制：建议分页查询时填写。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `1` |
| `page_size` | number | 建议 | 每页数量；不要超过平台限制和后台授权范围。；限制：建议不超过 100。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `20` |
| `supplier_id` | string | 可选 | 供应商 ID；用于按供应商精确查询。；限制：可选过滤条件。；来源：聚水潭开放平台文档；核验时间：2026/7/29 | `` |

> 说明：上表只覆盖已维护的常用/关键参数，不代表三方接口全部参数。完整参数请查看官方文档。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/suppliers/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"supplier_id":""}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/suppliers/query
接口用途：聚水潭供应商查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "supplier_id": ""
}
```

#### 聚水潭仓库查询

- 接口编码：`jushuitan.warehouses.query`
- 权限标识：`warehouses.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/warehouses/query`
- 三方官方方法：`wms.partner.query`
- 官方文档：https://open.jushuitan.com/
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：部分维护；已按聚水潭开放接口路径 /open/wms/partner/query 配置。 请求入口 /api/open/query.aspx；业务请求 body 字段名、类型、是否必填仍以聚水潭官方文档为准。
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：max_page_size=100
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/warehouses/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/warehouses/query
接口用途：聚水潭仓库查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

#### 聚水潭WMS采购单查询

- 接口编码：`jushuitan.wms.purchase.query`
- 权限标识：`wms.purchase.query`
- 请求地址：`POST https://api.wjyfek.com/api/proxy/v1/jushuitan/wms/purchase/query`
- 官方文档：暂未维护，请以三方开放平台实际文档为准
- 请求参数来源：按 聚水潭 开放平台原接口文档填写，代理不改 body 字段名
- 参数维护状态：待补官方文档
- 风险等级：只读
- 限流：60 次/分钟
- 数据范围：默认范围
- 后台使用凭证：聚水潭凭证（仅后台可见，不提供给业务方）

请求 body 示例：

```json
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

常用/关键参数说明：

> 该接口尚未维护常用参数表。请按官方文档填写 body；如果需要给业务长期开放，建议先在后台补充常用参数和示例。

curl 示例：

```bash
curl -sS -X POST 'https://api.wjyfek.com/api/proxy/v1/jushuitan/wms/purchase/query' \
  -H 'X-Client-Id: app_2fa315c7dff24df59b36' \
  -H 'Authorization: Bearer <Client Secret>' \
  -H 'Content-Type: application/json' \
  -d '{"page_index":1,"page_size":20,"modified_begin":"2026-07-01 00:00:00","modified_end":"2026-07-28 23:59:59"}'
```

给 AI 的单接口提示词：

```text
请调用 POST https://api.wjyfek.com/api/proxy/v1/jushuitan/wms/purchase/query
接口用途：聚水潭WMS采购单查询
请求头使用 X-Client-Id 和 Authorization，body 参数按 聚水潭 开放平台文档保持原样。
示例 body：
{
  "page_index": 1,
  "page_size": 20,
  "modified_begin": "2026-07-01 00:00:00",
  "modified_end": "2026-07-28 23:59:59"
}
```

## 给 AI 的通用调用提示词

```text
你现在要通过 API 安全代理调用「供应链项目」已授权的第三方接口。
代理基础地址：https://api.wjyfek.com
Client ID：app_2fa315c7dff24df59b36
Authorization 使用我提供的 Client Secret。不要询问或暴露任何数据库、服务器或第三方平台密钥。
调用时按本说明中的接口地址、请求头和 body 示例组织请求；body 字段按对应第三方开放平台文档保持原样。
返回后请保留 request_id，便于后台日志追踪。
```
