# -*- coding: utf-8 -*-
"""交期催办口径：把采购明细行折算成按采购单的催办清单。

台账页、Agent 工具和钉钉推送默认走 `profile=followup`（跟单三档），
与前端 `frontend/src/pages/ledger/` 一致，见 README「交期提醒台账」：
池子是已确认未完结、排除返修；交期取该行 `item_delivery_date`，为空退到
`最早预计到货日期`；只算还有待入库数量的明细行；一张采购单的交期是所有
待入库行里最早的那个，档位取最急的一档（≤10 天 / ≤3 天 / 已逾期）。

`profile=ledger` 仍保留旧四波（T-20 / T-10 / T-1 / 逾期），供对照测试。
本模块不碰数据库也不碰网络。
"""
from __future__ import annotations

from datetime import date, datetime

from .business_time import business_now, business_today
from .procurement_data import day, integer, number, text


WAVES = {
    "overdue": {"label": "逾期催办", "wave": "第 4 次 · 逾期", "action": "逐日追"},
    "t1": {"label": "T-1", "wave": "第 3 次 · T-1", "action": "核对物流单号"},
    "t10": {"label": "T-10", "wave": "第 2 次 · T-10", "action": "确认发货计划"},
    "t20": {"label": "T-20", "wave": "第 1 次 · T-20", "action": "确认排产进度"},
    "later": {"label": "暂不提醒", "wave": "未进提醒窗", "action": "无需动作"},
    "unscheduled": {"label": "未排期", "wave": "无交期", "action": "先补交期"},
}
# 需要真正发提醒的四波，按紧急程度从急到缓
URGENT_BUCKETS = ("overdue", "t1", "t10", "t20")
BUCKET_ORDER = URGENT_BUCKETS + ("later", "unscheduled")

FOLLOWUP_WAVES = {
    "overdue": {"label": "已逾期", "wave": "交期已过", "action": "逐日追"},
    "d3": {"label": "剩 3 天", "wave": "交期 − 3 天", "action": "确认发货"},
    "d10": {"label": "剩 10 天", "wave": "交期 − 10 天", "action": "确认排产/发货计划"},
    "later": {"label": "暂不提醒", "wave": "未进提醒窗", "action": "无需动作"},
    "unscheduled": {"label": "未排期", "wave": "无交期", "action": "先补交期"},
}
FOLLOWUP_URGENT = ("overdue", "d3", "d10")
FOLLOWUP_ORDER = FOLLOWUP_URGENT + ("later", "unscheduled")


def classify(remaining_days):
    """按剩余天数返回催办档位；None 表示没有交期。"""
    if remaining_days is None:
        return "unscheduled"
    if remaining_days < 0:
        return "overdue"
    if remaining_days <= 1:
        return "t1"
    if remaining_days <= 10:
        return "t10"
    if remaining_days <= 20:
        return "t20"
    return "later"


def classify_followup(remaining_days):
    """跟单三档：剩余 ≤10 天、≤3 天、已逾期。"""
    if remaining_days is None:
        return "unscheduled"
    if remaining_days < 0:
        return "overdue"
    if remaining_days <= 3:
        return "d3"
    if remaining_days <= 10:
        return "d10"
    return "later"


def is_repair_row(row) -> bool:
    """返修退货：单头 labels 或备注。"""
    blob = " ".join(
        text(row.get(key))
        for key in ("标签", "备注", "labels", "remark")
    )
    return "返修退货" in blob or "返修采购单" in blob


def _profile_spec(profile: str) -> dict:
    if profile == "followup":
        return {
            "classify": classify_followup,
            "waves": FOLLOWUP_WAVES,
            "urgent": FOLLOWUP_URGENT,
            "order": FOLLOWUP_ORDER,
        }
    return {
        "classify": classify,
        "waves": WAVES,
        "urgent": URGENT_BUCKETS,
        "order": BUCKET_ORDER,
    }


def _as_date(value):
    value = day(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def build_reminders(rows, today=None, *, profile="ledger"):
    """把明细行折算成按采购单的催办清单。

    `profile=followup` 是跟单三档（已确认、排除返修、剩 10 / 3 / 逾期），
    与交期台账页一致；`ledger` 是旧四波。返回 `{today, orders, buckets, byBuyer, totals}`。
    """
    spec = _profile_spec(profile)
    waves = spec["waves"]
    urgent = spec["urgent"]
    bucket_order = spec["order"]
    today = _as_date(today) or business_today()
    orders = {}
    for row in rows:
        if profile == "followup" and is_repair_row(row):
            continue
        erp_status = text(row.get("erp_status"))
        if profile == "followup" and erp_status and erp_status != "Confirmed":
            continue
        qty = integer(row.get("数量"))
        pending = qty - integer(row.get("item_in_qty"))
        order_no = text(row.get("采购单号"))
        if not order_no:
            continue
        agreed = day(row.get("item_delivery_date"))
        expected = day(row.get("最早预计到货日期"))
        eta = agreed or expected
        entry = orders.get(order_no)
        if entry is None:
            entry = orders[order_no] = {
                "purchaseOrderNo": order_no,
                "orderDate": day(row.get("采购日期")),
                "buyer": text(row.get("采购员")) or "未知",
                "supplier": text(row.get("item_supplier_id")) or "未知",
                "warehouse": text(row.get("仓储方")) or "未指定",
                "deliveryDate": "",
                "dateSource": "",
                "purchaseQty": 0,
                "pendingQty": 0,
                "pendingAmount": 0.0,
                "lineCount": 0,
                "skus": [],
            }
        entry["purchaseQty"] += qty
        if pending <= 0:
            continue
        entry["pendingQty"] += pending
        entry["lineCount"] += 1
        unit_price = number(row.get("基本售价"))
        entry["pendingAmount"] += pending * unit_price
        sku = text(row.get("商品编码"))
        if sku and sku not in entry["skus"]:
            entry["skus"].append(sku)
        if eta and (not entry["deliveryDate"] or eta < entry["deliveryDate"]):
            entry["deliveryDate"] = eta
            entry["dateSource"] = "交期" if agreed else "预计到货"

    result = []
    for entry in orders.values():
        if entry["pendingQty"] <= 0:
            continue
        eta = _as_date(entry["deliveryDate"])
        remaining = (eta - today).days if eta else None
        bucket = spec["classify"](remaining)
        entry.update(
            remainingDays=remaining,
            bucket=bucket,
            waveLabel=waves[bucket]["label"],
            wave=waves[bucket]["wave"],
            action=waves[bucket]["action"],
            pendingAmount=round(entry["pendingAmount"], 2),
            skus=entry["skus"][:8],
        )
        result.append(entry)
    result.sort(key=lambda item: (
        bucket_order.index(item["bucket"]),
        item["deliveryDate"] or "9999-99-99",
        item["purchaseOrderNo"],
    ))

    buckets = {}
    for key in bucket_order:
        members = [item for item in result if item["bucket"] == key]
        buckets[key] = {
            "label": waves[key]["label"],
            "wave": waves[key]["wave"],
            "action": waves[key]["action"],
            "orderCount": len(members),
            "purchaseQty": sum(item["purchaseQty"] for item in members),
            "pendingQty": sum(item["pendingQty"] for item in members),
            "pendingAmount": round(sum(item["pendingAmount"] for item in members), 2),
            "buyers": sorted({item["buyer"] for item in members}),
            "earliestDeliveryDate": min((item["deliveryDate"] for item in members if item["deliveryDate"]), default=""),
        }

    by_buyer = {}
    for item in result:
        if item["bucket"] not in urgent:
            continue
        stat = by_buyer.setdefault(item["buyer"], {
            "buyer": item["buyer"], "orderCount": 0, "purchaseQty": 0, "pendingQty": 0,
            "pendingAmount": 0.0, "buckets": {},
        })
        stat["orderCount"] += 1
        stat["purchaseQty"] += item["purchaseQty"]
        stat["pendingQty"] += item["pendingQty"]
        stat["pendingAmount"] = round(stat["pendingAmount"] + item["pendingAmount"], 2)
        stat["buckets"][item["bucket"]] = stat["buckets"].get(item["bucket"], 0) + 1

    urgent_orders = [item for item in result if item["bucket"] in urgent]
    return {
        "today": today.isoformat(),
        "generated": business_now().strftime("%Y-%m-%d %H:%M"),
        "orders": result,
        "buckets": buckets,
        "byBuyer": sorted(by_buyer.values(), key=lambda item: -item["pendingQty"]),
        "totals": {
            "orderCount": len(result),
            "urgentOrderCount": len(urgent_orders),
            "urgentPurchaseQty": sum(item["purchaseQty"] for item in urgent_orders),
            "urgentPendingQty": sum(item["pendingQty"] for item in urgent_orders),
            "urgentPendingAmount": round(sum(item["pendingAmount"] for item in urgent_orders), 2),
        },
    }


def _as_name_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    name = str(value).strip()
    return [name] if name else []


def filter_orders(reminders, *, buckets=None, buyer="", supplier="", limit=50):
    """按档位、采购员、供应商裁剪催办清单，供工具和推送共用。"""
    known = {**WAVES, **FOLLOWUP_WAVES}
    wanted = tuple(buckets) if buckets else URGENT_BUCKETS
    unknown = [key for key in wanted if key not in known]
    if unknown:
        raise ValueError("催办档位只能是：" + "、".join(sorted(known)))
    buyer_names = _as_name_list(buyer)
    supplier = str(supplier or "").strip()
    picked = [
        item for item in reminders["orders"]
        if item["bucket"] in wanted
        and (not buyer_names or any(name in item["buyer"] for name in buyer_names))
        and (not supplier or supplier in item["supplier"])
    ]
    limit = max(1, min(int(limit or 50), 500))
    return picked[:limit], len(picked)


def reminder_markdown(reminders, orders, *, title="采购交期催办"):
    """渲染钉钉 markdown 催办清单。数字只统计本条消息里的单，不拿全库合计。"""
    today = reminders["today"]
    heading = title if today in title else f"{title}（{today}）"
    purchased = sum(item.get("purchaseQty", item["pendingQty"]) for item in orders)
    qty = sum(item["pendingQty"] for item in orders)
    lines = [
        f"### {heading}",
        f"> 需催 **{len(orders)}** 单 · 采购 **{purchased:,}** 件 · 待入库 **{qty:,}** 件",
    ]
    grouped = {}
    for item in orders:
        grouped.setdefault(item["buyer"], []).append(item)
    for buyer, members in sorted(grouped.items(), key=lambda pair: -len(pair[1])):
        if len(grouped) > 1:
            lines.append(f"\n**{buyer}**（{len(members)} 单）")
        for item in members:
            remaining = item["remainingDays"]
            when = "无交期" if remaining is None else (
                f"逾期 {abs(remaining)} 天" if remaining < 0 else f"剩 {remaining} 天"
            )
            lines.append(
                f"- {item['waveLabel']} · {item['purchaseOrderNo']} · {item['supplier']} · "
                f"{item['deliveryDate'] or '—'}（{when}）· "
                f"采购 {item.get('purchaseQty', item['pendingQty'])} 件 · "
                f"待入库 {item['pendingQty']} 件"
            )
    return "\n".join(lines)
