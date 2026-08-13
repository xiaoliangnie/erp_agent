# -*- coding: utf-8 -*-
"""交期四波催办口径：把采购明细行折算成按采购单的催办清单。

口径与前端交期台账页（`frontend/src/pages/ledger/`）完全一致，见 README「交期口径」章节：
交期取该行 `item_delivery_date`，为空退到 `最早预计到货日期`；只算还有待入库
数量的明细行；一张采购单的交期是所有待入库行里最早的那个，波次取最急的一档。

本模块不碰数据库也不碰网络，`rows` 就是 `fetch_realtime_purchase_rows` 的产物，
所以 Agent 工具和钉钉定时推送共用同一份计算，不会各算一套。
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


def _as_date(value):
    value = day(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def build_reminders(rows, today=None):
    """把明细行折算成按采购单的催办清单。

    返回 `{today, orders, buckets, byBuyer, totals}`；`orders` 已按紧急程度和
    交期排序，可直接渲染成催办清单或钉钉消息。
    """
    today = _as_date(today) or business_today()
    orders = {}
    for row in rows:
        pending = integer(row.get("数量")) - integer(row.get("item_in_qty"))
        if pending <= 0:
            continue
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
                "pendingQty": 0,
                "pendingAmount": 0.0,
                "lineCount": 0,
                "skus": [],
            }
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
        eta = _as_date(entry["deliveryDate"])
        remaining = (eta - today).days if eta else None
        bucket = classify(remaining)
        entry.update(
            remainingDays=remaining,
            bucket=bucket,
            waveLabel=WAVES[bucket]["label"],
            wave=WAVES[bucket]["wave"],
            action=WAVES[bucket]["action"],
            pendingAmount=round(entry["pendingAmount"], 2),
            skus=entry["skus"][:8],
        )
        result.append(entry)
    result.sort(key=lambda item: (
        BUCKET_ORDER.index(item["bucket"]),
        item["deliveryDate"] or "9999-99-99",
        item["purchaseOrderNo"],
    ))

    buckets = {}
    for key in BUCKET_ORDER:
        members = [item for item in result if item["bucket"] == key]
        buckets[key] = {
            "label": WAVES[key]["label"],
            "wave": WAVES[key]["wave"],
            "action": WAVES[key]["action"],
            "orderCount": len(members),
            "pendingQty": sum(item["pendingQty"] for item in members),
            "pendingAmount": round(sum(item["pendingAmount"] for item in members), 2),
            "buyers": sorted({item["buyer"] for item in members}),
            "earliestDeliveryDate": min((item["deliveryDate"] for item in members if item["deliveryDate"]), default=""),
        }

    by_buyer = {}
    for item in result:
        if item["bucket"] not in URGENT_BUCKETS:
            continue
        stat = by_buyer.setdefault(item["buyer"], {
            "buyer": item["buyer"], "orderCount": 0, "pendingQty": 0,
            "pendingAmount": 0.0, "buckets": {},
        })
        stat["orderCount"] += 1
        stat["pendingQty"] += item["pendingQty"]
        stat["pendingAmount"] = round(stat["pendingAmount"] + item["pendingAmount"], 2)
        stat["buckets"][item["bucket"]] = stat["buckets"].get(item["bucket"], 0) + 1

    urgent = [item for item in result if item["bucket"] in URGENT_BUCKETS]
    return {
        "today": today.isoformat(),
        "generated": business_now().strftime("%Y-%m-%d %H:%M"),
        "orders": result,
        "buckets": buckets,
        "byBuyer": sorted(by_buyer.values(), key=lambda item: -item["pendingQty"]),
        "totals": {
            "orderCount": len(result),
            "urgentOrderCount": len(urgent),
            "urgentPendingQty": sum(item["pendingQty"] for item in urgent),
            "urgentPendingAmount": round(sum(item["pendingAmount"] for item in urgent), 2),
        },
    }


def filter_orders(reminders, *, buckets=None, buyer="", supplier="", limit=50):
    """按档位、采购员、供应商裁剪催办清单，供工具和推送共用。"""
    wanted = tuple(buckets) if buckets else URGENT_BUCKETS
    unknown = [key for key in wanted if key not in WAVES]
    if unknown:
        raise ValueError("催办档位只能是：" + "、".join(BUCKET_ORDER))
    buyer = str(buyer or "").strip()
    supplier = str(supplier or "").strip()
    picked = [
        item for item in reminders["orders"]
        if item["bucket"] in wanted
        and (not buyer or buyer in item["buyer"])
        and (not supplier or supplier in item["supplier"])
    ]
    limit = max(1, min(int(limit or 50), 500))
    return picked[:limit], len(picked)


def reminder_markdown(reminders, orders, *, title="采购交期催办"):
    """渲染钉钉 markdown 催办清单；按采购员分组，方便群内 @ 到人。"""
    lines = [f"### {title}（{reminders['today']}）"]
    totals = reminders["totals"]
    lines.append(
        f"> 需催 **{totals['urgentOrderCount']}** 单 · "
        f"**{totals['urgentPendingQty']:,}** 件待入库"
    )
    grouped = {}
    for item in orders:
        grouped.setdefault(item["buyer"], []).append(item)
    for buyer, members in sorted(grouped.items(), key=lambda pair: -len(pair[1])):
        lines.append(f"\n**{buyer}**（{len(members)} 单）")
        for item in members:
            remaining = item["remainingDays"]
            when = "无交期" if remaining is None else (
                f"逾期 {abs(remaining)} 天" if remaining < 0 else f"剩 {remaining} 天"
            )
            lines.append(
                f"- {item['waveLabel']} · {item['purchaseOrderNo']} · {item['supplier']} · "
                f"{item['deliveryDate'] or '—'}（{when}）· 待入库 {item['pendingQty']} 件"
            )
    return "\n".join(lines)
