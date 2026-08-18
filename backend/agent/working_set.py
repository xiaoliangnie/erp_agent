# -*- coding: utf-8 -*-
"""当前话题的业务对象快照：只抽编号，不抽数量/金额/交期。"""
from __future__ import annotations

import re
from typing import Any


PO_RE = re.compile(r"(?:采购单(?:号)?|单号|查(?:一下)?)\s*[：:#]?\s*(\d{5,8})")
OID_RE = re.compile(
    r"(?:内部订单|销售订单|订单号|内部单号|o_id)\s*[：:=#]?\s*(\d{6,10})",
    re.I,
)
SKU_RE = re.compile(r"\b([A-Z]{2}\d[\w-]{4,22})\b")
ACTION_RE = re.compile(r"(?:确认|动作|编号)\s*[：:#]?\s*([a-f0-9]{24})")

_ID_KEYS = {
    "po_id": "purchaseOrders",
    "poId": "purchaseOrders",
    "o_id": "salesOrders",
    "oId": "salesOrders",
    "o_ids": "salesOrders",
    "oIds": "salesOrders",
    "sku": "skus",
    "source_sku": "skus",
    "sourceSku": "skus",
    "target_sku": "skus",
    "targetSku": "skus",
}


def extract_working_set(messages: list[dict] | None = None,
                        pending: list[dict] | None = None) -> dict[str, list[str]]:
    snapshot = {
        "purchaseOrders": [],
        "salesOrders": [],
        "skus": [],
        "pendingActions": [],
    }
    for message in messages or []:
        if message.get("role") not in {"user", "assistant"}:
            continue
        _collect_from_text(str(message.get("content") or ""), snapshot)
    for action in pending or []:
        action_id = str(action.get("id") or "").strip()
        if action_id:
            _push(snapshot["pendingActions"], action_id)
        _collect_from_mapping(action.get("arguments") or {}, snapshot)
        preview = action.get("preview") or {}
        if isinstance(preview, dict):
            _collect_from_mapping(preview, snapshot)
        _collect_from_text(str(action.get("title") or ""), snapshot)
    return {key: values[:8] for key, values in snapshot.items()}


def format_working_set(snapshot: dict[str, list[str]] | None) -> str:
    snapshot = snapshot or {}
    lines = ["当前话题对象（只作指代，数量/金额/交期/状态必须重新查工具）："]
    labels = (
        ("purchaseOrders", "采购单"),
        ("salesOrders", "销售订单"),
        ("skus", "SKU"),
        ("pendingActions", "待确认"),
    )
    for key, label in labels:
        values = [str(item).strip() for item in snapshot.get(key) or [] if str(item).strip()]
        if values:
            lines.append(f"- {label}：{'、'.join(values)}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _collect_from_text(text: str, snapshot: dict[str, list[str]]) -> None:
    for match in PO_RE.finditer(text):
        _push(snapshot["purchaseOrders"], match.group(1))
    for match in OID_RE.finditer(text):
        _push(snapshot["salesOrders"], match.group(1))
    for match in SKU_RE.finditer(text):
        _push(snapshot["skus"], match.group(1))
    for match in ACTION_RE.finditer(text):
        _push(snapshot["pendingActions"], match.group(1))


def _collect_from_mapping(payload: Any, snapshot: dict[str, list[str]]) -> None:
    if not isinstance(payload, dict):
        return
    for key, value in payload.items():
        bucket = _ID_KEYS.get(str(key))
        if not bucket:
            if isinstance(value, dict):
                _collect_from_mapping(value, snapshot)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _collect_from_mapping(item, snapshot)
            continue
        if isinstance(value, list):
            for item in value:
                _push(snapshot[bucket], str(item))
        else:
            _push(snapshot[bucket], str(value))


def _push(bucket: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in bucket:
        bucket.append(text)
