# -*- coding: utf-8 -*-
"""把数据库或 CSV 的采购明细转换成两个前端页面的数据结构。"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .business_time import business_now


SIZE_NONE, SIZE_HT, SIZE_SHOE, SIZE_ALPHA = 0, 1, 2, 3
ALPHA_SIZE = re.compile(r"^(X{0,3}[SML]|[2-6]XL|均码)$")


def text(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def day(value):
    value = text(value)
    return value[:10] if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-" else ""


def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError, InvalidOperation):
        return 0.0


def integer(value):
    return int(number(value))


def shoe_size(value):
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def parse_size(spec):
    """返回（类型, 归一化尺码）。"""
    spec = text(spec)
    if not spec:
        return SIZE_NONE, ""
    match = re.match(r"^(\d{3})/\d{2,3}", spec)
    if match and 100 <= int(match.group(1)) <= 200:
        return SIZE_HT, match.group(1)
    match = re.match(r"^(\d{2,3}(?:\.\d)?)\s*码", spec)
    if match:
        return SIZE_SHOE, shoe_size(float(match.group(1)))
    match = re.match(r"^(\d{3}(?:\.\d)?)\s*[（(]", spec)
    if match:
        return SIZE_SHOE, shoe_size(float(match.group(1)) / 5 - 10)
    match = re.match(r"^(\d{2}(?:\.\d)?)$", spec)
    if match and 30 <= float(match.group(1)) <= 50:
        return SIZE_SHOE, shoe_size(float(match.group(1)))
    if ALPHA_SIZE.match(spec):
        return SIZE_ALPHA, spec
    return SIZE_NONE, ""


def make_encoder(rows, field, default="未知", *, transform=text, sorted_values=True):
    values = [transform(row.get(field)) or default for row in rows]
    categories = sorted(set(values)) if sorted_values else list(dict.fromkeys(values))
    lookup = {value: index for index, value in enumerate(categories)}
    return categories, [lookup[value] for value in values]


def meta_range(values):
    values = sorted(value for value in values if value)
    return (values[0], values[-1]) if values else ("", "")


def build_dashboard_payload(rows, source="MySQL · purchase_order_lines"):
    """构建采购看板的字典编码数据。"""
    rows = list(rows)
    colors, specs, sizes = [], [], []
    for row in rows:
        color, _, spec = text(row.get("颜色及规格")).partition(";")
        colors.append(color or "—")
        specs.append(spec.strip())
        sizes.append(parse_size(spec.strip()))

    dicts, encoded = {}, {}
    definitions = [
        ("buyers", "采购员", "未知", text),
        ("cats", "item_sku_other_3", "未分类", text),
        ("seasons", "item_sku_other_2", "未知", text),
        ("brands", "item_brand", "未知", text),
        ("channels", "item_sku_other_10", "未知", text),
        ("warehouses", "仓储方", "未指定", text),
        ("addrs", "收货地址", "未指定", text),
        ("pays", "付款方式", "未指定", text),
        ("styles", "款式编码", "未知", text),
        ("spus", "item_sku_other_1", "未知", text),
        ("suppliers", "item_supplier_id", "未知", text),
    ]
    for key, field, default, transform in definitions:
        dicts[key], encoded[key] = make_encoder(rows, field, default, transform=transform)
    dicts["colors"] = sorted(set(colors))
    color_lookup = {value: index for index, value in enumerate(dicts["colors"])}

    order_index, orders = {}, []
    for index, row in enumerate(rows):
        order_no = text(row.get("采购单号"))
        if order_no in order_index:
            continue
        order_index[order_no] = len(orders)
        orders.append([
            order_no, day(row.get("采购日期")), 1 if text(row.get("状态")) == "已确认" else 0,
            encoded["buyers"][index], encoded["suppliers"][index], encoded["warehouses"][index],
            encoded["addrs"][index], encoded["pays"][index], text(row.get("外部单号")),
            day(row.get("审核日期")), text(row.get("采购单建立时间")),
        ])

    lines = []
    for index, row in enumerate(rows):
        size_type, size_value = sizes[index]
        lines.append([
            order_index[text(row.get("采购单号"))], encoded["spus"][index],
            encoded["styles"][index], color_lookup[colors[index]], specs[index],
            encoded["cats"][index], encoded["seasons"][index], encoded["brands"][index],
            encoded["channels"][index], integer(row.get("数量")), integer(row.get("item_in_qty")),
            round(number(row.get("基本金额")), 2), round(number(row.get("基本售价")), 2),
            size_type, size_value, day(row.get("最早预计到货日期")), text(row.get("商品编码")),
        ])

    min_date, max_date = meta_range(order[1] for order in orders)
    return {
        "meta": {"source": source, "generated": business_now().strftime("%Y-%m-%d %H:%M"),
                 "rows": len(lines), "orders": len(orders), "minDate": min_date, "maxDate": max_date},
        "dict": dicts, "orders": orders, "lines": lines,
    }


def build_delivery_payload(rows, source="MySQL · purchase_order_lines"):
    """构建交期提醒台账的字典编码数据。"""
    rows = list(rows)
    definitions = [
        ("buyers", "采购员", "未知"), ("suppliers", "item_supplier_id", "未知"),
        ("warehouses", "仓储方", "未指定"), ("spus", "item_sku_other_1", "未命名"),
        ("cats", "item_sku_other_3", "未分类"),
    ]
    dicts, encoded = {}, {}
    for key, field, default in definitions:
        dicts[key], encoded[key] = make_encoder(rows, field, default, sorted_values=False)

    color_values = []
    specs = []
    for row in rows:
        color, _, spec = text(row.get("颜色及规格")).partition(";")
        color_values.append(color or "—")
        specs.append(spec.strip())
    dicts["colors"] = list(dict.fromkeys(color_values))
    color_lookup = {value: index for index, value in enumerate(dicts["colors"])}

    order_index, orders = {}, []
    for index, row in enumerate(rows):
        order_no = text(row.get("采购单号"))
        if order_no in order_index:
            continue
        order_index[order_no] = len(orders)
        orders.append([
            order_no, day(row.get("采购日期")), 1 if text(row.get("状态")) == "已确认" else 0,
            encoded["buyers"][index], encoded["suppliers"][index], encoded["warehouses"][index],
            text(row.get("外部单号")), day(row.get("审核日期")),
        ])

    lines = []
    for index, row in enumerate(rows):
        lines.append([
            order_index[text(row.get("采购单号"))], encoded["spus"][index], text(row.get("商品编码")),
            color_lookup[color_values[index]], specs[index], encoded["cats"][index],
            integer(row.get("数量")), integer(row.get("item_in_qty")), day(row.get("item_delivery_date")),
            day(row.get("最早预计到货日期")), round(number(row.get("基本金额")), 2),
        ])

    min_date, max_date = meta_range(order[1] for order in orders)
    etas = sorted({line[8] for line in lines if line[8]})
    covered = sum(1 for line in lines if line[8])
    return {
        "meta": {"source": source, "generated": business_now().strftime("%Y-%m-%d %H:%M"),
                 "rows": len(lines), "orders": len(orders), "minDate": min_date, "maxDate": max_date,
                 "etaMin": etas[0] if etas else "", "etaMax": etas[-1] if etas else "",
                 "etaCoverage": round(covered / len(lines), 4) if lines else 0,
                 "today": max_date},
        "dict": dicts, "orders": orders, "lines": lines,
    }
