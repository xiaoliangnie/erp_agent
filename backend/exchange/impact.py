# -*- coding: utf-8 -*-
"""换货 Impact Analysis：预览前的确定性影响评估。

只回答「执行后会影响什么」。LLM 可以解释结果，不能改 decision / 库存 / 金额。
第一期只覆盖 SKU 换货：重复任务、目标 SKU 是否存在、跨店。库存未进镜像，不做库存结论。
"""
from __future__ import annotations

from typing import Any


RULE_VERSION = "exchange-impact-1"
ACTIVE_JOB_STATUSES = frozenset({
    "pending", "planning", "awaiting_confirm", "confirmed", "executing",
})


def _item(code: str, message: str, **extra) -> dict:
    payload = {"code": code, "message": message}
    payload.update(extra)
    return payload


def _replacement(payload: dict[str, Any]) -> tuple[str, str]:
    rules = payload.get("rules") if isinstance(payload, dict) else {}
    replacements = (rules or {}).get("replacements") or []
    first = replacements[0] if replacements and isinstance(replacements[0], dict) else {}
    return str(first.get("from") or "").strip(), str(first.get("to") or "").strip()


def _oids(payload: dict[str, Any]) -> list[str]:
    targets = payload.get("targets") if isinstance(payload, dict) else {}
    raw = (targets or {}).get("o_ids") or []
    items = []
    for value in raw:
        oid = str(value or "").strip()
        if oid and oid not in items:
            items.append(oid)
    return items


def _product_keys(product: dict) -> set[str]:
    keys = set()
    for field in ("sku", "sku_id", "styleCode", "i_id", "iId"):
        value = str(product.get(field) or "").strip()
        if value:
            keys.add(value)
    return keys


def _job_oids(job: dict) -> set[str]:
    targets = job.get("targets") if isinstance(job.get("targets"), dict) else {}
    return {str(item).strip() for item in (targets.get("o_ids") or []) if str(item).strip()}


def _job_pair(job: dict) -> tuple[str, str]:
    rules = job.get("rules") if isinstance(job.get("rules"), dict) else {}
    replacements = rules.get("replacements") or []
    first = replacements[0] if replacements and isinstance(replacements[0], dict) else {}
    return str(first.get("from") or "").strip(), str(first.get("to") or "").strip()


def assess_exchange_impact(
    payload: dict[str, Any],
    *,
    products=None,
    orders=None,
    open_jobs=None,
) -> dict:
    """对一组换货参数做可重复计算的影响评估。

    `products` / `orders` / `open_jobs` 由调用方注入，便于离线单测。
    `products is None` 表示这次没查商品表，不因此判阻断。
    """
    source, target = _replacement(payload or {})
    oids = _oids(payload or {})
    blockers: list[dict] = []
    warnings: list[dict] = []
    infos: list[dict] = []

    infos.append(_item(
        "scope",
        f"{source or '未填源 SKU'} → {target or '未填目标 SKU'}，{len(oids)} 张订单",
        sourceSku=source, targetSku=target, orderCount=len(oids), oIds=oids[:50],
    ))
    infos.append(_item(
        "inventory_unavailable",
        "现势库存未进镜像，本次不评估可用库存，也不给出金额影响",
    ))

    if products is None:
        infos.append(_item("products_not_checked", "未提供商品主数据，跳过目标 SKU 存在性检查"))
    elif target:
        catalog = set()
        for product in products:
            if isinstance(product, dict):
                catalog.update(_product_keys(product))
        if target not in catalog:
            blockers.append(_item(
                "target_sku_missing",
                f"目标 SKU {target} 在商品主数据中不存在，不能进入预览",
                targetSku=target,
            ))

    requested = set(oids)
    for job in open_jobs or []:
        if not isinstance(job, dict):
            continue
        if str(job.get("status") or "") not in ACTIVE_JOB_STATUSES:
            continue
        overlap = sorted(requested & _job_oids(job))
        if not overlap:
            continue
        job_from, job_to = _job_pair(job)
        same_pair = bool(source and target and (job_from, job_to) == (source, target))
        shown = "、".join(overlap[:8])
        suffix = "…" if len(overlap) > 8 else ""
        item = _item(
            "duplicate_job",
            f"订单 {shown}{suffix} 已有进行中的换货任务 {job.get('id') or ''}",
            jobId=str(job.get("id") or ""),
            overlap=overlap[:20],
            sameReplacement=same_pair,
        )
        if same_pair:
            blockers.append(item)
        else:
            warnings.append({**item, "code": "overlapping_order"})

    shops = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        shop = str(order.get("shopName") or order.get("shop") or "").strip()
        if shop and shop not in shops:
            shops.append(shop)
    if len(shops) > 1:
        warnings.append(_item(
            "cross_shop",
            "所选订单来自多家店铺：" + "、".join(shops[:8]),
            shops=shops,
        ))
    elif shops:
        infos.append(_item("single_shop", f"订单均来自 {shops[0]}", shops=shops))
    elif orders is None:
        infos.append(_item("shops_not_checked", "未提供订单店铺，跳过跨店检查"))

    if blockers:
        decision = "block"
    elif warnings:
        decision = "allow_with_warning"
    else:
        decision = "allow"

    return {
        "ruleVersion": RULE_VERSION,
        "commandType": "exchange_items",
        "decision": decision,
        "blockers": blockers,
        "warnings": warnings,
        "infos": infos,
        "downstreamObjects": [
            {"type": "sales_order", "id": oid} for oid in oids[:50]
        ],
    }
