# -*- coding: utf-8 -*-
"""操作员记忆：只存偏好和指代，不存采购数字。"""
from __future__ import annotations

import re

from .session_commands import FORGET_PATTERN, REMEMBER_PATTERN
from .store import AgentStore, now


MEMORY_LIMIT = 20
INJECT_LIMIT = 5
INJECT_CHARS = 500
CONTENT_LIMIT = 120

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HIDDEN_UNICODE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
INJECTION_PATTERN = re.compile(
    r"(ignore\s+(all\s+)?(previous|above|prior)|you\s+are\s+now|"
    r"system\s+prompt|<\/?system>|\[INST\]|"
    r"覆盖(系统|规则|提示)|忘记你是|忽略(以上|之前|系统)|你现在是)",
    re.I,
)


def sanitize_memory_content(content: str) -> str:
    """去掉控制字符和隐藏 Unicode，并拒绝注入句、数字。"""
    text = HIDDEN_UNICODE.sub("", CONTROL_CHARS.sub("", str(content or "")))
    text = re.sub(r"\s+", " ", text).strip()[:CONTENT_LIMIT]
    if not text:
        raise ValueError("记忆内容不能为空")
    if INJECTION_PATTERN.search(text):
        raise ValueError("记忆内容不能改写系统规则")
    if re.search(r"\d", text):
        raise ValueError("记忆不能包含数字，采购数字请每次走工具重查")
    return text


class OperatorMemories:
    def __init__(self, store: AgentStore, *, enabled: bool = False):
        self.store = store
        self.enabled = bool(enabled)

    def remember(self, operator: str, content: str, *, kind: str = "preference",
                 source: str = "explicit", user_id: str = "") -> dict:
        operator = str(operator or "").strip()
        user_id = str(user_id or "").strip()
        content = sanitize_memory_content(content)
        if not operator:
            raise ValueError("未绑定员工不能写入记忆")
        stamp = now()
        with self.store.write() as conn:
            if user_id:
                existing = conn.execute(
                    """SELECT id FROM operator_memories
                       WHERE user_id=? AND active=1 AND content=? LIMIT 1""",
                    (user_id, content),
                ).fetchone()
            else:
                existing = conn.execute(
                    """SELECT id FROM operator_memories
                       WHERE operator=? AND active=1 AND content=? LIMIT 1""",
                    (operator, content),
                ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE operator_memories
                       SET updated_at=?, source=?, user_id=CASE WHEN ?!='' THEN ? ELSE user_id END
                       WHERE id=?""",
                    (stamp, source, user_id, user_id, existing["id"]),
                )
                memory_id = existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO operator_memories
                       (operator, user_id, kind, content, source, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                    (operator, user_id, kind, content, source, stamp, stamp),
                )
                memory_id = cursor.lastrowid
            if user_id:
                rows = conn.execute(
                    """SELECT id FROM operator_memories
                       WHERE active=1 AND user_id=?
                       ORDER BY updated_at DESC""",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id FROM operator_memories
                       WHERE active=1 AND operator=?
                       ORDER BY updated_at DESC""",
                    (operator,),
                ).fetchall()
            for row in rows[MEMORY_LIMIT:]:
                conn.execute(
                    "UPDATE operator_memories SET active=0, updated_at=? WHERE id=?",
                    (stamp, row["id"]),
                )
        return self.get(memory_id)

    def forget(self, operator: str, keyword: str, *, user_id: str = "") -> list[dict]:
        operator = str(operator or "").strip()
        user_id = str(user_id or "").strip()
        keyword = str(keyword or "").strip()
        if (not operator and not user_id) or not keyword:
            return []
        with self.store.write() as conn:
            if user_id:
                rows = conn.execute(
                    """SELECT * FROM operator_memories
                       WHERE active=1 AND (user_id=? OR (user_id='' AND operator=?))
                         AND content LIKE ?""",
                    (user_id, operator, f"%{keyword}%"),
                ).fetchall()
            else:
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

    def list_active(self, operator: str, *, user_id: str = "",
                    limit: int = INJECT_LIMIT) -> list[dict]:
        operator = str(operator or "").strip()
        user_id = str(user_id or "").strip()
        if not operator and not user_id:
            return []
        with self.store.read() as conn:
            if user_id:
                rows = conn.execute(
                    """SELECT * FROM operator_memories
                       WHERE active=1 AND (user_id=? OR (user_id='' AND operator=?))
                       ORDER BY updated_at DESC LIMIT ?""",
                    (user_id, operator, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM operator_memories
                       WHERE operator=? AND active=1
                       ORDER BY updated_at DESC LIMIT ?""",
                    (operator, max(1, int(limit))),
                ).fetchall()
        return [self._row(row) for row in rows]

    def prompt_block(self, operator: str, *, user_id: str = "") -> str:
        if not self.enabled:
            return ""
        items = self.list_active(operator, user_id=user_id)
        if not items:
            return ""
        lines = ["关于当前员工的已知信息（可说「忘记 xx」修改）："]
        used = 0
        for item in items:
            try:
                content = sanitize_memory_content(item["content"])
            except ValueError:
                continue
            line = f"- {content}"
            if used + len(line) > INJECT_CHARS:
                break
            lines.append(line)
            used += len(line)
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def get(self, memory_id: int) -> dict:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM operator_memories WHERE id=?", (memory_id,)).fetchone()
        return self._row(row) if row else {}

    @staticmethod
    def _row(row) -> dict:
        keys = row.keys()
        return {
            "id": row["id"],
            "operator": row["operator"],
            "userId": row["user_id"] if "user_id" in keys else "",
            "kind": row["kind"],
            "content": row["content"],
            "source": row["source"],
            "active": bool(row["active"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }


__all__ = [
    "FORGET_PATTERN",
    "INJECT_CHARS",
    "INJECT_LIMIT",
    "MEMORY_LIMIT",
    "OperatorMemories",
    "REMEMBER_PATTERN",
    "sanitize_memory_content",
]
