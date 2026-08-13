# -*- coding: utf-8 -*-
"""会话与消息持久化：渠道 + 会话键唯一，网页和钉钉共用同一张表。"""
from __future__ import annotations

import secrets
from typing import Any

from .store import AgentStore, dumps, loads, now


class SessionStore:
    def __init__(self, store: AgentStore, *, history_limit: int = 20):
        self.store = store
        self.history_limit = history_limit

    def ensure(self, channel: str, session_key: str, operator: str = "") -> dict:
        """按渠道和会话键取会话，不存在就建；顺带刷新最近操作人。"""
        channel = str(channel or "web").strip()[:40] or "web"
        session_key = str(session_key or "").strip()[:200]
        if not session_key:
            raise ValueError("会话键不能为空")
        operator = str(operator or "").strip()[:120]
        stamp = now()
        with self.store.write(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE channel = ? AND session_key = ?",
                (channel, session_key),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE agent_sessions SET updated_at = ?, operator = ? WHERE id = ?",
                    (stamp, operator or row["operator"], row["id"]),
                )
                return {**dict(row), "operator": operator or row["operator"]}
            session_id = secrets.token_hex(12)
            conn.execute(
                """INSERT INTO agent_sessions (id, channel, session_key, operator, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, channel, session_key, operator, stamp, stamp),
            )
            return {
                "id": session_id, "channel": channel, "session_key": session_key,
                "operator": operator, "title": "", "created_at": stamp, "updated_at": stamp,
            }

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = "",
        *,
        run_id: str | None = None,
        name: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list | None = None,
    ) -> None:
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO agent_messages
                   (session_id, run_id, role, content, name, tool_call_id, tool_calls_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, run_id, role, str(content or ""), name, tool_call_id,
                 dumps(tool_calls) if tool_calls else None, now()),
            )

    def history(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """取最近若干条消息并还原成 OpenAI 消息序列。

        截断可能把 `tool` 结果和它对应的 `assistant.tool_calls` 拆开，这种消息序列
        会被服务端拒绝，所以统一从最近一条 `user` 消息处起头。
        """
        limit = limit or self.history_limit
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, max(1, int(limit))),
            ).fetchall()
        rows = list(reversed(rows))
        while rows and rows[0]["role"] != "user":
            rows.pop(0)
        messages = []
        for row in rows:
            message: dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if row["name"]:
                message["name"] = row["name"]
            if row["tool_call_id"]:
                message["tool_call_id"] = row["tool_call_id"]
            tool_calls = loads(row["tool_calls_json"])
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
        return messages

    def transcript(self, session_id: str, limit: int = 50) -> list[dict]:
        """给页面展示的对话记录，只保留员工可见的两种角色。"""
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT role, content, created_at FROM agent_messages
                   WHERE session_id = ? AND role IN ('user', 'assistant') AND content <> ''
                   ORDER BY id DESC LIMIT ?""",
                (session_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"], "createdAt": row["created_at"]}
                for row in reversed(rows)]

    def reset(self, session_id: str) -> dict:
        with self.store.write() as conn:
            conn.execute("DELETE FROM agent_messages WHERE session_id = ?", (session_id,))
        return {"ok": True, "sessionId": session_id}
