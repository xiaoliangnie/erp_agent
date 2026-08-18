# -*- coding: utf-8 -*-
"""RetrievalRouter 接口预留。

实时 ERP 数字继续走 Exact Query；文档/制度/历史附件才允许未来走 RAG。
本期不部署向量库。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrievalDecision:
    kind: str
    query: str = ""
    reason: str = ""
    entities: dict = field(default_factory=dict)


class RetrievalRouter:
    """第一期只区分 structured / document / deny。"""

    def route(self, text: str, *, intent_route: str = "") -> RetrievalDecision:
        text = str(text or "").strip()
        if intent_route in {"exact_query", "workflow"}:
            return RetrievalDecision(kind="structured", query=text, reason="intent_exact")
        return RetrievalDecision(kind="none", query=text, reason="not_implemented")
