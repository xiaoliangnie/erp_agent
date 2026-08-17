# -*- coding: utf-8 -*-
"""合同选择的历史留痕。只写本机 JSON，不进镜像库。

现在只记「这家供应商上次用的付款方式」，供下次生成合同时预选并标注来源。
文件跟供应商主数据放在一起（`files/config/payment_history.json`），可以直接看、直接改。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .business_time import business_now


from .paths import CONFIG_DIR

PAYMENT_HISTORY_PATH = CONFIG_DIR / "payment_history.json"
HISTORY_LIMIT = 500
_WRITE_LOCK = threading.Lock()


def _resolve(path=None) -> Path:
    return Path(path) if path else PAYMENT_HISTORY_PATH


def load_payment_history(path=None) -> dict:
    """供应商简称 → 上次选择。文件损坏或不存在都当作没有历史，不阻断合同。"""
    resolved = _resolve(path)
    if not resolved.is_file():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    history = {}
    for key, raw in payload.items():
        supplier = str(key or "").strip()
        if not supplier or not isinstance(raw, dict):
            continue
        history[supplier] = {
            "option": str(raw.get("option") or ""),
            "text": str(raw.get("text") or ""),
            "poId": str(raw.get("poId") or ""),
            "at": str(raw.get("at") or ""),
        }
    return history


def last_payment_choice(supplier: str, path=None, history=None) -> dict:
    supplier = str(supplier or "").strip()
    if not supplier:
        return {}
    table = history if history is not None else load_payment_history(path)
    return table.get(supplier) or {}


def record_payment_choice(supplier: str, *, option: str, text: str, po_id: str, path=None) -> dict:
    """记下这家供应商本次用的付款方式。写失败只当没记住，不影响已生成的合同。"""
    supplier = str(supplier or "").strip()
    text = str(text or "").strip()
    if not supplier or not text:
        return {}
    entry = {
        "option": str(option or ""),
        "text": text,
        "poId": str(po_id or ""),
        "at": business_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    resolved = _resolve(path)
    with _WRITE_LOCK:
        history = load_payment_history(resolved)
        history[supplier] = entry
        if len(history) > HISTORY_LIMIT:
            ordered = sorted(history.items(), key=lambda item: item[1].get("at") or "", reverse=True)
            history = dict(ordered[:HISTORY_LIMIT])
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(resolved, history)
        except OSError:
            return {}
    return entry


def _atomic_write(path: Path, payload: dict):
    """先写临时文件再替换，避免并发生成合同时把历史写成半截 JSON。"""
    handle, temp_name = tempfile.mkstemp(prefix=".payment-history-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
