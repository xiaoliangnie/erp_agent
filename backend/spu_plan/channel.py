# -*- coding: utf-8 -*-
"""百货出库：店铺 → 线上 / 线下。

线下认 ERP「店铺设置」分组：消防业务部、渠道业务部、公安业务部、交警业务部、内部店铺。
与报表「销售渠道 → 按店铺设置中分组展示」同一份 group_name。
对不上这些组的（含查不到店铺的出库）算线上。
没有分组时才退回店名规则。
"""
from __future__ import annotations

OFFLINE_GROUPS = {
    "消防业务部": "消防",
    "渠道业务部": "渠道",
    "公安业务部": "公安",
    "交警业务部": "交警",
    "内部店铺": "内部",
}

# 先匹配更具体的部，避免「消防渠道负责人」落到渠道。
DEPARTMENT_RULES = (
    ("交警", ("交警-", "交警业务", "蜀黍家交警")),
    ("消防", ("消防-", "消防业务", "消防渠道", "蜀黍家消防")),
    ("渠道", ("KA渠道-", "渠道-", "渠道业务")),
    ("公安", ("公安-", "公安业务")),
)


def offline_department(shop_name: str = "", group_name: str = "") -> str:
    """命中四个业务部之一则返回部名，否则空字符串（线上）。"""
    group = str(group_name or "").strip()
    if group:
        return OFFLINE_GROUPS.get(group, "")
    name = str(shop_name or "").strip()
    if not name:
        return ""
    if name in ("{内部店铺}", "内部店铺"):
        return "内部"
    for department, tokens in DEPARTMENT_RULES:
        for token in tokens:
            if token.endswith("-"):
                if name.startswith(token):
                    return department
            elif token in name:
                return department
    return ""


def is_offline_shop(shop_name: str = "", group_name: str = "") -> bool:
    return bool(offline_department(shop_name, group_name))


def shop_id_from_raw_so(raw_so_id: str) -> str:
    """出库明细 raw_so_id 常见 `店铺ID:线上单号`。"""
    text = str(raw_so_id or "").strip()
    if ":" not in text:
        return ""
    prefix = text.split(":", 1)[0].strip()
    return prefix if prefix.isdigit() else ""


def so_id_from_raw_so(raw_so_id: str) -> str:
    text = str(raw_so_id or "").strip()
    if ":" not in text:
        return ""
    return text.split(":", 1)[1].strip()
