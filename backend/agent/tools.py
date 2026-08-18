# -*- coding: utf-8 -*-
"""工具注册表：模型唯一能碰到的业务入口。

每个工具声明名称、入参 JSON Schema、风险级和 handler。模型只能选工具、填参数，
不能生成 SQL、不能改数字、不能跑 Shell。L0 直接执行；L1/L2 由 runner 转成
pending_action，人工确认后才真正执行（见 `actions.py`）。

新增能力就是在这里加一条注册，不需要改 Agent Core。
"""
from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from ..business_time import business_today
from ..contracts import INVOICE_LABELS, generate_contract, get_contract_options
from ..database import fetch_contract_order_choices, fetch_exchange_products
from ..delivery_reminders import (
    FOLLOWUP_ORDER, FOLLOWUP_URGENT, build_reminders, filter_orders, reminder_markdown,
)
from ..gb_standards import catalog_status, family_ids_for, lookup_product_standards
from ..exchange.insole import (
    execute_insole_orders, format_insole_list,
    load_reserved_insole_orders, load_written_insole_orders,
    locate_insole_orders, public_order, remember_insole_writes, sync_insole_mirror,
)
from ..paths import resolve_repo_path
from ..order_source import OrderSourceError, fetch_exchange_order_items, fetch_exchange_orders
from ..procurement_data import day, integer, number, text
from ..product_images import resolve_product_image
from ..staff_names import SELF_SCOPE_UNBOUND


RISK_LEVELS = {"L0": "只读", "L1": "生成产物", "L2": "对外动作", "L3": "改主数据"}
PERMISSIONS = {"read", "write", "notify"}
SIDE_EFFECTS = {"none", "file", "notify", "erp", "write"}
SELF_SCOPE_TOKENS = frozenset({"我", "我的", "我名下", "本人", "自己"})

# 架构方案 §14 预留的工具位：只占位，不提前写实现。上线任何一项都是在这里加一条
# `registry.register(...)`，Agent Core 不动。价格盯盘 / 库存预警 / 采购草稿已取消。
RESERVED_TOOLS = {
    "supplier_scorecard": "供应商绩效评价（交期达成率 / 逾期率 / 入库速度），口径待在 README 定义",
}


class ToolError(ValueError):
    """业务侧可回给员工的工具错误。"""


class PermissionDenied(ToolError):
    """权限拒绝，带结构化 decision，不靠 prompt 拦。"""

    def __init__(self, reason: str, *, role="", tool="", permission="", channel=""):
        super().__init__(reason)
        self.decision = {
            "ok": False,
            "reason": reason,
            "role": role,
            "tool": tool,
            "permission": permission,
            "channel": channel,
        }


@dataclass
class ToolContext:
    """工具的静态依赖 + 当次调用的身份。"""

    env_path: str
    root: Path
    fetch_rows: Callable[..., tuple]
    fetch_followup: Callable[..., tuple] | None = None
    exchange: Any = None
    erp: Any = None
    forecast: Any = None
    notifier: Any = None
    audit: Any = None
    setting: Any = None
    quality: Any = None
    mirror: Any = None
    operator: str = ""
    user_id: str = ""
    channel: str = "web"
    session_id: str | None = None
    run_id: str | None = None
    action_id: str | None = None
    role: str = "operator"
    buyer_names: tuple[str, ...] = ()

    def for_caller(self, *, operator: str, channel: str, session_id=None, run_id=None,
                   action_id=None, user_id="", role="operator", buyer_names=()):
        return replace(
            self, operator=operator, user_id=str(user_id or ""),
            channel=channel, session_id=session_id, run_id=run_id, action_id=action_id,
            role=str(role or "operator"), buyer_names=tuple(buyer_names or ()),
        )

    def rows(self, year=None):
        """取一年的采购明细行；缓存策略由注入的 `fetch_rows` 决定。"""
        return self.fetch_rows(year)

    def followup_rows(self):
        """跟单池：全库已确认未完结。未注入时回退 `fetch_rows`。"""
        if self.fetch_followup is not None:
            return self.fetch_followup()
        return self.fetch_rows(None)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    risk: str
    handler: Callable[[dict, ToolContext], Any]
    title: Callable[[dict], str] | None = None
    preview: Callable[[dict, ToolContext], dict] | None = None
    permission: str = ""
    side_effect: str = ""
    channels: tuple[str, ...] = ()
    domain: str = ""
    concurrency_mode: str = ""

    def __post_init__(self):
        if not self.permission:
            if self.risk == "L0":
                permission = "read"
            elif self.name.startswith("send_") or self.name.startswith("push_"):
                permission = "notify"
            else:
                permission = "write"
            object.__setattr__(self, "permission", permission)
        if not self.side_effect:
            if self.risk == "L0":
                effect = "none"
            elif self.name.startswith("send_") or self.name.startswith("push_"):
                effect = "notify"
            elif "contract" in self.name:
                effect = "file"
            else:
                effect = "write"
            object.__setattr__(self, "side_effect", effect)
        if self.permission not in PERMISSIONS:
            raise ValueError(f"未知 permission {self.permission}")
        if self.side_effect not in SIDE_EFFECTS:
            raise ValueError(f"未知 side_effect {self.side_effect}")
        if self.channels:
            object.__setattr__(self, "channels", tuple(str(item) for item in self.channels if item))

    @property
    def needs_confirm(self) -> bool:
        return self.risk != "L0"

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


def declared_arguments(tool: Tool, arguments: dict) -> dict:
    """只保留 schema.properties 声明过的入参，未声明字段直接丢弃。

    properties 为空时无法白名单，原样返回（测试桩或尚未声明 schema 的工具）。
    """
    if not isinstance(arguments, dict):
        return {}
    properties = (tool.parameters or {}).get("properties") or {}
    if not properties:
        return dict(arguments)
    return {key: arguments[key] for key in properties if key in arguments}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.risk not in RISK_LEVELS:
            raise ValueError(f"未知风险级 {tool.risk}")
        if tool.risk != "L0" and tool.preview is None:
            raise ValueError(f"工具 {tool.name} 是 {tool.risk}，必须提供 preview")
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
            "permission": self._tools[name].permission,
            "sideEffect": self._tools[name].side_effect,
            "channels": list(self._tools[name].channels),
            "description": self._tools[name].description,
        } for name in self.names()]


def _limit(value, default=20, cap=200):
    try:
        return max(1, min(int(value or default), cap))
    except (TypeError, ValueError):
        return default


LINE_CAP = 30


def tool_envelope(*, ok=True, summary, data=None, truncated=False, hint=""):
    """给模型的工具返回：短中文摘要 + 截断后的 data。全量仍只给页面。"""
    payload = {
        "ok": bool(ok),
        "summary": str(summary or ""),
        "data": data,
        "truncated": bool(truncated),
    }
    if hint:
        payload["hint"] = str(hint)
    return payload


def clip_rows(rows, cap=LINE_CAP, keep=None):
    rows = list(rows or [])
    truncated = len(rows) > cap
    clipped = rows[:cap]
    if keep:
        clipped = [
            {key: row.get(key) for key in keep if isinstance(row, dict) and key in row}
            if isinstance(row, dict) else row
            for row in clipped
        ]
    return clipped, truncated


def as_tool_envelope(result):
    """L0 结果统一成信封。已是信封或 ``{error}`` 的保持原样。"""
    if isinstance(result, dict) and result.get("error") and "ok" not in result:
        return result
    if isinstance(result, dict) and "ok" in result and "summary" in result:
        result.setdefault("data", None)
        result.setdefault("truncated", False)
        return result
    if isinstance(result, dict) and "summary" in result:
        return tool_envelope(
            summary=str(result.get("summary") or ""),
            data=result,
            truncated=bool(result.get("truncated")),
            hint=str(result.get("hint") or ""),
        )
    return tool_envelope(summary=_fallback_summary(result), data=result)


def _fallback_summary(result) -> str:
    if not isinstance(result, dict):
        return "已返回结果"
    for key in ("count", "purchaseOrderNo", "today", "message", "selectedOrderCount"):
        if result.get(key) not in (None, ""):
            return f"{key}={result[key]}"
    return "已返回结构化结果，请引用 data 中的数字，不要口算"


def is_self_scope(value) -> bool:
    return str(value or "").strip() in SELF_SCOPE_TOKENS


def scoped_buyers(value, ctx) -> list[str]:
    """空 = 不限；「我名下」= 绑定采购员署名；其它原文。"""
    raw = str(value or "").strip()
    if is_self_scope(raw):
        names = [name for name in (ctx.buyer_names or ()) if name]
        if not names:
            raise ToolError(SELF_SCOPE_UNBOUND)
        return names
    if raw:
        return [raw]
    return []


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
    if is_self_scope(query):
        query = scoped_buyers(query, ctx)[0]
    orders = fetch_contract_order_choices(ctx.env_path, limit=_limit(arguments.get("limit"), 20, 100), query=query)
    return tool_envelope(
        summary=f"命中 {len(orders)} 张采购单" + (f"，关键词 {query}" if query else ""),
        data={"query": query, "count": len(orders), "orders": orders},
        hint="只要一张单时用 get_purchase_order，不要用催办或换货代替",
    )


def _get_purchase_order(arguments, ctx):
    options = get_contract_options(_po_id(arguments), ctx.env_path)
    items = [{
        "sku": item["sku"], "name": item["name"], "specification": item["specification"],
        "quantity": item["quantity"], "inQuantity": item["inQuantity"],
        "pendingQuantity": max(0, item["quantity"] - item["inQuantity"]),
        "erpPrice": item["erpPrice"], "deliveryDate": item["deliveryDate"],
        "maintainedPrices": item["prices"],
        "category": item.get("category") or "",
        "unit": item.get("unit") or "",
        "nationalCode": item.get("nationalCode") or "",
        "gbStandard": item.get("gbStandard") or "",
        "gbOptionCount": len(item.get("gbOptions") or []),
    } for item in options["items"]]
    pending = sum(item["pendingQuantity"] for item in items)
    shown, truncated = clip_rows(
        items, LINE_CAP, keep=("sku", "name", "quantity", "pendingQuantity", "deliveryDate"),
    )
    extra = "（已截断展示前 30 行）" if truncated else ""
    return tool_envelope(
        summary=(
            f"采购单 {options['purchaseOrderNo']}，供应商{options['supplierShortName'] or '未维护'}，"
            f"待入库 {pending}，明细 {len(items)} 行{extra}"
        ),
        data={
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
            "paymentOption": options["paymentOption"],
            "paymentOptions": options["paymentOptions"],
            "invoiceRates": options["invoiceRates"],
            "totalQuantity": options["totalQuantity"],
            "pendingQuantity": pending,
            "itemCount": len(items),
            "items": shown,
        },
        truncated=truncated,
        hint="需要某一行的单价请再查，不要口算合计" if truncated else "",
    )


def _delivery_reminders(arguments, ctx):
    rows, meta = ctx.followup_rows()
    reminders = build_reminders(rows, arguments.get("today"), profile="followup")
    buckets = arguments.get("buckets") or ([arguments["bucket"]] if arguments.get("bucket") else None)
    orders, matched = filter_orders(
        reminders,
        buckets=buckets,
        buyer=scoped_buyers(arguments.get("buyer"), ctx),
        supplier=arguments.get("supplier") or "",
        limit=_limit(arguments.get("limit"), 30, 200),
    )
    totals = reminders["totals"]
    return tool_envelope(
        summary=(
            f"交期提醒 {reminders['today']}，匹配 {matched} 张，返回 {len(orders)} 张；"
            f"紧急 {totals.get('urgentOrderCount', 0)} 单、"
            f"待入库 {totals.get('urgentPendingQty', 0)}"
        ),
        data={
            "today": reminders["today"],
            "year": meta.get("year"),
            "totals": totals,
            "buckets": reminders["buckets"],
            "byBuyer": reminders["byBuyer"][:20],
            "matched": matched,
            "returned": len(orders),
            "orders": orders,
            "note": "跟单三档：已确认未完结、排除返修；交期取 item_delivery_date，为空退到最早预计到货日期；只统计仍有待入库数量的明细行",
        },
        truncated=matched > len(orders),
        hint="不要用换货工具处理采购逾期；只要一张单用 get_purchase_order",
    )


def _dashboard_summary(arguments, ctx):
    """看板统计：直接从明细行现算，与页面同一口径。"""
    rows, meta = ctx.rows(arguments.get("year"))
    buyers = scoped_buyers(arguments.get("buyer"), ctx)
    supplier = str(arguments.get("supplier") or "").strip()
    picked = [row for row in rows
              if (not buyers or any(name in text(row.get("采购员")) for name in buyers))
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
    payload = {
        "year": meta.get("year"),
        "source": meta.get("source"),
        "availableYears": meta.get("availableYears") or [],
        "filter": {"buyer": "、".join(buyers), "supplier": supplier},
        "dateRange": {"min": dates[0] if dates else "", "max": dates[-1] if dates else ""},
        "totals": totals,
        "topBuyers": top(by_buyer),
        "topSuppliers": top(by_supplier),
        "topCategories": top(by_category),
        "note": "采购金额 = 基本金额；已入库 = item_in_qty；待入库 = 数量 − 已入库按行取正",
    }
    return tool_envelope(
        summary=(
            f"{payload['year'] or '本年'}看板：{totals['orders']} 单、"
            f"待入库 {totals['pendingQuantity']}、金额 {totals['amount']}"
        ),
        data=payload,
        hint="看板统计不要用交期催办或换货代替",
    )


def _search_products(arguments, ctx):
    products = fetch_exchange_products(
        ctx.env_path,
        limit=_limit(arguments.get("limit"), 20, 100),
        query=str(arguments.get("query") or "").strip(),
    )
    return tool_envelope(
        summary=f"命中 {len(products)} 个商品",
        data={"count": len(products), "products": products},
        hint="锁定 SKU 后再查国标或换货，不要猜编码",
    )


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


GAP_LIST_CAP = 20


def _load_config_object(root: Path, name: str) -> dict:
    from ..paths import local_dir
    path = local_dir("config", root=root) / name
    if not path.is_file():
        raise ToolError(f"找不到配置 {name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(f"{name} 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ToolError(f"{name} 必须是对象")
    return data


def _tool_today(arguments) -> date:
    raw = str(arguments.get("today") or "").strip()
    if not raw:
        return business_today()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ToolError("today 必须是 YYYY-MM-DD") from exc


def _gaps_markdown(payload: dict) -> str:
    """钉钉可直接粘贴的主数据缺口清单，结构对齐催办 markdown。"""
    counts = payload["counts"]
    days = payload["days"]
    lines = [f"### 主数据缺口（近 {days} 天 · {payload['today']}）"]
    lines.append(
        f"> 采购行 **{payload['rowCount']}** · "
        f"供应商未维护 **{counts['missingSuppliers']}** · "
        f"SKU 无图 **{counts['missingImages']}** · "
        f"缺价 **{counts['missingPrices']}** · "
        f"分类未映射国标 **{counts['unmappedCategories']}**"
    )
    invoice_note = payload.get("invoiceType") or "全部票种"
    lines.append(f"> 票种口径：{invoice_note}")

    def section(title: str, key: str, items: list, render) -> None:
        total = counts[key]
        lines.append(f"\n**{title}**（{total}）")
        if not items:
            lines.append("- 无")
            return
        for item in items:
            lines.append(f"- {render(item)}")
        extra = total - len(items)
        if extra > 0:
            lines.append(f"- …另有 {extra} 条")

    section(
        "供应商未维护", "missingSuppliers", payload["missingSuppliers"],
        lambda item: f"{item['name']} · {item['poCount']} 单 · {item['buyer'] or '—'}",
    )
    section(
        "近期采购 SKU 无图", "missingImages", payload["missingImages"],
        lambda item: f"{item['sku']} · {item['name'] or item['style'] or '—'} · {item['poCount']} 单",
    )
    section(
        "票种缺价", "missingPrices", payload["missingPrices"],
        lambda item: f"{item['sku']} · {item['name'] or '—'} · 缺 { '、'.join(item['missingLabels']) }",
    )
    section(
        "分类未映射国标目录族", "unmappedCategories", payload["unmappedCategories"],
        lambda item: f"{item['category']} · {item['skuCount']} 个 SKU",
    )
    return "\n".join(lines)


def _master_data_gaps(arguments, ctx):
    """近 N 天采购涉及的供应商 / 图片 / 票种价格 / 国标分类映射缺口。只读。"""
    days = _limit(arguments.get("days"), 30, 365)
    today = _tool_today(arguments)
    invoice_type = str(arguments.get("invoice_type") or "").strip()
    if invoice_type and invoice_type not in INVOICE_LABELS:
        raise ToolError("票种只能是：" + "、".join(INVOICE_LABELS))
    wanted_types = [invoice_type] if invoice_type else list(INVOICE_LABELS)
    year_arg = str(arguments.get("year") or "").strip()
    if year_arg and not re.fullmatch(r"\d{4}", year_arg):
        raise ToolError("统计年度必须是四位数字")
    years = [int(year_arg)] if year_arg else [today.year]
    if not year_arg:
        start = today - timedelta(days=days)
        if start.year < today.year:
            years.append(start.year)

    rows = []
    for year in years:
        chunk, _ = ctx.rows(str(year))
        rows.extend(chunk or [])
    cutoff = (today - timedelta(days=days)).isoformat()
    window = [row for row in rows if (day(row.get("采购日期")) or "") >= cutoff]

    from ..supplier_master import load_supplier_book
    try:
        supplier_book = load_supplier_book(root=ctx.root)
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    products = _load_config_object(ctx.root, "products.json")
    mapping = _load_config_object(ctx.root, "gb_category_map.json")
    ignored = {str(item).strip() for item in (mapping.get("ignore") or [])}

    missing_suppliers: dict[str, dict] = {}
    images: dict[str, dict] = {}
    prices: dict[str, dict] = {}
    categories: dict[str, dict] = {}

    for row in window:
        supplier = text(row.get("item_supplier_id") or row.get("seller"))
        sku = text(row.get("商品编码"))
        style = text(row.get("款式编码"))
        name = text(row.get("商品名称"))
        category = text(row.get("item_sku_other_3")) or "未分类"
        buyer = text(row.get("采购员"))
        po_id = text(row.get("采购单号"))
        if supplier and supplier_book.lookup(supplier) is None:
            item = missing_suppliers.setdefault(
                supplier, {"name": supplier, "poIds": set(), "buyers": set()},
            )
            if po_id:
                item["poIds"].add(po_id)
            if buyer:
                item["buyers"].add(buyer)
        if not sku:
            continue
        product = products.get(sku) or products.get(style) or {}
        image = resolve_product_image(product, sku=sku, style=style, root=ctx.root)
        if image.get("status") != "ready":
            item = images.setdefault(
                sku, {"sku": sku, "style": style, "name": name, "poIds": set()},
            )
            if po_id:
                item["poIds"].add(po_id)
            if name and not item["name"]:
                item["name"] = name
        missing_types = [
            key for key in wanted_types
            if (product.get("prices") or {}).get(key) is None
        ]
        if missing_types:
            item = prices.setdefault(
                sku, {"sku": sku, "name": name, "missingTypes": set()},
            )
            item["missingTypes"].update(missing_types)
            if name and not item["name"]:
                item["name"] = name
        if category not in ignored and not family_ids_for(category, mapping):
            item = categories.setdefault(
                category, {"category": category, "skus": set()},
            )
            item["skus"].add(sku)

    def cap(items):
        return items[:GAP_LIST_CAP]

    supplier_list = cap(sorted(
        ({
            "name": item["name"],
            "poCount": len(item["poIds"]),
            "buyer": "、".join(sorted(item["buyers"])),
        } for item in missing_suppliers.values()),
        key=lambda item: (-item["poCount"], item["name"]),
    ))
    image_list = cap(sorted(
        ({
            "sku": item["sku"], "style": item["style"], "name": item["name"],
            "poCount": len(item["poIds"]),
        } for item in images.values()),
        key=lambda item: (-item["poCount"], item["sku"]),
    ))
    price_list = cap(sorted(
        ({
            "sku": item["sku"], "name": item["name"],
            "missingTypes": sorted(item["missingTypes"]),
            "missingLabels": [INVOICE_LABELS[key] for key in sorted(item["missingTypes"])],
        } for item in prices.values()),
        key=lambda item: item["sku"],
    ))
    category_list = cap(sorted(
        ({
            "category": item["category"], "skuCount": len(item["skus"]),
        } for item in categories.values()),
        key=lambda item: (-item["skuCount"], item["category"]),
    ))
    payload = {
        "today": today.isoformat(),
        "days": days,
        "rowCount": len(window),
        "invoiceType": INVOICE_LABELS[invoice_type] if invoice_type else "",
        "missingSuppliers": supplier_list,
        "missingImages": image_list,
        "missingPrices": price_list,
        "unmappedCategories": category_list,
        "counts": {
            "missingSuppliers": len(missing_suppliers),
            "missingImages": len(images),
            "missingPrices": len(prices),
            "unmappedCategories": len(categories),
        },
        "note": "供应商按本机「供应商管理」表的简称匹配 ERP seller；图片按商品映射 / SKU 本地图 / 缓存；缺价指 products.json 该票种单价为 null",
    }
    payload["markdown"] = _gaps_markdown(payload)
    counts = payload["counts"]
    return tool_envelope(
        summary=(
            f"近 {days} 天主数据缺口：供应商未维护 {counts['missingSuppliers']}、"
            f"SKU 无图 {counts['missingImages']}、缺价 {counts['missingPrices']}、"
            f"分类未映射 {counts['unmappedCategories']}"
        ),
        data=payload,
        hint="不要编造未维护的供应商或价格",
    )


def _require_order_source(ctx):
    if ctx.setting is None:
        raise ToolError("订单镜像查询尚未配置")
    return ctx.setting


def _search_sales_orders(arguments, ctx):
    """让 Agent 先把员工说的平台单号/店铺等解析成明确 ERP o_id。"""
    query = str(arguments.get("query") or "").strip()
    source_sku = str(arguments.get("source_sku") or "").strip()
    shop = str(arguments.get("shop") or "").strip()
    date_from = str(arguments.get("date_from") or "").strip()
    date_to = str(arguments.get("date_to") or "").strip()
    status = arguments.get("status")
    if status is None:
        status = arguments.get("status_include")
    result = fetch_exchange_orders(
        _require_order_source(ctx), ctx.env_path,
        query=query, source_sku=source_sku, shop=shop, status=status,
        date_from=date_from, date_to=date_to,
        limit=_limit(arguments.get("limit"), 20, 100),
    )
    if not result.get("configured"):
        raise ToolError(result.get("message") or "订单镜像暂不可用")
    orders = result.get("orders") or []
    oids = [item.get("oId") for item in orders if item.get("oId")]
    filters = result.get("filters") or {}
    shown = "、".join(oids[:8])
    extra = f"，o_id：{shown}" if shown else ""
    hint = "结果不唯一就列出候选追问，不要猜 o_id"
    if len(oids) == 1:
        hint = "已收到唯一 o_id，可再查明细或登记换货"
    elif not oids:
        hint = "没有命中订单；放宽状态/日期或核对 SKU 后再查，不要编造单号"
    return tool_envelope(
        summary=f"命中 {len(orders)} 张销售订单{extra}",
        data={"count": len(orders), "orders": orders, "oIds": oids, "filters": filters},
        hint=hint,
    )


def _get_sales_order_items(arguments, ctx):
    """读取明确订单内的 SKU，供自然语言换货消歧，不执行 ERP 写操作。"""
    o_ids = _sku_list(arguments.get("o_ids"), "o_ids")
    result = fetch_exchange_order_items(
        _require_order_source(ctx), ctx.env_path, o_ids=o_ids,
    )
    if not result.get("configured"):
        raise ToolError(result.get("message") or "订单明细镜像暂不可用")
    items = result.get("items") or []
    shown, truncated = clip_rows(
        items, LINE_CAP, keep=("sku", "styleCode", "name", "totalQuantity", "orderCount"),
    )
    return tool_envelope(
        summary=f"{result.get('selectedOrderCount') or 0} 张订单共 {len(items)} 个 SKU",
        data={**result, "items": shown, "itemCount": len(items)},
        truncated=truncated,
        hint="确认源 SKU 后再登记换货，不要口算数量",
    )


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
            operator=ctx.operator, user_id=ctx.user_id, session_id=ctx.session_id,
            run_id=ctx.run_id, pending_action_id=ctx.action_id,
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
    payment_key = str(arguments.get("payment_option") or options.get("paymentOption") or "")
    payment_text = next(
        (item["text"] for item in options.get("paymentOptions") or [] if item["key"] == payment_key),
        options.get("paymentMethod") or "",
    )
    return {
        "purchaseOrderNo": po_id,
        "invoiceType": invoice_type,
        "invoiceLabel": INVOICE_LABELS[invoice_type],
        "taxRate": rate,
        "paymentOption": payment_key,
        "paymentTerms": payment_text,
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
    from ..paths import local_dir
    output_dir = local_dir("outputs", root=ctx.root) / "agent" / contract_id
    generate_contract(
        po_id, invoice_type, output_dir / "contract.xlsx",
        tax_rate=arguments.get("tax_rate"),
        payment_option=arguments.get("payment_option"),
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


def _lookup_orders_for_impact(ctx, o_ids) -> list | None:
    if ctx.setting is None:
        return None
    try:
        from ..order_source import fetch_exchange_orders
    except Exception:
        return None
    found = []
    try:
        for oid in list(o_ids or [])[:20]:
            result = fetch_exchange_orders(ctx.setting, ctx.env_path, query=str(oid), limit=5)
            for order in result.get("orders") or []:
                if str(order.get("oId") or "") == str(oid):
                    found.append(order)
                    break
    except Exception:
        return None
    return found


def _exchange_impact(arguments, ctx, payload) -> dict:
    from ..exchange.impact import assess_exchange_impact
    products = None
    try:
        source = str(arguments.get("source_sku") or "").strip()
        target = str(arguments.get("target_sku") or "").strip()
        found = []
        if target:
            found.extend(fetch_exchange_products(ctx.env_path, query=target, limit=20))
        if source:
            found.extend(fetch_exchange_products(ctx.env_path, query=source, limit=20))
        products = found
    except Exception:
        products = None
    open_jobs = []
    if ctx.exchange is not None:
        try:
            open_jobs = ctx.exchange.list_jobs(limit=100)
        except Exception:
            open_jobs = []
    return assess_exchange_impact(
        payload,
        products=products,
        orders=_lookup_orders_for_impact(ctx, (payload.get("targets") or {}).get("o_ids") or []),
        open_jobs=open_jobs,
    )


def _exchange_preview(arguments, ctx):
    payload = _exchange_payload({**arguments, "env_path": ctx.env_path})
    if ctx.exchange is None:
        raise ToolError("换货子系统尚未启用")
    rules, targets = ctx.exchange.validate_submission(payload)
    status = ctx.exchange.status()
    impact = _exchange_impact(arguments, ctx, payload)
    if impact.get("decision") == "block":
        first = (impact.get("blockers") or [{}])[0]
        raise ToolError(first.get("message") or "换货影响分析阻断，不能进入预览")
    return {
        "sourceSku": rules["replacements"][0]["from"],
        "targetSku": rules["replacements"][0]["to"],
        "orderCount": len(targets["o_ids"]),
        "oIds": targets["o_ids"][:50],
        "onlineWorkers": status.get("onlineWorkers", 0),
        "impact": impact,
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


def _optional_oids(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,，;；]+", value.strip())
    if not isinstance(value, list):
        raise ToolError("o_ids 必须是列表")
    items = []
    for raw in value:
        item = str(raw or "").strip()
        if item and item not in items:
            items.append(item)
    return items


def _insole_written(ctx) -> dict:
    return load_written_insole_orders(ctx.setting, root=ctx.root)


def _insole_reserved(ctx) -> dict:
    if ctx.setting is None:
        return {}
    db = resolve_repo_path(
        ctx.setting("AGENT_DATABASE_PATH", "files/data/agent.sqlite3"),
        root=ctx.root,
    )
    return load_reserved_insole_orders(
        db, exclude_action_id=ctx.action_id, viewer=ctx.operator,
    )


def _insole_shop(arguments) -> str:
    return str(arguments.get("shop") or "").strip()


def _frozen_insole_orders(arguments, written: dict, reserved: dict) -> list[dict]:
    rows = []
    for item in arguments.get("orders") or []:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("oId") or item.get("o_id") or "").strip()
        target = str(item.get("targetSku") or item.get("target_sku") or "").strip()
        if not oid or not target:
            continue
        if oid in written or oid in reserved:
            continue
        rows.append({
            "o_id": oid,
            "so_id": str(item.get("soId") or item.get("so_id") or ""),
            "status": str(item.get("status") or ""),
            "shop": str(item.get("shop") or ""),
            "target_sku": target,
            "source_sku": str(item.get("sourceSku") or item.get("source_sku") or ""),
        })
    return rows


def _locate_insole(arguments, ctx):
    shop = _insole_shop(arguments)
    try:
        located = locate_insole_orders(
            ctx.setting, ctx.env_path,
            shop=shop, o_ids=_optional_oids(arguments.get("o_ids")),
            written=_insole_written(ctx), reserved=_insole_reserved(ctx),
            root=ctx.root,
        )
    except (OrderSourceError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    processable = [public_order(row) for row in located["processable"]]
    parked = [public_order(row) for row in located["parked"]]
    return tool_envelope(
        summary=(
            f"鞋垫待处理 {located['processableCount']} 单，"
            f"暂不处理 {located['parkedCount']} 单"
        ),
        data={
            "shop": located["shop"],
            "sourceSku": located["sourceSku"],
            "sync": located["sync"],
            "processableCount": located["processableCount"],
            "parkedCount": located["parkedCount"],
            "skippedCount": located["skippedCount"],
            "oIds": located["oIds"],
            "orders": processable,
            "parked": parked[:30],
            "markdown": format_insole_list(located),
        },
        truncated=len(located["parked"]) > 30,
        hint="要处理必须再走 process_insole_orders；员工回复「确认」后由后端写入。Delivering 不要自行加入",
    )


def _insole_preview(arguments, ctx):
    shop = _insole_shop(arguments)
    try:
        located = locate_insole_orders(
            ctx.setting, ctx.env_path,
            shop=shop, o_ids=_optional_oids(arguments.get("o_ids")),
            written=_insole_written(ctx), reserved=_insole_reserved(ctx),
            root=ctx.root,
        )
    except (OrderSourceError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    arguments["o_ids"] = list(located["oIds"])
    arguments["shop"] = shop
    arguments["orders"] = [
        {"oId": row["o_id"], "targetSku": row.get("target_sku") or "",
         "soId": row.get("so_id") or "", "status": row.get("status") or "",
         "shop": row.get("shop") or ""}
        for row in located["processable"]
    ]
    if not located["processable"]:
        raise ToolError(
            format_insole_list(located)
            + "\n没有可处理的鞋垫订单（需要 Question / WaitConfirm；半码按码数舍去小数后映射）。"
        )
    return {
        "shop": shop,
        "sourceSku": located["sourceSku"],
        "processableCount": located["processableCount"],
        "parkedCount": located["parkedCount"],
        "oIds": located["oIds"],
        "orders": [public_order(row) for row in located["processable"]],
        "parked": [public_order(row) for row in located["parked"][:20]],
        "markdown": format_insole_list(located),
        "note": "直接回复「确认」即可，由后端串行写入 ERP，不用去换货页。写完会再发一条【任务完成】结果。发货中不处理；半码按码数舍去小数。",
    }


def _process_insole(arguments, ctx):
    shop = _insole_shop(arguments)
    o_ids = _optional_oids(arguments.get("o_ids"))
    written = _insole_written(ctx)
    reserved = _insole_reserved(ctx)
    started = time.monotonic()
    frozen = _frozen_insole_orders(arguments, written, reserved)
    if frozen:
        located = {
            "processable": frozen,
            "parked": [],
            "skipped": [],
            "processableCount": len(frozen),
            "parkedCount": 0,
            "skippedCount": 0,
            "oIds": [row["o_id"] for row in frozen],
            "shop": shop,
            "sourceSku": "",
            "sync": {},
        }
    else:
        if not o_ids:
            raise ToolError("没有可执行的订单清单，请先定位并确认")
        try:
            located = locate_insole_orders(
                ctx.setting, ctx.env_path, shop=shop, o_ids=o_ids,
                written=written, reserved=reserved, root=ctx.root,
            )
        except (OrderSourceError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
    if not located["processable"]:
        if any(oid in written for oid in o_ids):
            raise ToolError("这些订单已经写入过，镜像尚未跟上，不再重复执行")
        if any(oid in reserved for oid in o_ids):
            raise ToolError("这些订单已有他人待确认或正在写入，本批不再重复执行")
        raise ToolError("确认时这些订单已不可处理（可能已换过或状态变了）")
    try:
        result = execute_insole_orders(ctx.erp, located["processable"])
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    ok_ids = {str(item.get("o_id") or "") for item in result.get("succeeded") or []}
    fail_map = {
        str(item.get("o_id") or ""): str(item.get("error") or item.get("reason") or "")
        for item in result.get("failed") or []
    }
    skip_map = {
        str(item.get("o_id") or ""): str(item.get("reason") or "")
        for item in result.get("plans") or [] if not item.get("ok")
    }
    log = []
    for row in located["processable"]:
        oid = str(row["o_id"])
        if oid in ok_ids:
            log.append({"oId": oid, "targetSku": row.get("target_sku") or "", "result": "ok"})
        elif oid in fail_map:
            log.append({
                "oId": oid, "targetSku": row.get("target_sku") or "",
                "result": "failed", "error": fail_map[oid],
            })
        else:
            log.append({
                "oId": oid, "targetSku": row.get("target_sku") or "",
                "result": "skipped", "error": skip_map.get(oid, ""),
            })
    writes = [
        {"o_id": item["oId"], "target_sku": item.get("targetSku") or ""}
        for item in log if item.get("result") == "ok" and item.get("oId")
    ]
    if writes:
        remember_insole_writes(writes, root=ctx.root)
        sync_insole_mirror(ctx.env_path, writes, mirror=getattr(ctx, "mirror", None))
    return {
        "okCount": result["okCount"],
        "skippedCount": result["skippedCount"],
        "failedCount": result["failedCount"],
        "attempted": result["attempted"],
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "prepareMs": result.get("prepareMs"),
        "writeMs": result.get("writeMs"),
        "oIds": [row["o_id"] for row in located["processable"]],
        "failed": result.get("failed") or [],
        "log": log,
        "reconciliation": result.get("reconciliation") or {},
        "evidence": result.get("evidence") or {},
    }


def _reminder_selection(arguments, ctx):
    rows, meta = ctx.followup_rows()
    reminders = build_reminders(rows, arguments.get("today"), profile="followup")
    buckets = arguments.get("buckets") or list(FOLLOWUP_URGENT)
    orders, matched = filter_orders(
        reminders, buckets=buckets,
        buyer=scoped_buyers(arguments.get("buyer"), ctx),
        limit=_limit(arguments.get("limit"), 100, 500),
    )
    return reminders, orders, matched, meta


def _freeze_reminder_orders(orders) -> list[dict]:
    frozen = []
    for item in orders or []:
        if not isinstance(item, dict):
            continue
        frozen.append({
            "purchaseOrderNo": item.get("purchaseOrderNo") or "",
            "buyer": item.get("buyer") or "",
            "supplier": item.get("supplier") or "",
            "bucket": item.get("bucket") or "",
            "waveLabel": item.get("waveLabel") or "",
            "deliveryDate": item.get("deliveryDate") or "",
            "remainingDays": item.get("remainingDays"),
            "purchaseQty": item.get("purchaseQty", 0),
            "pendingQty": item.get("pendingQty", 0),
        })
    return frozen


def _reminder_preview(arguments, ctx):
    if ctx.notifier is None:
        raise ToolError("钉钉推送尚未启用")
    reminders, orders, matched, _ = _reminder_selection(arguments, ctx)
    if not orders:
        raise ToolError("当前口径下没有需要催办的采购单，不发送空提醒")
    buyers = sorted({item["buyer"] for item in orders})
    targets = ctx.notifier.describe_targets(buyers)
    frozen = _freeze_reminder_orders(orders)
    arguments["today"] = reminders["today"]
    arguments["orders"] = frozen
    arguments["poIds"] = [item["purchaseOrderNo"] for item in frozen if item["purchaseOrderNo"]]
    arguments["buyers"] = buyers
    arguments["atUserIds"] = list(targets.get("atUserIds") or [])
    return {
        "today": reminders["today"],
        "orderCount": len(frozen),
        "matched": matched,
        "buyers": buyers,
        "poIds": arguments["poIds"],
        "targets": targets,
        "purchaseQty": sum(item.get("purchaseQty", 0) for item in frozen),
        "pendingQty": sum(item["pendingQty"] for item in frozen),
        "text": reminder_markdown(reminders, frozen)[:3000],
        "note": "确认后按这份清单和收件人发送，不再重查跟单池。清单过期请重新发起。",
    }


def _send_reminder(arguments, ctx):
    if ctx.notifier is None:
        raise ToolError("钉钉推送尚未启用")
    frozen = _freeze_reminder_orders(arguments.get("orders") or [])
    if frozen:
        reminders = {"today": arguments.get("today") or ""}
        orders = frozen
    else:
        reminders, orders, _, _ = _reminder_selection(arguments, ctx)
    if not orders:
        raise ToolError("当前口径下没有需要催办的采购单，不发送空提醒")
    return ctx.notifier.send_reminders(
        reminders, orders,
        idempotency_key=f"agent-action-{ctx.action_id}" if ctx.action_id else None,
        operator=ctx.operator,
        at_user_ids=arguments.get("atUserIds") or None,
    )


# ------------------------------------------------------------------- 注册表


YEAR_PARAM = {"type": "string", "description": "统计年度，四位数字；缺省用当前年度"}
TODAY_PARAM = {"type": "string", "description": "以哪天为今天计算剩余天数，YYYY-MM-DD；缺省服务器当天"}
BUCKETS_PARAM = {
    "type": "array",
    "items": {"type": "string", "enum": list(FOLLOWUP_ORDER)},
    "description": "催办档位：overdue 已逾期 / d3 剩≤3天 / d10 剩≤10天 / later 暂不提醒 / unscheduled 未排期；缺省为需催三档",
}


def _quality_or_error(ctx):
    if ctx.quality is None:
        raise ToolError("品控台账未启用")
    return ctx.quality


def _record_quality_issue(arguments, ctx):
    ledger = _quality_or_error(ctx)
    return ledger.record(
        description=str(arguments.get("description") or "").strip(),
        supplier=str(arguments.get("supplier") or "").strip(),
        po_id=str(arguments.get("po_id") or "").strip(),
        sku=str(arguments.get("sku") or "").strip(),
        severity=str(arguments.get("severity") or "").strip(),
        reporter=ctx.operator, channel=ctx.channel, run_id=ctx.run_id,
        raw_text=str(arguments.get("description") or ""),
    )


def _record_quality_preview(arguments, ctx):
    return {
        "description": str(arguments.get("description") or "").strip(),
        "supplier": str(arguments.get("supplier") or "").strip(),
        "po_id": str(arguments.get("po_id") or "").strip(),
        "sku": str(arguments.get("sku") or "").strip(),
        "severity": str(arguments.get("severity") or "").strip(),
        "note": "确认后写入本地品控台账，不改 ERP。",
    }


def _list_quality_issues(arguments, ctx):
    ledger = _quality_or_error(ctx)
    issues = ledger.query(query=str(arguments.get("query") or "今天"))
    summary = ledger.summary(issues)
    return {
        "summary": summary,
        "truncated": len(issues) > 30,
        "issues": issues[:30],
        "markdown": ledger.format_query(str(arguments.get("query") or "今天")),
    }


def _push_quality_report(arguments, ctx):
    scheduler = getattr(ctx.quality, "scheduler", None) if ctx.quality else None
    if scheduler is None:
        raise ToolError("品控日报调度未装配")
    return scheduler.run_once(operator=ctx.operator or "agent")


def _push_quality_preview(arguments, ctx):
    ledger = _quality_or_error(ctx)
    today = business_today().isoformat()
    issues = ledger.list_for_report(today)
    return {"today": today, "count": len(issues), "note": "将把当日品控日报发到钉钉群"}


def _resolve_quality_issue(arguments, ctx):
    ledger = _quality_or_error(ctx)
    return ledger.resolve(
        str(arguments.get("issue_id") or "").strip(),
        str(arguments.get("resolution") or "").strip(),
    )


def _resolve_quality_preview(arguments, ctx):
    return {
        "issue_id": str(arguments.get("issue_id") or "").strip(),
        "resolution": str(arguments.get("resolution") or "").strip(),
        "note": "确认后关闭该品控记录，不改 ERP。",
    }


def _cancel_quality_issue(arguments, ctx):
    ledger = _quality_or_error(ctx)
    return ledger.cancel(str(arguments.get("issue_id") or "").strip())


def _cancel_quality_preview(arguments, ctx):
    return {
        "issue_id": str(arguments.get("issue_id") or "").strip(),
        "note": "确认后撤销该品控记录，不改 ERP。",
    }


def _dropship_preview(arguments, ctx):
    from ..dropship.workbook import dropship_filename
    return {
        "pool": "代发订单未安排",
        "filename": dropship_filename(),
        "note": "将打开 ERP 揭开收货并写入当日 Excel，约需数分钟。不改 ERP 单据。",
    }


def _generate_dropship_workbook(arguments, ctx):
    runtime = ctx.erp
    if runtime is None:
        raise ToolError("ERP Digital Worker 未装配。请先 scripts/run_erp_worker.py login")
    from ..dropship.export import export_today_dropship, public_export_result
    try:
        payload = export_today_dropship(runtime, root=ctx.root, env_path=ctx.env_path)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    return public_export_result(payload)


def build_registry(*, with_forecast=True, with_exchange=True, with_notifier=True,
                   with_quality=False) -> ToolRegistry:
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
        description=(
            "按采购单号读取单头与全部商品明细：供应商、采购员、交期、数量、已入库、待入库、ERP 单价、已维护的票种价格，以及该行已保存的执行标准（GB/T…，不是商品条码）。"
            "问到货/待入库用这个；催逾期采购单用 delivery_reminders；看板金额用 dashboard_summary；"
            "要看出库订单用 search_sales_orders。要看该类有哪些候选国标请用 lookup_gb_standards。"
        ),
        parameters={"type": "object", "properties": {
            "po_id": {"type": "string", "description": "ERP 采购单号，纯数字"},
        }, "required": ["po_id"]},
        risk="L0", handler=_get_purchase_order,
    ))
    registry.register(Tool(
        name="delivery_reminders",
        description=(
            "按跟单三档（剩≤10天 / ≤3天 / 已逾期）汇总已确认未完结、排除返修的采购单，可按档位、采购员、供应商过滤。"
            "只看采购交期，不要用来查销售订单、换货或看板金额。发到钉钉用 send_delivery_reminder。"
        ),
        parameters={"type": "object", "properties": {
            "buckets": BUCKETS_PARAM,
            "buyer": {"type": "string", "description": "采购员姓名，支持部分匹配；填「我名下」只看绑定人"},
            "supplier": {"type": "string", "description": "供应商名称，支持部分匹配"},
            "limit": {"type": "integer", "description": "返回采购单条数，默认 30"},
            "today": TODAY_PARAM,
        }},
        risk="L0", handler=_delivery_reminders,
    ))
    registry.register(Tool(
        name="dashboard_summary",
        description=(
            "采购看板统计：单数、明细行数、采购金额、数量、已入库、待入库、入库率，以及采购员/供应商/品类金额 Top。"
            "问今年买了多少、入库率用这个；列逾期采购单用 delivery_reminders；查某一张单用 get_purchase_order。"
        ),
        parameters={"type": "object", "properties": {
            "year": YEAR_PARAM,
            "buyer": {"type": "string", "description": "只看某个采购员；填「我名下」只看绑定人"},
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
    registry.register(Tool(
        name="master_data_gaps",
        description=(
            "汇总近 N 天采购涉及的主数据缺口：供应商未在本机供应商管理表维护、SKU 无图、"
            "所选票种缺价、分类未映射国标目录族。问「哪些供应商还没维护」「哪些 SKU 没图」时用。"
            "只读，输出 markdown 可直接发钉钉；不要逐张单猜测或编造全称/单价。"
        ),
        parameters={"type": "object", "properties": {
            "days": {"type": "integer", "description": "回溯天数，默认 30，最多 365"},
            "invoice_type": {"type": "string", "enum": list(INVOICE_LABELS),
                             "description": "只检查某票种缺价：no_invoice / normal_invoice / special_invoice；缺省三种都查"},
            "year": YEAR_PARAM, "today": TODAY_PARAM,
        }},
        risk="L0", handler=_master_data_gaps,
    ))
    if with_exchange:
        registry.register(Tool(
            name="search_sales_orders",
            description=(
                "按内部订单号、平台订单号、店铺、日期、状态或源 SKU 搜索订单镜像，返回明确 ERP o_id。"
                "处理「异常订单」换货时：单号不明确就用这个工具收候选。"
                "未指定状态且不是在查某一张明确单号时，默认只看待发货。"
                "不要用交期催办工具。自然语言换货在 o_id 不明确时必须先调用。"
            ),
            parameters={"type": "object", "properties": {
                "query": {"type": "string", "description": "订单号、平台单号或买家关键词，可空"},
                "source_sku": {"type": "string", "description": "可选：订单必须包含的源 SKU"},
                "shop": {"type": "string", "description": "可选：店铺名，部分匹配"},
                "status": {
                    "type": "string",
                    "description": "订单状态，默认待发货；查某一张明确单号时可不传；传 all 表示不限状态",
                },
                "date_from": {"type": "string", "description": "下单日起 YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "下单日止 YYYY-MM-DD"},
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
            "payment_option": {
                "type": "string",
                "description": (
                    "付款方式条款键，取值见 get_purchase_order 返回的 paymentOptions；"
                    "缺省用 ERP 付款方式预选。不要自己编写付款条款。"
                ),
            },
            "gb_overrides": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "明细 poi_id 到执行标准号（如 GB/T 9832-2026）的映射，可空",
            },
        }, "required": ["po_id", "invoice_type"]},
        risk="L1", handler=_generate_contract, preview=_contract_preview,
        title=lambda args: f"生成采购合同 {args.get('po_id', '')}（{INVOICE_LABELS.get(str(args.get('invoice_type')), '')}）",
    ))
    registry.register(Tool(
        name="generate_dropship_workbook",
        description=(
            "抓取 ERP「代发订单未安排」并生成当日 YYMMDD-代发.xlsx。"
            "会先给出文件名供员工确认；确认后打开 Digital Worker 揭开收货并写入，约需数分钟。"
            "不改 ERP 单据。问「导出代发」「今天的代发表」时用。"
        ),
        parameters={"type": "object", "properties": {}},
        risk="L1", handler=_generate_dropship_workbook, preview=_dropship_preview,
        title=lambda args: "生成代发订单 Excel",
        side_effect="file",
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
        registry.register(Tool(
            name="locate_insole_orders",
            description=(
                "查询抖音/快手/视频号订单里仍挂着旧鞋垫 SKU（XZ25401308-101）的清单，"
                "按同单鞋子毫米数映射目标鞋垫。只读，不写 ERP。"
                "默认只把 Question / WaitConfirm 标为待处理；Delivering 只列出。"
                "半码按码数舍去小数后映射。员工说「查一下要换的鞋垫订单」时用这个。"
            ),
            parameters={"type": "object", "properties": {
                "shop": {"type": "string", "description": "店铺关键词，默认抖音+快手+视频号"},
                "o_ids": {"type": "array", "items": {"type": "string"},
                          "description": "可选：只看这些内部订单号"},
            }},
            risk="L0", handler=_locate_insole,
        ))
        registry.register(Tool(
            name="process_insole_orders",
            description=(
                "按定位清单串行更换鞋垫（抖音/快手/视频号）。必须先给出订单信息供员工确认；"
                "员工回复「确认」后由后端写入 ERP，不要叫员工去换货页，不要再次调用本工具。"
                "不要把 Delivering 加进执行清单。指定源→目标的普通换货仍用 submit_exchange_dry_run。"
            ),
            parameters={"type": "object", "properties": {
                "shop": {"type": "string", "description": "店铺关键词，默认抖音+快手+视频号"},
                "o_ids": {"type": "array", "items": {"type": "string"},
                          "description": "要处理的内部订单号；缺省为当前全部可处理单"},
                "orders": {"type": "array", "items": {"type": "object"},
                           "description": "预览冻结的订单（含目标鞋垫），确认时不再重查全库"},
            }},
            risk="L2", handler=_process_insole, preview=_insole_preview,
            title=lambda args: "处理鞋垫订单",
            side_effect="erp",
        ))
    if with_notifier:
        registry.register(Tool(
            name="send_delivery_reminder",
            description="把交期催办清单私聊发给已绑定采购员。未绑定的人跳过，不发群。对外动作，确认前会先给出完整清单。",
            parameters={"type": "object", "properties": {
                "buckets": BUCKETS_PARAM,
                "buyer": {"type": "string", "description": "只催某个采购员；填「我名下」只看绑定人"},
                "limit": {"type": "integer", "description": "最多包含多少张采购单，默认 100"},
                "today": TODAY_PARAM,
                "orders": {"type": "array", "items": {"type": "object"},
                           "description": "预览冻结的催办清单，确认时不再重查"},
                "poIds": {"type": "array", "items": {"type": "string"}},
                "buyers": {"type": "array", "items": {"type": "string"}},
                "atUserIds": {"type": "array", "items": {"type": "string"}},
            }},
            risk="L2", handler=_send_reminder, preview=_reminder_preview,
            title=lambda args: "发送交期催办到已绑定采购员",
        ))
    if with_quality:
        registry.register(Tool(
            name="record_quality_issue",
            description="把一条品控问题记入本地台账。字段由你抽取，员工确认后才落库。不要编造供应商或单号。",
            parameters={"type": "object", "properties": {
                "description": {"type": "string", "description": "问题描述，必填"},
                "supplier": {"type": "string", "description": "供应商简称"},
                "po_id": {"type": "string", "description": "采购单号"},
                "sku": {"type": "string", "description": "商品编码"},
                "severity": {"type": "string", "description": "一般或严重"},
            }, "required": ["description"], "additionalProperties": False},
            risk="L1", handler=_record_quality_issue, preview=_record_quality_preview,
            title=lambda args: "登记品控问题",
        ))
        registry.register(Tool(
            name="list_quality_issues",
            description="按今天/本周/供应商/未关闭查询品控台账。",
            parameters={"type": "object", "properties": {
                "query": {"type": "string", "description": "今天 / 本周 / 未关闭 / 供应商名"},
            }},
            risk="L0", handler=_list_quality_issues,
        ))
        registry.register(Tool(
            name="push_quality_report",
            description="手动把当日品控日报发到钉钉群。对外动作，需确认。",
            parameters={"type": "object", "properties": {}},
            risk="L2", handler=_push_quality_report, preview=_push_quality_preview,
            title=lambda args: "发送品控日报",
        ))
        registry.register(Tool(
            name="resolve_quality_issue",
            description="关闭一条品控记录。必须有 6 位编号；确认后才改台账，不改 ERP。",
            parameters={"type": "object", "properties": {
                "issue_id": {"type": "string", "description": "品控编号，6 位十六进制"},
                "resolution": {"type": "string", "description": "关闭说明，可空"},
            }, "required": ["issue_id"], "additionalProperties": False},
            risk="L1", handler=_resolve_quality_issue, preview=_resolve_quality_preview,
            title=lambda args: f"关闭品控 {args.get('issue_id', '')}",
        ))
        registry.register(Tool(
            name="cancel_quality_issue",
            description="撤销一条品控记录。必须有 6 位编号；确认后才改台账。已关闭的不能再撤销当新登记。",
            parameters={"type": "object", "properties": {
                "issue_id": {"type": "string", "description": "品控编号，6 位十六进制"},
            }, "required": ["issue_id"], "additionalProperties": False},
            risk="L1", handler=_cancel_quality_issue, preview=_cancel_quality_preview,
            title=lambda args: f"撤销品控 {args.get('issue_id', '')}",
        ))
    return registry
