# -*- coding: utf-8 -*-
"""从商品资料圈定三品类线，并解析备注里的起订量。"""
from __future__ import annotations

import json
import re

from .formula import SKIP_WARNING_TAGS

CATEGORY_LINES = ("鞋类", "通勤裤", "服装-非通勤裤")
# 百货预测面板按商品标签圈款，不按品类线（文创百货 / 装备 / 易耗品都可能带这枚标签）
BAIHUO_LABEL = "自营百货"
# 商品备注里的起订量写法：起订2000 / 起订量300 / 合布起订500（可带尾注）
MOQ_PATTERN = re.compile(r"起订量?\s*[:：]?\s*(\d+)")


def _payload(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def parse_labels(value) -> list[str]:
    text = str(value or "").replace("，", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_moq(remark) -> int:
    """商品备注 → 起订量；没写返回 0。"""
    match = MOQ_PATTERN.search(str(remark or ""))
    return int(match.group(1)) if match else 0


def parse_product_attrs(row: dict) -> dict:
    payload = _payload(row.get("source_payload") or row.get("sourcePayload"))
    labels = parse_labels(row.get("labels") or payload.get("labels"))
    sale = row.get("sale_price") if row.get("sale_price") not in (None, "") else payload.get("sale_price")
    cost = row.get("cost_price") if row.get("cost_price") not in (None, "") else payload.get("cost_price")
    try:
        sale_price = float(sale or 0)
    except (TypeError, ValueError):
        sale_price = 0.0
    try:
        cost_price = float(cost or 0)
    except (TypeError, ValueError):
        cost_price = 0.0
    return {
        "skuId": str(row.get("sku_id") or row.get("skuId") or payload.get("sku_id") or "").strip(),
        "styleId": str(row.get("i_id") or row.get("iId") or payload.get("i_id") or "").strip(),
        "name": str(row.get("name") or payload.get("name") or "").strip(),
        "productName": str(payload.get("other_1") or row.get("name") or payload.get("name") or "").strip(),
        "categoryLine": str(payload.get("other_3") or "").strip(),
        "season": str(payload.get("other_2") or "").strip(),
        "productionMode": str(payload.get("other_10") or "").strip(),
        "category": str(row.get("category") or payload.get("category") or "").strip(),
        "salePrice": sale_price,
        "costPrice": cost_price,
        "labels": labels,
        "moq": parse_moq(payload.get("remark")),
        "enabled": row.get("enabled") not in (0, False, "0", "false", "False"),
    }


def product_in_scope(attrs: dict) -> bool:
    return bool(attrs.get("styleId")) and attrs.get("categoryLine") in CATEGORY_LINES


def product_in_baihuo_scope(attrs: dict) -> bool:
    """标签含「自营百货」的启用款；不要求生产模式=自营。"""
    return bool(attrs.get("skuId")) and BAIHUO_LABEL in (attrs.get("labels") or [])


def skip_stockout_warning(labels) -> bool:
    tags = labels if isinstance(labels, (list, tuple, set)) else parse_labels(labels)
    return any(any(flag in tag for flag in SKIP_WARNING_TAGS) for tag in tags)
