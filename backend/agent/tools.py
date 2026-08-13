# -*- coding: utf-8 -*-
"""工具注册表：模型唯一能碰到的业务入口。

每个工具声明名称、入参 JSON Schema、风险级和 handler。模型只能选工具、填参数，
不能生成 SQL、不能改数字、不能跑 Shell。L0 直接执行；L1/L2 由 runner 转成
pending_action，人工确认后才真正执行（见 `actions.py`）。

新增能力就是在这里加一条注册，不需要改 Agent Core。
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ..contracts import INVOICE_LABELS, generate_contract, get_contract_options
from ..database import fetch_contract_order_choices, fetch_exchange_products
from ..delivery_reminders import BUCKET_ORDER, URGENT_BUCKETS, build_reminders, filter_orders, reminder_markdown
from ..gb_standards import catalog_status, lookup_product_standards
from ..order_source import fetch_exchange_order_items, fetch_exchange_orders
from ..procurement_data import day, integer, number, text


RISK_LEVELS = {"L0": "只读", "L1": "生成产物", "L2": "对外动作", "L3": "改主数据"}

# 架构方案 §14 预留的工具位：只占位，不提前写实现。上线任何一项都是在这里加一条
# `registry.register(...)`，Agent Core 不动。
RESERVED_TOOLS = {
    "supplier_scorecard": "供应商绩效评价（交期达成率 / 逾期率 / 入库速度），口径待在 README 定义",
    "price_watch": "价格异常监控（同 SKU 历史价、跨供应商比价），阈值配置化、不做模型",
    "inventory_watch": "库存预警与滞销分析，依赖库存表（与预测同一数据前提）",
    "create_purchase_draft": "由订货建议单生成采购单草稿，L2/L3 写操作",
    "master_data_gaps": "主数据缺口汇总（供应商未维护 / 近期采购 SKU 无图 / 合同不可生成原因），只读",
}


class ToolError(ValueError):
    """业务侧可回给员工的工具错误。"""


@dataclass
class ToolContext:
    """工具的静态依赖 + 当次调用的身份。"""

    env_path: str
    root: Path
    fetch_rows: Callable[..., tuple]
    exchange: Any = None
    forecast: Any = None
    notifier: Any = None
    audit: Any = None
    setting: Any = None
    operator: str = ""
    channel: str = "web"
    session_id: str | None = None
    run_id: str | None = None
    action_id: str | None = None

    def for_caller(self, *, operator: str, channel: str, session_id=None, run_id=None, action_id=None):
        return replace(self, operator=operator, channel=channel, session_id=session_id,
                       run_id=run_id, action_id=action_id)

    def rows(self, year=None):
        """取一年的采购明细行；缓存策略由注入的 `fetch_rows` 决定。"""
        return self.fetch_rows(year)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    risk: str
    handler: Callable[[dict, ToolContext], Any]
    title: Callable[[dict], str] | None = None
    preview: Callable[[dict, ToolContext], dict] | None = None

    @property
    def needs_confirm(self) -> bool:
        return self.risk in ("L1", "L2")

    def schema(self) -> dict:
        note = "" if not self.needs_confirm else "（该动作需要员工确认后才会真正执行）"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description + note,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.risk not in RISK_LEVELS:
            raise ValueError(f"未知风险级 {tool.risk}")
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已注册")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if not tool:
            raise ToolError(f"没有名为 {name} 的工具")
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict]:
        return [self._tools[name].schema() for name in self.names()]

    def catalog(self) -> list[dict]:
        return [{
            "name": name,
            "risk": self._tools[name].risk,
            "riskLabel": RISK_LEVELS[self._tools[name].risk],
            "needsConfirm": self._tools[name].needs_confirm,
            "description": self._tools[name].description,
        } for name in self.names()]


def _limit(value, default=20, cap=200):
    try:
        return max(1, min(int(value or default), cap))
    except (TypeError, ValueError):
        return default


def _po_id(arguments) -> str:
    po_id = str(arguments.get("po_id") or arguments.get("purchaseOrderNo") or "").strip()
    if not po_id.isdigit():
        raise ToolError("采购单号必须是纯数字")
    return po_id


def _sku_list(value, label) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,，;；]+", value.strip())
    if not isinstance(value, list):
        raise ToolError(f"{label} 必须是列表")
    items = []
    for raw in value:
        item = str(raw or "").strip()
        if item and item not in items:
            items.append(item)
    if not items:
        raise ToolError(f"{label} 不能为空")
    return items


# ---------------------------------------------------------------- L0 只读工具


def _search_purchase_orders(arguments, ctx):
    query = str(arguments.get("query") or "").strip()
    orders = fetch_contract_order_choices(ctx.env_path, limit=_limit(arguments.get("limit"), 20, 100), query=query)
    return {"query": query, "count": len(orders), "orders": orders}


def _get_purchase_order(arguments, ctx):
    options = get_contract_options(_po_id(arguments), ctx.env_path)
    return {
        "purchaseOrderNo": options["purchaseOrderNo"],
        "orderDate": options["orderDate"],
        "deliveryDate": options["deliveryDate"],
        "status": options["status"],
        "purchaser": options["purchaser"],
        "supplier": options["supplierShortName"],
        "supplierMapped": options["supplierMapped"],
        "warehouse": options["warehouse"],
        "receiveAddress": options["receiveAddress"],
        "paymentMethod": options["paymentMethod"],
        "invoiceRates": options["invoiceRates"],
        "totalQuantity": options["totalQuantity"],
        "items": [{
            "sku": item["sku"], "name": item["name"], "specification": item["specification"],
            "quantity": item["quantity"], "inQuantity": item["inQuantity"],
            "pendingQuantity": max(0, item["quantity"] - item["inQuantity"]),
            "erpPrice": item["erpPrice"], "deliveryDate": item["deliveryDate"],
            "maintainedPrices": item["prices"],
            "category": item.get("category") or "",
            "gbStandard": item.get("gbStandard") or "",
            "gbOptionCount": len(item.get("gbOptions") or []),
        } for item in options["items"]],
    }


def _delivery_reminders(arguments, ctx):
    rows, meta = ctx.rows(arguments.get("year"))
    reminders = build_reminders(rows, arguments.get("today"))
    buckets = arguments.get("buckets") or ([arguments["bucket"]] if arguments.get("bucket") else None)
    orders, matched = filter_orders(
        reminders,
        buckets=buckets,
        buyer=arguments.get("buyer") or "",
        supplier=arguments.get("supplier") or "",
        limit=_limit(arguments.get("limit"), 30, 200),
    )
    return {
        "today": reminders["today"],
        "year": meta.get("year"),
        "totals": reminders["totals"],
        "buckets": reminders["buckets"],
        "byBuyer": reminders["byBuyer"][:20],
        "matched": matched,
        "returned": len(orders),
        "orders": orders,
        "note": "交期取 item_delivery_date，为空退到最早预计到货日期；只统计仍有待入库数量的明细行",
    }


def _dashboard_summary(arguments, ctx):
    """看板统计：直接从明细行现算，与页面同一口径。"""
    rows, meta = ctx.rows(arguments.get("year"))
    buyer = str(arguments.get("buyer") or "").strip()
    supplier = str(arguments.get("supplier") or "").strip()
    picked = [row for row in rows
              if (not buyer or buyer in text(row.get("采购员")))
              and (not supplier or supplier in text(row.get("item_supplier_id")))]
    totals = {"lines": len(picked), "orders": 0, "quantity": 0, "inQuantity": 0,
              "pendingQuantity": 0, "amount": 0.0}
    orders, by_buyer, by_supplier, by_category, dates = set(), {}, {}, {}, []
    for row in picked:
        quantity = integer(row.get("数量"))
        in_quantity = integer(row.get("item_in_qty"))
        amount = number(row.get("基本金额"))
        totals["quantity"] += quantity
        totals["inQuantity"] += in_quantity
        totals["pendingQuantity"] += max(0, quantity - in_quantity)
        totals["amount"] += amount
        orders.add(text(row.get("采购单号")))
        purchase_day = day(row.get("采购日期"))
        if purchase_day:
            dates.append(purchase_day)
        for bucket, key in ((by_buyer, text(row.get("采购员")) or "未知"),
                            (by_supplier, text(row.get("item_supplier_id")) or "未知"),
                            (by_category, text(row.get("item_sku_other_3")) or "未分类")):
            stat = bucket.setdefault(key, {"name": key, "amount": 0.0, "quantity": 0, "pendingQuantity": 0})
            stat["amount"] += amount
            stat["quantity"] += quantity
            stat["pendingQuantity"] += max(0, quantity - in_quantity)
    totals["orders"] = len(orders)
    totals["amount"] = round(totals["amount"], 2)
    totals["inRate"] = round(totals["inQuantity"] / totals["quantity"], 4) if totals["quantity"] else 0

    def top(bucket, size=8):
        ranked = sorted(bucket.values(), key=lambda item: -item["amount"])[:size]
        return [{**item, "amount": round(item["amount"], 2)} for item in ranked]

    dates.sort()
    return {
        "year": meta.get("year"),
        "source": meta.get("source"),
        "availableYears": meta.get("availableYears") or [],
        "filter": {"buyer": buyer, "supplier": supplier},
        "dateRange": {"min": dates[0] if dates else "", "max": dates[-1] if dates else ""},
        "totals": totals,
        "topBuyers": top(by_buyer),
        "topSuppliers": top(by_supplier),
        "topCategories": top(by_category),
        "note": "采购金额 = 基本金额；已入库 = item_in_qty；待入库 = 数量 − 已入库按行取正",
    }


def _search_products(arguments, ctx):
    products = fetch_exchange_products(
        ctx.env_path,
        limit=_limit(arguments.get("limit"), 20, 100),
        query=str(arguments.get("query") or "").strip(),
    )
    return {"count": len(products), "products": products}


def _gb_catalog_status(arguments, ctx):
    payload = catalog_status(ctx.env_path)
    if ctx.setting is not None:
        enabled = str(ctx.setting("GB_SYNC_ENABLED", "false") or "").strip().lower()
        payload["dailySyncEnabled"] = enabled in ("1", "true", "yes", "on", "enabled")
        payload["dailySyncTime"] = ctx.setting("GB_SYNC_TIME", "02:30")
    return payload


def _lookup_gb_standards(arguments, ctx):
    sku = str(arguments.get("sku") or "").strip()
    query = str(arguments.get("query") or "").strip()
    category = str(arguments.get("category") or "").strip()
    standard_no = str(arguments.get("standard_no") or "").strip()
    family_id = str(arguments.get("family_id") or "").strip()
    if not any((sku, query, category, standard_no, family_id)):
        raise ToolError("请提供 SKU、商品名称、分类、标准号或目录族")
    return lookup_product_standards(
        ctx.env_path,
        sku=sku, query=query, category=category,
        standard_no=standard_no, family_id=family_id,
        limit=_limit(arguments.get("limit"), 12, 40),
    )


def _require_order_source(ctx):
    if ctx.setting is None:
        raise ToolError("订单镜像查询尚未配置")
    return ctx.setting


def _search_sales_orders(arguments, ctx):
    """让 Agent 先把员工说的平台单号/店铺等解析成明确 ERP o_id。"""
    result = fetch_exchange_orders(
        _require_order_source(ctx), ctx.env_path,
        query=str(arguments.get("query") or "").strip(),
        source_sku=str(arguments.get("source_sku") or "").strip(),
        limit=_limit(arguments.get("limit"), 20, 100),
    )
    if not result.get("configured"):
        raise ToolError(result.get("message") or "订单镜像暂不可用")
    return {"count": len(result.get("orders") or []), **result}


def _get_sales_order_items(arguments, ctx):
    """读取明确订单内的 SKU，供自然语言换货消歧，不执行 ERP 写操作。"""
    o_ids = _sku_list(arguments.get("o_ids"), "o_ids")
    result = fetch_exchange_order_items(
        _require_order_source(ctx), ctx.env_path, o_ids=o_ids,
    )
    if not result.get("configured"):
        raise ToolError(result.get("message") or "订单明细镜像暂不可用")
    return result


def _require_forecast(ctx):
    if ctx.forecast is None:
        raise ToolError("预测子系统尚未启用")
    return ctx.forecast


def _forecast_demand(arguments, ctx):
    service = _require_forecast(ctx)
    keys = _sku_list(arguments.get("keys") or arguments.get("skus"), "keys")
    return service.predict(keys, horizon_days=arguments.get("horizon_days"))


def _order_suggestion(arguments, ctx):
    service = _require_forecast(ctx)
    keys = _sku_list(arguments.get("keys") or arguments.get("skus"), "keys")
    result = service.order_suggestion(
        keys,
        lead_time_days=arguments.get("lead_time_days"),
        service_level=arguments.get("service_level"),
        buffer_days=arguments.get("buffer_days"),
        inventory=arguments.get("inventory"),
    )
    if ctx.audit is not None:
        result["forecastRunId"] = ctx.audit.record_forecast(
            model_name=result["model"]["name"], model_version=result["model"]["version"],
            keys=keys, inputs=result["inputs"], output=result,
            operator=ctx.operator, session_id=ctx.session_id, run_id=ctx.run_id,
            pending_action_id=ctx.action_id,
        )
    return result


# ------------------------------------------------------- L1 / L2 需确认的工具


def _contract_preview(arguments, ctx):
    po_id = _po_id(arguments)
    invoice_type = str(arguments.get("invoice_type") or "special_invoice")
    if invoice_type not in INVOICE_LABELS:
        raise ToolError("票种只能是 no_invoice、normal_invoice 或 special_invoice")
    options = get_contract_options(po_id, ctx.env_path)
    rate = arguments.get("tax_rate")
    if rate is None:
        rate = options["invoiceRates"].get(invoice_type)
    return {
        "purchaseOrderNo": po_id,
        "invoiceType": invoice_type,
        "invoiceLabel": INVOICE_LABELS[invoice_type],
        "taxRate": rate,
        "supplier": options["supplierShortName"],
        "supplierMapped": options["supplierMapped"],
        "purchaser": options["purchaser"],
        "orderDate": options["orderDate"],
        "deliveryDate": options["deliveryDate"],
        "itemCount": len(options["items"]),
        "totalQuantity": options["totalQuantity"],
        "items": [{"sku": item["sku"], "name": item["name"], "quantity": item["quantity"],
                   "erpPrice": item["erpPrice"], "maintainedPrices": item["prices"],
                   "gbStandard": item.get("gbStandard") or "",
                   "gbOptionCount": len(item.get("gbOptions") or [])}
                  for item in options["items"][:20]],
    }


def _generate_contract(arguments, ctx):
    po_id = _po_id(arguments)
    invoice_type = str(arguments.get("invoice_type") or "special_invoice")
    contract_id = secrets.token_hex(12)
    output_dir = ctx.root / "outputs" / "agent" / contract_id
    generate_contract(
        po_id, invoice_type, output_dir / "contract.xlsx",
        tax_rate=arguments.get("tax_rate"),
        price_overrides=arguments.get("price_overrides") or {},
        gb_overrides=arguments.get("gb_overrides") or arguments.get("gbOverrides") or {},
        preview_path=output_dir / "preview.png",
        env_path=ctx.env_path,
    )
    return {
        "contractId": contract_id,
        "purchaseOrderNo": po_id,
        "invoiceType": invoice_type,
        "fileName": f"采购合同-{po_id}.xlsx",
        "downloadUrl": f"/api/agent/contracts/{contract_id}/file",
        "previewUrl": f"/api/agent/contracts/{contract_id}/preview",
    }


def _exchange_payload(arguments):
    source = str(arguments.get("source_sku") or "").strip()
    target = str(arguments.get("target_sku") or "").strip()
    if not source or not target:
        raise ToolError("源 SKU 和目标 SKU 都不能为空")
    o_ids = _sku_list(arguments.get("o_ids"), "o_ids")
    products = fetch_exchange_products(
        arguments.get("env_path") or "hanli.env", query=target, limit=20,
    )
    target_product = next((item for item in products if item["sku"] == target), None)
    source_products = fetch_exchange_products(
        arguments.get("env_path") or "hanli.env", query=source, limit=20,
    )
    source_product = next((item for item in source_products if item["sku"] == source), None)
    # 特殊源 SKU 不一定存在采购镜像，其源款式由白名单配置强制补齐。
    from ..exchange.policy import load_policy
    special = next((item for item in load_policy()["specialMappings"] if item["sourceSku"] == source), None)
    source_style = (source_product or {}).get("styleCode") or (special or {}).get("sourceStyle") or ""
    target_style = (target_product or {}).get("styleCode") or ""
    return {
        "rules": {"strategy": "direct", "replacements": [{
            "from": source, "to": target,
            "sourceStyle": source_style, "targetStyle": target_style,
        }]},
        "targets": {"o_ids": o_ids, "limit": max(1, min(len(o_ids), 500))},
    }


def _exchange_preview(arguments, ctx):
    payload = _exchange_payload({**arguments, "env_path": ctx.env_path})
    if ctx.exchange is None:
        raise ToolError("换货子系统尚未启用")
    rules, targets = ctx.exchange.validate_submission(payload)
    status = ctx.exchange.status()
    return {
        "sourceSku": rules["replacements"][0]["from"],
        "targetSku": rules["replacements"][0]["to"],
        "orderCount": len(targets["o_ids"]),
        "oIds": targets["o_ids"][:50],
        "onlineWorkers": status.get("onlineWorkers", 0),
        "note": "确认后只登记 dry-run 任务；真实换货仍需在换货页核对试算清单后二次确认",
    }


def _submit_exchange(arguments, ctx):
    payload = _exchange_payload({**arguments, "env_path": ctx.env_path})
    if ctx.exchange is None:
        raise ToolError("换货子系统尚未启用")
    job = ctx.exchange.create_job(
        payload,
        operator=ctx.operator or "agent",
        idempotency_key=f"agent-action-{ctx.action_id}" if ctx.action_id else None,
    )
    return {
        "jobId": job["id"],
        "status": job["status"],
        "orderCount": len(job["targets"]["o_ids"]),
        "nextStep": "ERP Worker 领取后回报 dry-run，请到换货页核对并确认执行",
    }


def _reminder_selection(arguments, ctx):
    rows, meta = ctx.rows(arguments.get("year"))
    reminders = build_reminders(rows, arguments.get("today"))
    buckets = arguments.get("buckets") or list(URGENT_BUCKETS)
    orders, matched = filter_orders(
        reminders, buckets=buckets,
        buyer=arguments.get("buyer") or "",
        limit=_limit(arguments.get("limit"), 100, 500),
    )
    return reminders, orders, matched, meta


def _reminder_preview(arguments, ctx):
    if ctx.notifier is None:
        raise ToolError("钉钉推送尚未启用")
    reminders, orders, matched, _ = _reminder_selection(arguments, ctx)
    if not orders:
        raise ToolError("当前口径下没有需要催办的采购单，不发送空提醒")
    buyers = sorted({item["buyer"] for item in orders})
    return {
        "today": reminders["today"],
        "orderCount": len(orders),
        "matched": matched,
        "buyers": buyers,
        "targets": ctx.notifier.describe_targets(buyers),
        "pendingQty": sum(item["pendingQty"] for item in orders),
        "text": reminder_markdown(reminders, orders)[:3000],
    }


def _send_reminder(arguments, ctx):
    if ctx.notifier is None:
        raise ToolError("钉钉推送尚未启用")
    reminders, orders, _, _ = _reminder_selection(arguments, ctx)
    if not orders:
        raise ToolError("当前口径下没有需要催办的采购单，不发送空提醒")
    return ctx.notifier.send_reminders(
        reminders, orders,
        idempotency_key=f"agent-action-{ctx.action_id}" if ctx.action_id else None,
        operator=ctx.operator,
    )


# ------------------------------------------------------------------- 注册表


YEAR_PARAM = {"type": "string", "description": "统计年度，四位数字；缺省用当前年度"}
TODAY_PARAM = {"type": "string", "description": "以哪天为今天计算剩余天数，YYYY-MM-DD；缺省服务器当天"}
BUCKETS_PARAM = {
    "type": "array",
    "items": {"type": "string", "enum": list(BUCKET_ORDER)},
    "description": "催办档位：overdue 逾期 / t1 剩0-1天 / t10 剩2-10天 / t20 剩11-20天 / later 暂不提醒 / unscheduled 未排期；缺省为需催的四波",
}


def build_registry(*, with_forecast=True, with_exchange=True, with_notifier=True) -> ToolRegistry:
    """按已启用的子系统装配工具注册表。"""
    registry = ToolRegistry()
    registry.register(Tool(
        name="search_purchase_orders",
        description="按采购单号、供应商简称或采购员姓名搜索本年度采购单，返回单号、日期、供应商、状态。",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词，可空"},
            "limit": {"type": "integer", "description": "返回条数，默认 20"},
        }},
        risk="L0", handler=_search_purchase_orders,
    ))
    registry.register(Tool(
        name="get_purchase_order",
        description="按采购单号读取单头与全部商品明细：供应商、采购员、交期、数量、已入库、待入库、ERP 单价、已维护的票种价格，以及该行已保存的执行标准（GB/T…，不是商品条码）。要看该类有哪些候选国标请用 lookup_gb_standards。",
        parameters={"type": "object", "properties": {
            "po_id": {"type": "string", "description": "ERP 采购单号，纯数字"},
        }, "required": ["po_id"]},
        risk="L0", handler=_get_purchase_order,
    ))
    registry.register(Tool(
        name="delivery_reminders",
        description="按四波催办口径（T-20 / T-10 / T-1 / 逾期）汇总仍有待入库数量的采购单，可按档位、采购员、供应商过滤。",
        parameters={"type": "object", "properties": {
            "buckets": BUCKETS_PARAM,
            "buyer": {"type": "string", "description": "采购员姓名，支持部分匹配"},
            "supplier": {"type": "string", "description": "供应商名称，支持部分匹配"},
            "limit": {"type": "integer", "description": "返回采购单条数，默认 30"},
            "year": YEAR_PARAM, "today": TODAY_PARAM,
        }},
        risk="L0", handler=_delivery_reminders,
    ))
    registry.register(Tool(
        name="dashboard_summary",
        description="采购看板统计：单数、明细行数、采购金额、数量、已入库、待入库、入库率，以及采购员/供应商/品类金额 Top。",
        parameters={"type": "object", "properties": {
            "year": YEAR_PARAM,
            "buyer": {"type": "string", "description": "只看某个采购员"},
            "supplier": {"type": "string", "description": "只看某个供应商"},
        }},
        risk="L0", handler=_dashboard_summary,
    ))
    registry.register(Tool(
        name="search_products",
        description="按商品编码或名称搜索商品主数据里的 SKU（含分类）。确认换货、预测或查执行标准前用它锁定商品编码；查国标接着调用 lookup_gb_standards。",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "SKU 或商品名称关键词"},
            "limit": {"type": "integer", "description": "返回条数，默认 20"},
        }},
        risk="L0", handler=_search_products,
    ))
    registry.register(Tool(
        name="gb_catalog_status",
        description=(
            "查询本地国标目录库的同步状态和条数：上次成功时间、现行/即将实施/废止数量、各目录族条数。"
            "问「国标库同步了吗」「有多少条标准」时用。只读，不会触发同步，也不含标准全文。"
        ),
        parameters={"type": "object", "properties": {}},
        risk="L0", handler=_gb_catalog_status,
    ))
    registry.register(Tool(
        name="lookup_gb_standards",
        description=(
            "按 SKU、商品名称、分类、标准号或目录族查询执行标准（GB/T…）。"
            "问「毛绒玩具用什么国标」「GB/T 9832 是什么状态」时用。"
            "这不是商品条码「国标码」。现行和即将实施可能同时存在，不要擅自指定一条写进合同；"
            "未映射的分类如实说明，不要编造标准号。"
        ),
        parameters={"type": "object", "properties": {
            "sku": {"type": "string", "description": "商品编码，已知 SKU 时优先用这个"},
            "query": {"type": "string", "description": "商品名称、分类词或标准号，如 毛绒小熊 / GB/T 9832-2026"},
            "category": {"type": "string", "description": "ERP 商品分类，如 毛绒（04）、衬衫"},
            "standard_no": {"type": "string", "description": "标准号，如 GB/T 9832-2026"},
            "family_id": {"type": "string", "description": "目录族：服装 / 鞋类 / 玩具 / 杯壶 等"},
            "limit": {"type": "integer", "description": "返回条数，默认 12，最多 40"},
        }},
        risk="L0", handler=_lookup_gb_standards,
    ))
    if with_exchange:
        registry.register(Tool(
            name="search_sales_orders",
            description=(
                "按内部订单号、平台订单号、店铺、买家或源 SKU 搜索订单镜像，返回明确 ERP o_id。"
                "处理「异常订单」换货时：单号不明确就用这个工具，按待发货 + 含源 SKU 收候选；"
                "不要用交期催办工具。自然语言换货在 o_id 不明确时必须先调用。"
            ),
            parameters={"type": "object", "properties": {
                "query": {"type": "string", "description": "订单号、平台单号、店铺或买家关键词，可空"},
                "source_sku": {"type": "string", "description": "可选：订单必须包含的源 SKU"},
                "limit": {"type": "integer", "description": "返回条数，默认 20"},
            }},
            risk="L0", handler=_search_sales_orders,
        ))
        registry.register(Tool(
            name="get_sales_order_items",
            description=(
                "读取一组明确 ERP 内部订单号中的商品 SKU、款式、名称、规格、数量和订单覆盖数；"
                "员工只说商品名称或尺码时先用它确认源 SKU。"
            ),
            parameters={"type": "object", "properties": {
                "o_ids": {"type": "array", "items": {"type": "string"},
                          "description": "ERP 内部订单号列表"},
            }, "required": ["o_ids"]},
            risk="L0", handler=_get_sales_order_items,
        ))
    if with_forecast:
        registry.register(Tool(
            name="forecast_demand",
            description="查询给定 SKU 或款式编码的逐日销量预测（p50 点预测与 p10/p90 区间）。数字由模型工件给出，不要自行推算或修改。",
            parameters={"type": "object", "properties": {
                "keys": {"type": "array", "items": {"type": "string"},
                         "description": "SKU 或款式编码列表"},
                "horizon_days": {"type": "integer", "description": "预测天数，缺省用模型默认范围"},
            }, "required": ["keys"]},
            risk="L0", handler=_forecast_demand,
        ))
        registry.register(Tool(
            name="order_suggestion",
            description=(
                "按确定性公式计算订货建议：交期内预测需求 + 安全库存 − 可用库存 − 在途待入库，"
                "并给出建议下单日。你只能解释这份结果，不能改动任何数字。"
            ),
            parameters={"type": "object", "properties": {
                "keys": {"type": "array", "items": {"type": "string"}, "description": "SKU 列表"},
                "lead_time_days": {"type": "integer", "description": "供应商交期天数"},
                "service_level": {"type": "number", "description": "服务水平 0.5–0.99，默认 0.9"},
                "buffer_days": {"type": "integer", "description": "下单缓冲天数，默认 3"},
                "inventory": {"type": "object", "description": "可用库存覆盖值，键是 SKU"},
            }, "required": ["keys"]},
            risk="L0", handler=_order_suggestion,
        ))
    registry.register(Tool(
        name="generate_purchase_contract",
        description="为某张采购单生成正式采购合同 Excel（含预览图）。会先给出合同要点供员工确认。",
        parameters={"type": "object", "properties": {
            "po_id": {"type": "string", "description": "ERP 采购单号，纯数字"},
            "invoice_type": {"type": "string", "enum": list(INVOICE_LABELS),
                             "description": "票种：no_invoice 不开票 / normal_invoice 普票 / special_invoice 专票"},
            "tax_rate": {"type": "number", "description": "税率百分数，缺省用供应商维护值"},
            "gb_overrides": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "明细 poi_id 到执行标准号（如 GB/T 9832-2026）的映射，可空",
            },
        }, "required": ["po_id", "invoice_type"]},
        risk="L1", handler=_generate_contract, preview=_contract_preview,
        title=lambda args: f"生成采购合同 {args.get('po_id', '')}（{INVOICE_LABELS.get(str(args.get('invoice_type')), '')}）",
    ))
    if with_exchange:
        registry.register(Tool(
            name="submit_exchange_dry_run",
            description=(
                "登记一个订单 SKU 换货任务，只做 dry-run 试算，不会立即修改 ERP。"
                "必须提供明确的 ERP 内部订单号 o_id，不接受模糊条件。"
            ),
            parameters={"type": "object", "properties": {
                "source_sku": {"type": "string", "description": "订单中当前的商品编码"},
                "target_sku": {"type": "string", "description": "要更换成的商品编码"},
                "o_ids": {"type": "array", "items": {"type": "string"},
                          "description": "ERP 内部订单号列表"},
            }, "required": ["source_sku", "target_sku", "o_ids"]},
            risk="L1", handler=_submit_exchange, preview=_exchange_preview,
            title=lambda args: f"登记换货试算 {args.get('source_sku', '')} → {args.get('target_sku', '')}",
        ))
    if with_notifier:
        registry.register(Tool(
            name="send_delivery_reminder",
            description="把交期催办清单发到钉钉采购群并 @ 对应采购员。对外动作，确认前会先给出完整清单。",
            parameters={"type": "object", "properties": {
                "buckets": BUCKETS_PARAM,
                "buyer": {"type": "string", "description": "只催某个采购员"},
                "limit": {"type": "integer", "description": "最多包含多少张采购单，默认 100"},
                "year": YEAR_PARAM, "today": TODAY_PARAM,
            }},
            risk="L2", handler=_send_reminder, preview=_reminder_preview,
            title=lambda args: "发送交期催办到钉钉群",
        ))
    return registry
