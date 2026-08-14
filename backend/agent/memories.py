# -*- coding: utf-8 -*-
"""操作员记忆：只存偏好和指代，不存采购数字。"""
from __future__ import annotations

import re

from .store import AgentStore, now


MEMORY_LIMIT = 20
INJECT_LIMIT = 5
INJECT_CHARS = 500
CONTENT_LIMIT = 120


class OperatorMemories:
    def __init__(self, store: AgentStore, *, enabled: bool = False):
        self.store = store
        self.enabled = bool(enabled)

    def remember(self, operator: str, content: str, *, kind: str = "preference",
                 source: str = "explicit") -> dict:
        operator = str(operator or "").strip()
        content = str(content or "").strip()[:CONTENT_LIMIT]
        if not operator:
            raise ValueError("未绑定员工不能写入记忆")
        if not content:
            raise ValueError("记忆内容不能为空")
        if re.search(r"\d", content):
            raise ValueError("记忆不能包含数字，采购数字请每次走工具重查")
        stamp = now()
        with self.store.write() as conn:
            existing = conn.execute(
                """SELECT id FROM operator_memories
                   WHERE operator=? AND active=1 AND content=? LIMIT 1""",
                (operator, content),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE operator_memories SET updated_at=?, source=? WHERE id=?",
                    (stamp, source, existing["id"]),
                )
                memory_id = existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO operator_memories
                       (operator, kind, content, source, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (operator, kind, content, source, stamp, stamp),
                )
                memory_id = cursor.lastrowid
            rows = conn.execute(
                """SELECT id FROM operator_memories
                   WHERE operator=? AND active=1 ORDER BY updated_at DESC""",
                (operator,),
            ).fetchall()
            for row in rows[MEMORY_LIMIT:]:
                conn.execute(
                    "UPDATE operator_memories SET active=0, updated_at=? WHERE id=?",
                    (stamp, row["id"]),
                )
        return self.get(memory_id)

    def forget(self, operator: str, keyword: str) -> list[dict]:
        operator = str(operator or "").strip()
        keyword = str(keyword or "").strip()
        if not operator or not keyword:
            return []
        with self.store.write() as conn:
            rows = conn.execute(
                """SELECT * FROM operator_memories
                   WHERE operator=? AND active=1 AND content LIKE ?""",
                (operator, f"%{keyword}%"),
            ).fetchall()
            stamp = now()
            for row in rows:
                conn.execute(
                    "UPDATE operator_memories SET active=0, updated_at=? WHERE id=?",
                    (stamp, row["id"]),
                )
        return [self._row(row) for row in rows]

    def list_active(self, operator: str, *, limit: int = INJECT_LIMIT) -> list[dict]:
        operator = str(operator or "").strip()
        if not operator:
            return []
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT * FROM operator_memories
                   WHERE operator=? AND active=1
                   ORDER BY updated_at DESC LIMIT ?""",
                (operator, max(1, int(limit))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def prompt_block(self, operator: str) -> str:
        if not self.enabled:
            return ""
        items = self.list_active(operator)
        if not items:
            return ""
        lines = ["关于当前员工的已知信息（可说「忘记 xx」修改）："]
        used = 0
        for item in items:
            line = f"- {item['content']}"
            if used + len(line) > INJECT_CHARS:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def get(self, memory_id: int) -> dict:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM operator_memories WHERE id=?", (memory_id,)).fetchone()
        return self._row(row) if row else {}

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "operator": row["operator"],
            "kind": row["kind"],
            "content": row["content"],
            "source": row["source"],
            "active": bool(row["active"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }


REMEMBER_PATTERN = re.compile(r"^记住\s+(.+)$")
FORGET_PATTERN = re.compile(r"^忘记\s+(.+)$")
