# -*- coding: utf-8 -*-
"""ERP 写入证据：before / after / 回读核验。

不信页面返回「成功」。写入后必须回读订单明细，源 SKU 还应在或目标 SKU
没出现，则整单不记成功。回读失败标 unknown，调用方不得重试。
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path

from ..business_time import business_now


def oid_of(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("o_id") or item.get("oId") or "").strip()
    return ""


def plans_from_payload(payload: dict | None) -> list[dict]:
    raw = dict(payload or {})
    plans = raw.get("plans") or (raw.get("plan") or {}).get("plans") or []
    return [item for item in plans if isinstance(item, dict)]


def snapshot_from_order(order: dict | None) -> dict:
    """只留单号、状态、SKU/数量，不收录地址或买家。"""
    raw = dict(order or {})
    items = []
    for line in raw.get("items") or []:
        if not isinstance(line, dict):
            continue
        sku = str(line.get("sku_id") or line.get("skuId") or line.get("sku") or "").strip()
        qty = line.get("qty")
        if qty is None:
            qty = line.get("qty_count", line.get("orderQty", 0))
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = 0
        items.append({
            "sku": sku,
            "qty": qty,
            "oiId": str(line.get("oi_id") or line.get("oiId") or ""),
        })
    skus = sorted({item["sku"] for item in items if item["sku"]})
    return {
        "oId": str(raw.get("o_id") or raw.get("oId") or ""),
        "status": str(raw.get("status") or ""),
        "items": items,
        "skus": skus,
        "loadError": str(raw.get("load_error") or raw.get("loadError") or ""),
    }


def expected_from_plan(plan: dict | None) -> dict:
    raw = dict(plan or {})
    return {
        "oId": oid_of(raw),
        "sourceSku": str(raw.get("src_sku_id") or raw.get("source_sku") or "").strip(),
        "targetSku": str(raw.get("new_sku_id") or raw.get("target_sku") or "").strip(),
        "qty": raw.get("qty"),
    }


def already_exchanged(before: dict | None, expected: dict | None) -> bool:
    """写入前就已经是目标 SKU、源 SKU 不在，视为幂等成功，不再改单。"""
    exp = dict(expected or {})
    source = str(exp.get("sourceSku") or "")
    target = str(exp.get("targetSku") or "")
    if not source or not target:
        return False
    skus = set((before or {}).get("skus") or [])
    return target in skus and source not in skus


def matches_after(after: dict | None, expected: dict | None) -> bool:
    exp = dict(expected or {})
    source = str(exp.get("sourceSku") or "")
    target = str(exp.get("targetSku") or "")
    snap = dict(after or {})
    if snap.get("loadError") or not source or not target:
        return False
    skus = set(snap.get("skus") or [])
    return target in skus and source not in skus


def reconcile(before_map: dict, after_map: dict, expected_list: list[dict]) -> dict:
    confirmed = []
    already = []
    mismatches = []
    unknown = []
    for exp in expected_list:
        oid = str(exp.get("oId") or "")
        if not oid:
            continue
        before = before_map.get(oid) or {}
        after = after_map.get(oid) or {}
        if already_exchanged(before, exp):
            already.append(oid)
            continue
        if after.get("loadError"):
            unknown.append({"oId": oid, "error": after.get("loadError")})
            continue
        if matches_after(after, exp):
            confirmed.append(oid)
            continue
        mismatches.append({
            "oId": oid,
            "expected": exp,
            "afterSkus": list(after.get("skus") or []),
        })
    status = "ok"
    if unknown:
        status = "unknown"
    if mismatches:
        status = "reconciliation_failed"
    return {
        "status": status,
        "ok": status == "ok",
        "confirmed": confirmed,
        "alreadyDone": already,
        "mismatches": mismatches,
        "unknown": unknown,
    }


def apply_reconciliation(result: dict | None, recon: dict | None) -> dict:
    """页面「成功」但回读对不上的单，从 succeeded 挪到 failed。"""
    payload = dict(result or {})
    recon = dict(recon or {})
    keep = set(recon.get("confirmed") or []) | set(recon.get("alreadyDone") or [])
    mismatch_ids = {str(item.get("oId") or "") for item in recon.get("mismatches") or []}
    unknown_ids = {str(item.get("oId") or "") for item in recon.get("unknown") or []}
    succeeded = []
    seen = set()
    for item in payload.get("succeeded") or []:
        oid = oid_of(item)
        if not oid or oid in seen:
            continue
        if oid in keep:
            succeeded.append(item if isinstance(item, dict) else {"o_id": oid})
            seen.add(oid)
    for oid in recon.get("alreadyDone") or []:
        if oid and oid not in seen:
            succeeded.append({"o_id": oid, "alreadyDone": True})
            seen.add(oid)
    failed = []
    for item in payload.get("failed") or []:
        oid = oid_of(item)
        if oid:
            failed.append(item if isinstance(item, dict) else {"o_id": oid})
    for item in recon.get("mismatches") or []:
        oid = str(item.get("oId") or "")
        if oid and oid not in {oid_of(row) for row in failed}:
            failed.append({
                "o_id": oid,
                "error": "回读与预览不一致",
                "afterSkus": item.get("afterSkus") or [],
            })
    payload["succeeded"] = succeeded
    payload["failed"] = failed
    payload["reconciliation"] = recon
    payload["okCount"] = len(succeeded)
    payload["failedCount"] = len(failed)
    if mismatch_ids or unknown_ids:
        payload["error"] = (
            "回读失败，结果未知，未重试" if unknown_ids and not mismatch_ids
            else "回读与预览不一致，已停止记成功"
        )
    return payload


def write_evidence(root, *, command: str, command_id: str = "",
                   before: dict | None = None, after: dict | None = None,
                   result: dict | None = None, reconciliation: dict | None = None,
                   summary: dict | None = None) -> dict:
    """落到 files/data/erp-evidence/<id>/，凭证和买家信息不进文件。"""
    if root is None:
        return {"dir": "", "files": []}
    folder = Path(root) / (str(command_id or "").strip() or secrets.token_hex(12))
    folder.mkdir(parents=True, exist_ok=True)
    stamp = business_now().isoformat()
    files = {
        "request-summary.json": {
            "command": command,
            "commandId": folder.name,
            "at": stamp,
            **dict(summary or {}),
        },
        "before.json": before or {},
        "after.json": after or {},
        "result.json": result or {},
        "reconciliation.json": reconciliation or {},
    }
    written = []
    for name, payload in files.items():
        path = folder / name
        handle, temp_name = tempfile.mkstemp(prefix=".erp-ev-", dir=str(folder))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temp_name, path)
            written.append(name)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
    return {"dir": str(folder), "commandId": folder.name, "files": written, "at": stamp}
