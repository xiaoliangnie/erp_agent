# -*- coding: utf-8 -*-
"""MemoryStore 接口预留。本期没有实际消费者，不要向下展开。"""
from __future__ import annotations

from typing import Protocol


class MemoryStore(Protocol):
    """跨会话稳定偏好。禁止 Agent 自行写入；禁止存 ERP 数字。"""

    def list(self, user_id: str) -> list[dict]:
        ...

    def remember(self, user_id: str, content: str, *, source: str) -> dict:
        ...

    def forget(self, user_id: str, keyword: str) -> dict:
        ...
