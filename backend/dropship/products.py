# -*- coding: utf-8 -*-
"""代发 Excel 用的 SKU 资料：供应商、成本价、供应商编码。

订单列表页没有这些列；商品主数据是补全来源，不影响代发池本身的取数。
连不上库时返回空 dict，不中止揭开收货后的导出。
"""
from __future__ import annotations

from pathlib import Path

from ..database import REALTIME_PRODUCT_TABLE, REALTIME_SUPPLIER_TABLE, _is_missing_table, clean_master_text, connect
from ..paths import ROOT


def default_env_path(root=None) -> str:
    base = Path(root) if root is not None else ROOT
    return str(base / "hanli.env")


def fetch_sku_facts(sku_ids, style_ids=(), env_path=None) -> dict:
    """sku_id / 款式编码 → 供应商与成本价。"""
    skus = [str(sku).strip() for sku in dict.fromkeys(sku_ids or []) if str(sku).strip()]
    styles = [str(sid).strip() for sid in dict.fromkeys(style_ids or []) if str(sid).strip()]
    if not skus and not styles:
        return {}
    path = env_path or default_env_path()
    if not Path(path).is_file():
        return {}
    clauses = []
    params = []
    if skus:
        clauses.append(f"p.sku_id IN ({','.join(['%s'] * len(skus))})")
        params.extend(skus)
    if styles:
        clauses.append(f"p.i_id IN ({','.join(['%s'] * len(styles))})")
        params.extend(styles)
    sql = (
        f"SELECT p.sku_id, p.i_id, p.name, p.supplier_name, p.supplier_sku_id, "
        f"p.supplier_i_id, p.cost_price, p.supplier_id, s.name AS supplier_table_name "
        f"FROM `{REALTIME_PRODUCT_TABLE}` p "
        f"LEFT JOIN `{REALTIME_SUPPLIER_TABLE}` s ON s.supplier_id = p.supplier_id "
        f"WHERE {' OR '.join(clauses)}"
    )
    try:
        with connect(path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall() or []
    except Exception as exc:
        if _is_missing_table(exc):
            return {}
        return {}
    facts = {}
    for row in rows:
        supplier = clean_master_text(row.get("supplier_name")) or clean_master_text(
            row.get("supplier_table_name")
        )
        record = {
            "name": clean_master_text(row.get("name")),
            "supplier": supplier,
            "supplier_sku": clean_master_text(row.get("supplier_sku_id")),
            "supplier_style": clean_master_text(row.get("supplier_i_id")),
            "cost": row.get("cost_price"),
        }
        sku = str(row.get("sku_id") or "").strip()
        style = str(row.get("i_id") or "").strip()
        if sku:
            facts[sku] = record
        if style and style not in facts:
            facts[style] = record
    return facts


def _merge_fact(primary: dict, secondary: dict) -> dict:
    merged = dict(secondary or {})
    for key, value in (primary or {}).items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def apply_sku_facts(orders: list[dict], facts: dict) -> list[dict]:
    """只填列表里空着的供应商 / 成本 / 编码 / 品名，不覆盖订单页已有值。"""
    for order in orders or []:
        for item in order.get("items") or []:
            sku_fact = facts.get(str(item.get("sku") or "").strip()) or {}
            style_fact = facts.get(str(item.get("style") or "").strip()) or {}
            fact = _merge_fact(sku_fact, style_fact)
            if not fact:
                continue
            if not str(item.get("supplier") or "").strip():
                item["supplier"] = fact.get("supplier") or ""
            if item.get("cost") in (None, ""):
                item["cost"] = fact.get("cost")
            if not str(item.get("supplier_sku") or "").strip():
                item["supplier_sku"] = fact.get("supplier_sku") or ""
            if not str(item.get("supplier_style") or "").strip():
                item["supplier_style"] = fact.get("supplier_style") or ""
            if not str(item.get("name") or "").strip():
                item["name"] = fact.get("name") or ""
    return orders


def missing_sku_ids(orders: list[dict]) -> list[str]:
    """还缺供应商 / 成本 / 供应商款号的 SKU，交给页面 GetSku。"""
    found = []
    for order in orders or []:
        for item in order.get("items") or []:
            sku = str(item.get("sku") or "").strip()
            if not sku or sku in found:
                continue
            if (
                not str(item.get("supplier") or "").strip()
                or item.get("cost") in (None, "")
                or not str(item.get("supplier_style") or "").strip()
            ):
                found.append(sku)
    return found
