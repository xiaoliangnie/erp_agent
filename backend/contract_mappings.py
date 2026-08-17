# -*- coding: utf-8 -*-
"""合同生成映射表：ERP / Excel 的原始取值 → 合同字段。

只读本机 `files/config/contract_mappings.json`，按修改时间重读。表里没有的取值一律
中止生成，不猜、不兜底——与合同链路其余部分同一哲学。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


from .paths import CONFIG_DIR

MAPPING_PATH = CONFIG_DIR / "contract_mappings.json"
INVOICE_KEYS = ("no_invoice", "normal_invoice", "special_invoice")


def _invoice_types(payload: dict, path: Path) -> dict:
    raw = payload.get("invoice_types")
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} 的 invoice_types 必须是对象")
    table = {}
    for key, value in raw.items():
        label = str(key or "").strip()
        kind = str(value or "").strip()
        if not label or not kind:
            continue
        if kind not in INVOICE_KEYS:
            raise ValueError(f"{path.name} 的 invoice_types「{label}」指向未知票种 {kind}")
        table[label] = kind
    return table


def _payment_options(payload: dict, path: Path) -> list[dict]:
    raw = payload.get("payment_options")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path.name} 的 payment_options 必须是非空数组")
    options = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path.name} 的 payment_options 每一项必须是对象")
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or "").strip()
        text = str(item.get("text") or "").strip()
        if not key or not text:
            raise ValueError(f"{path.name} 的 payment_options 缺少 key 或 text")
        if key in seen:
            raise ValueError(f"{path.name} 的 payment_options 出现重复 key：{key}")
        seen.add(key)
        options.append({"key": key, "label": label or key, "text": text})
    return options


def _payment_defaults(payload: dict, path: Path, options: list[dict]) -> dict:
    raw = payload.get("erp_payment_defaults") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} 的 erp_payment_defaults 必须是对象")
    keys = {option["key"] for option in options}
    table = {}
    for code, value in raw.items():
        erp_code = str(code or "").strip()
        option_key = str(value or "").strip()
        if not erp_code or not option_key:
            continue
        if option_key not in keys:
            raise ValueError(
                f"{path.name} 的 erp_payment_defaults「{erp_code}」指向不存在的付款方式 {option_key}"
            )
        table[erp_code] = option_key
    return table


@lru_cache(maxsize=4)
def _cached_mappings(resolved: str, mtime: float) -> dict:
    path = Path(resolved)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} 必须是对象")
    options = _payment_options(payload, path)
    return {
        "invoice_types": _invoice_types(payload, path),
        "payment_options": options,
        "payment_texts": {option["key"]: option["text"] for option in options},
        "erp_payment_defaults": _payment_defaults(payload, path, options),
    }


def load_mappings(path=None) -> dict:
    resolved = Path(path) if path else MAPPING_PATH
    if not resolved.is_file():
        raise FileNotFoundError(f"找不到合同映射表：{resolved}")
    return _cached_mappings(str(resolved.resolve()), resolved.stat().st_mtime)


def clear_mapping_cache():
    _cached_mappings.cache_clear()
