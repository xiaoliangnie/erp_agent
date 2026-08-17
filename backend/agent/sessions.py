# -*- coding: utf-8 -*-
"""会话与消息持久化：渠道 + 会话键唯一，网页和钉钉共用同一张表。"""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from .store import AgentStore, dumps, loads, now


class SessionStore:
    def __init__(self, store: AgentStore, *, history_limit: int = 20,
                 idle_minutes: int = 120, char_budget: int = 60000,
                 tool_result_limit: int = 8000):
        self.store = store
        self.history_limit = history_limit
        self.idle_minutes = max(0, int(idle_minutes))
        self.char_budget = max(4000, int(char_budget))
        self.tool_result_limit = max(500, int(tool_result_limit))
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()

    def lock_for(self, session_id: str) -> threading.Lock:
        """同一会话的对话 / 确认串行，避免并发消息交叉写入上下文。"""
        session_id = str(session_id or "").strip()
        if not session_id:
            return threading.Lock()
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def ensure(self, channel: str, session_key: str, operator: str = "") -> dict:
        """按渠道和会话键取会话；空闲超时则 epoch+1，不删消息。"""
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
                epoch = int(row["epoch"] or 0)
                if self.idle_minutes and _stale(row["updated_at"], self.idle_minutes):
                    epoch += 1
                conn.execute(
                    """UPDATE agent_sessions
                       SET updated_at = ?, operator = ?, epoch = ? WHERE id = ?""",
                    (stamp, operator or row["operator"], epoch, row["id"]),
                )
                return {
                    **dict(row),
                    "operator": operator or row["operator"],
                    "epoch": epoch,
                    "updated_at": stamp,
                }
            session_id = secrets.token_hex(12)
            conn.execute(
                """INSERT INTO agent_sessions
                   (id, channel, session_key, operator, created_at, updated_at, epoch)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (session_id, channel, session_key, operator, stamp, stamp),
            )
            return {
                "id": session_id, "channel": channel, "session_key": session_key,
                "operator": operator, "title": "", "created_at": stamp, "updated_at": stamp,
                "epoch": 0,
            }

    def rotate(self, session_id: str) -> dict:
        """显式开新话题：epoch+1，消息保留供 transcript。"""
        stamp = now()
        with self.store.write() as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                raise ValueError("会话不存在")
            epoch = int(row["epoch"] or 0) + 1
            conn.execute(
                "UPDATE agent_sessions SET epoch = ?, updated_at = ? WHERE id = ?",
                (epoch, stamp, session_id),
            )
        return {"ok": True, "sessionId": session_id, "epoch": epoch}

    def reset(self, session_id: str) -> dict:
        return self.rotate(session_id)

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
            session = conn.execute(
                "SELECT epoch FROM agent_sessions WHERE id = ?", (session_id,),
            ).fetchone()
            epoch = int(session["epoch"] or 0) if session else 0
            conn.execute(
                """INSERT INTO agent_messages
                   (session_id, run_id, role, content, name, tool_call_id, tool_calls_json,
                    created_at, epoch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, run_id, role, str(content or ""), name, tool_call_id,
                 dumps(tool_calls) if tool_calls else None, now(), epoch),
            )

    def current_epoch(self, session_id: str) -> int:
        with self.store.read() as conn:
            row = conn.execute("SELECT epoch FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        return int(row["epoch"] or 0) if row else 0

    def history(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """取当前 epoch 最近若干条，并从最近一条 user 起头；丢掉悬空 tool_calls。"""
        limit = limit or self.history_limit
        epoch = self.current_epoch(session_id)
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT * FROM agent_messages
                   WHERE session_id = ? AND epoch = ?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, epoch, max(1, int(limit))),
            ).fetchall()
        rows = list(reversed(rows))
        while rows and rows[0]["role"] != "user":
            rows.pop(0)
        messages = [_message_from_row(row) for row in rows]
        return drop_dangling_tool_calls(messages)

    def context_messages(self, session_id: str, *, system: str, memory: str = "",
                         summary: str = "") -> list[dict[str, Any]]:
        """按字符预算从新到旧组装上下文。"""
        epoch = self.current_epoch(session_id)
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT * FROM agent_messages
                   WHERE session_id = ? AND epoch = ?
                   ORDER BY id DESC""",
                (session_id, epoch),
            ).fetchall()
        raw = [_message_from_row(row) for row in reversed(rows)]
        raw = drop_dangling_tool_calls(raw)
        last_user = 0
        for index, message in enumerate(raw):
            if message.get("role") == "user":
                last_user = index
        current_turn_tools = set()
        for message in raw[last_user:]:
            if message.get("role") == "tool" and message.get("tool_call_id"):
                current_turn_tools.add(message["tool_call_id"])
        prefix = [{"role": "system", "content": system}]
        if memory:
            prefix.append({"role": "system", "content": memory})
        if summary:
            prefix.append({"role": "system", "content": "此前会话摘要（不含金额/数量/日期）：\n" + summary})
        used = sum(len(str(item.get("content") or "")) for item in prefix)
        selected: list[dict[str, Any]] = []
        for message in reversed(raw):
            packed = message
            if message.get("role") == "tool" and message.get("tool_call_id") not in current_turn_tools:
                packed = {
                    **message,
                    "content": dumps({
                        "summary": str(message.get("content") or "")[:300],
                        "truncated": True,
                        "note": "历史工具结果已压缩，需要请重新调用工具",
                    }),
                }
            size = len(str(packed.get("content") or ""))
            if selected and used + size > self.char_budget:
                break
            selected.append(packed)
            used += size
        selected.reverse()
        while selected and selected[0].get("role") != "user":
            selected.pop(0)
        return prefix + drop_dangling_tool_calls(selected)

    def message_count(self, session_id: str) -> int:
        epoch = self.current_epoch(session_id)
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=? AND epoch=?",
                (session_id, epoch),
            ).fetchone()
        return int(row["n"] if row else 0)

    def oldest_unsummarized(self, session_id: str, *, keep: int = 20) -> tuple[list[dict], int]:
        epoch = self.current_epoch(session_id)
        with self.store.read() as conn:
            last = conn.execute(
                """SELECT MAX(upto_message_id) AS upto FROM agent_session_summaries
                   WHERE session_id=? AND epoch=?""",
                (session_id, epoch),
            ).fetchone()
            upto = int(last["upto"] or 0) if last else 0
            rows = conn.execute(
                """SELECT * FROM agent_messages
                   WHERE session_id=? AND epoch=? AND id > ?
                   ORDER BY id""",
                (session_id, epoch, upto),
            ).fetchall()
        if len(rows) <= keep:
            return [], upto
        head = rows[:-keep]
        return [_message_from_row(row) | {"id": row["id"]} for row in head], int(head[-1]["id"])

    def save_summary(self, session_id: str, content: str, upto_message_id: int) -> None:
        epoch = self.current_epoch(session_id)
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO agent_session_summaries
                   (session_id, epoch, upto_message_id, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, epoch, int(upto_message_id), str(content or "")[:800], now()),
            )

    def latest_summary(self, session_id: str) -> str:
        epoch = self.current_epoch(session_id)
        with self.store.read() as conn:
            row = conn.execute(
                """SELECT content FROM agent_session_summaries
                   WHERE session_id=? AND epoch=? ORDER BY id DESC LIMIT 1""",
                (session_id, epoch),
            ).fetchone()
        return str(row["content"]) if row else ""

    def transcript(self, session_id: str, limit: int = 50) -> list[dict]:
        """给页面展示的对话记录，可跨 epoch。"""
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT role, content, created_at FROM agent_messages
                   WHERE session_id = ? AND role IN ('user', 'assistant') AND content <> ''
                   ORDER BY id DESC LIMIT ?""",
                (session_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"], "createdAt": row["created_at"]}
                for row in reversed(rows)]


def drop_dangling_tool_calls(messages: list[dict]) -> list[dict]:
    """丢掉没有对应 tool 回复的 assistant.tool_calls，避免会话永久 400。"""
    result_ids = {item.get("tool_call_id") for item in messages if item.get("role") == "tool"}
    cleaned = []
    kept_ids = set()
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            calls = [call for call in message["tool_calls"] if (call.get("id") or "") in result_ids]
            if not calls:
                if message.get("content"):
                    cleaned.append({key: value for key, value in message.items() if key != "tool_calls"})
                continue
            kept_ids.update(call.get("id") for call in calls)
            cleaned.append({**message, "tool_calls": calls})
        else:
            cleaned.append(message)
    final = []
    for message in cleaned:
        if message.get("role") == "tool" and message.get("tool_call_id") not in kept_ids | result_ids:
            continue
        if message.get("role") == "tool":
            assistant_ids = set()
            for item in cleaned:
                for call in item.get("tool_calls") or []:
                    assistant_ids.add(call.get("id"))
            if message.get("tool_call_id") not in assistant_ids:
                continue
        final.append(message)
    return final


def encode_tool_result(result, *, limit: int = 8000) -> str:
    if isinstance(result, dict) and "ok" in result and "summary" in result:
        text = dumps(result)
        if len(text) <= limit:
            return text
        return dumps({
            "ok": result.get("ok"),
            "summary": result.get("summary"),
            "data": None,
            "truncated": True,
            "hint": result.get("hint") or "结果过长已截断，请用更精确的参数重查",
        })
    text = dumps(result) if not isinstance(result, str) else result
    if len(text) <= limit:
        return text
    envelope = {
        "truncated": True,
        "preview": text[: max(0, limit - 80)],
        "hint": "结果过长已截断，请用更精确的参数重查",
    }
    return dumps(envelope)


def _message_from_row(row) -> dict[str, Any]:
    message: dict[str, Any] = {"role": row["role"], "content": row["content"]}
    if row["name"]:
        message["name"] = row["name"]
    if row["tool_call_id"]:
        message["tool_call_id"] = row["tool_call_id"]
    tool_calls = loads(row["tool_calls_json"])
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _stale(updated_at: str, idle_minutes: int) -> bool:
    text = str(updated_at or "").strip()
    if not text:
        return False
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds()
    return age >= idle_minutes * 60
