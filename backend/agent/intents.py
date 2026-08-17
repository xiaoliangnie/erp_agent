# -*- coding: utf-8 -*-
"""对话意图识别。

当前只落地「抖音换鞋垫」这一条固定流程；其它句子返回 None，仍交给 LLM 选工具。
后续加意图只在这里加规则，不要把业务流程写进 prompt。
"""
from __future__ import annotations

from dataclasses import dataclass, field


INSOLE_QUERY = "insole_query"
INSOLE_PROCESS = "insole_process"

_EXCLUDE = ("品控", "开胶", "质量问题", "催办", "采购单")
_INSOLE_HINTS = ("换鞋垫", "鞋垫订单", "鞋垫")
_PROCESS = ("进行处理", "处理这些", "处理一下", "开始处理", "进行更换", "换掉", "换了")
_QUERY = ("查询", "查一下", "看看", "有哪些", "列出", "需要更换", "待处理")


@dataclass(frozen=True)
class Intent:
    name: str
    arguments: dict = field(default_factory=dict)


def classify_intent(text: str) -> Intent | None:
    """识别已落地的固定意图。未识别返回 None。"""
    text = str(text or "").strip()
    if not text:
        return None
    if any(token in text for token in _EXCLUDE):
        return None
    if not any(token in text for token in _INSOLE_HINTS):
        return None
    if "换成" in text and "XZ25401308-099" in text:
        return None
    shop = "抖音" if "抖音" in text or "douyin" in text.lower() else "抖音"
    arguments = {"shop": shop}
    if any(token in text for token in _PROCESS):
        return Intent(INSOLE_PROCESS, arguments)
    if any(token in text for token in _QUERY) or "订单" in text:
        return Intent(INSOLE_QUERY, arguments)
    return None
