# -*- coding: utf-8 -*-
"""品控钉钉指令的确定性解析。解析不出就留空，不猜。"""
from __future__ import annotations

import re

CLOSE_PATTERN = re.compile(r"^品控关闭\s+([a-f0-9]{6})\s*(.*)$")
CANCEL_PATTERN = re.compile(r"^撤销品控\s+([a-f0-9]{6})\s*$")
QUERY_PATTERN = re.compile(r"^品控查询(?:\s+(.+))?$")
RECORD_PATTERN = re.compile(r"^品控(?:登记)?\s+(.+)$")
SKU_PATTERN = re.compile(r"^[A-Z]{1,4}\d{5,}[A-Z0-9-]*$")
PO_PATTERN = re.compile(r"^\d{6,}$")


def parse_quality_command(text: str) -> dict | None:
    """识别品控四条指令；不是品控指令返回 None。"""
    text = str(text or "").strip()
    if not text:
        return None
    closed = CLOSE_PATTERN.match(text)
    if closed:
        return {"action": "resolve", "issueId": closed.group(1), "resolution": closed.group(2).strip()}
    cancelled = CANCEL_PATTERN.match(text)
    if cancelled:
        return {"action": "cancel", "issueId": cancelled.group(1)}
    queried = QUERY_PATTERN.match(text)
    if queried:
        return {"action": "query", "query": (queried.group(1) or "今天").strip()}
    recorded = RECORD_PATTERN.match(text)
    if recorded:
        return {"action": "record", "raw": recorded.group(1).strip()}
    return None


def parse_quality_fields(raw: str, *, suppliers: set[str], lookup_po=None) -> dict:
    """从「品控」后的正文抽出供应商 / 单号 / SKU，其余进 description。"""
    tokens = [part for part in str(raw or "").split() if part]
    used: set[int] = set()
    supplier = ""
    po_id = ""
    sku = ""
    for index, token in enumerate(tokens):
        if token in suppliers:
            supplier = token
            used.add(index)
            break
    for index, token in enumerate(tokens):
        if index in used:
            continue
        if PO_PATTERN.match(token) and lookup_po and lookup_po(token):
            po_id = token
            used.add(index)
            break
    for index, token in enumerate(tokens):
        if index in used:
            continue
        if SKU_PATTERN.match(token):
            sku = token
            used.add(index)
            break
    description = " ".join(token for index, token in enumerate(tokens) if index not in used).strip()
    return {"supplier": supplier, "po_id": po_id, "sku": sku, "description": description}
