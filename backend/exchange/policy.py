# -*- coding: utf-8 -*-
"""换货业务白名单。页面展示与服务端提交校验共用同一份配置。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .service import ExchangeError


RULE_PATH = Path(__file__).resolve().parents[2] / "config" / "exchange-rules.json"


@lru_cache(maxsize=4)
def _load_policy(path: str, mtime: float) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    mappings = []
    for raw in value.get("specialMappings") or []:
        targets = [str(item).strip() for item in raw.get("targetSkus") or [] if str(item).strip()]
        mappings.append({
            "name": str(raw.get("name") or "特殊映射"),
            "sourceSku": str(raw.get("sourceSku") or "").strip(),
            "sourceStyle": str(raw.get("sourceStyle") or "").strip(),
            "targetStyle": str(raw.get("targetStyle") or "").strip(),
            "targetSkus": targets,
        })
    return {"defaultPolicy": "same_style", "specialMappings": mappings}


def load_policy() -> dict:
    try:
        mtime = RULE_PATH.stat().st_mtime
    except OSError as exc:
        raise ExchangeError("无法读取换货规则配置") from exc
    return _load_policy(str(RULE_PATH.resolve()), mtime)


def public_policy() -> dict:
    return load_policy()


def enforce_replacement(source: str, target: str, source_style: str, target_style: str) -> dict:
    """返回规范化规则；任何调用端都不能绕过特殊白名单和默认同款限制。"""
    policy = load_policy()
    special = next((item for item in policy["specialMappings"] if item["sourceSku"] == source), None)
    if special:
        if target not in special["targetSkus"]:
            raise ExchangeError(
                f"商品 {source} 只能更换为已维护的 {len(special['targetSkus'])} 个目标 SKU"
            )
        if target_style and target_style != special["targetStyle"]:
            raise ExchangeError(f"目标 SKU {target} 的款式编码与特殊映射不一致")
        return {
            "exchangeType": "special_mapping",
            "sourceStyle": special["sourceStyle"],
            "targetStyle": special["targetStyle"],
            "policyName": special["name"],
        }
    if not source_style or not target_style:
        raise ExchangeError("普通商品换货必须选择带款式编码的源 SKU 和目标 SKU")
    if source_style != target_style:
        raise ExchangeError(
            f"普通商品只能在同一款式内换货：{source_style} ≠ {target_style}；"
            "仅已维护特殊映射的商品允许跨款"
        )
    return {
        "exchangeType": "same_style",
        "sourceStyle": source_style,
        "targetStyle": target_style,
        "policyName": "普通商品同款式换货",
    }
