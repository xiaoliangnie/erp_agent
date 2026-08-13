# -*- coding: utf-8 -*-
"""pending-action 确认状态机（架构方案 §5）。

L1/L2 工具不允许直接执行：先落一条 pending_action，渠道渲染确认，确认时以
`pending_action_id` 为幂等键执行且只执行一次。网页和钉钉走的是同一套状态机。

    pending → confirmed → executed
            ↘ cancelled / expired / failed
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from .store import AgentStore, dumps, later, loads, now
from ..staff_names import buyer_names_equivalent


DEFAULT_TTL_SECONDS = 30 * 60
FINAL_STATUSES = {"executed", "cancelled", "expired", "failed"}


class ActionError(ValueError):
    """可安全返回给调用方的确认流错误。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _expired(row) -> bool:
    try:
        return datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


class PendingActions:
    def __init__(self, store: AgentStore, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.store = store
        self.ttl_seconds = int(ttl_seconds)

    def create(
        self,
        *,
        tool: str,
        risk: str,
        arguments: dict,
        title: str = "",
        preview: dict | None = None,
        operator: str = "",
        channel: str = "web",
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        action_id = secrets.token_hex(12)
        stamp = now()
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO pending_actions
                   (id, session_id, run_id, channel, operator, tool, risk, title,
                    arguments_json, preview_json, status, created_at, updated_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (action_id, session_id, run_id, channel, str(operator or "")[:120], tool, risk,
                 str(title or tool)[:200], dumps(arguments or {}), dumps(preview or {}),
                 stamp, stamp, later(self.ttl_seconds)),
            )
        return self.get(action_id)

    def get(self, action_id: str) -> dict:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            raise ActionError("待确认动作不存在", 404)
        if row["status"] == "pending" and _expired(row):
            self._mark(action_id, "expired", error="确认超时")
            return self.get(action_id)
        return self._row(row)

    def list(self, *, session_id: str | None = None, status: str = "pending", limit: int = 20) -> list[dict]:
        self.expire_due()
        sql = "SELECT * FROM pending_actions WHERE status = ?"
        params: list = [status]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self.store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def expire_due(self) -> int:
        with self.store.write() as conn:
            cursor = conn.execute(
                """UPDATE pending_actions SET status = 'expired', error = '确认超时', updated_at = ?
                   WHERE status = 'pending' AND expires_at <= ?""",
                (now(), now()),
            )
        return cursor.rowcount or 0

    def execute(self, action_id: str, operator: str, executor) -> dict:
        """确认并执行一次。

        `executor(tool_name, arguments, action)` 由调用方提供；状态在事务里先推进到
        `confirmed`，所以并发的第二次确认拿不到执行权，重复确认直接回放已有结果。
        """
        operator = str(operator or "").strip()
        with self.store.write(immediate=True) as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
            if not row:
                raise ActionError("待确认动作不存在", 404)
            if row["status"] == "executed":
                return self._row(row)
            if row["status"] in FINAL_STATUSES:
                raise ActionError(f"该动作已{self._status_label(row['status'])}，不能再执行", 409)
            if row["status"] == "confirmed":
                raise ActionError("该动作正在执行中，请稍候", 409)
            if _expired(row):
                conn.execute(
                    "UPDATE pending_actions SET status='expired', error='确认超时', updated_at=? WHERE id=?",
                    (now(), action_id),
                )
                raise ActionError("确认已超时，请重新发起", 409)
            if row["operator"] and operator != row["operator"] and not buyer_names_equivalent(
                operator, row["operator"],
            ):
                raise ActionError("必须由发起该动作的员工确认", 403)
            conn.execute(
                "UPDATE pending_actions SET status='confirmed', confirmed_at=?, updated_at=? WHERE id=?",
                (now(), now(), action_id),
            )
            action = self._row(row)
        try:
            result = executor(action["tool"], action["arguments"], action)
        except Exception as exc:
            self._mark(action_id, "failed", error=f"{type(exc).__name__}: {exc}"[:1000])
            raise
        self._mark(action_id, "executed", result=result)
        return self.get(action_id)

    def cancel(self, action_id: str, operator: str = "") -> dict:
        operator = str(operator or "").strip()
        with self.store.write(immediate=True) as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
            if not row:
                raise ActionError("待确认动作不存在", 404)
            if row["status"] == "cancelled":
                return self._row(row)
            if row["status"] in FINAL_STATUSES:
                raise ActionError(f"该动作已{self._status_label(row['status'])}，不能取消", 409)
            if row["operator"] and operator and operator != row["operator"] and not buyer_names_equivalent(
                operator, row["operator"],
            ):
                raise ActionError("必须由发起该动作的员工取消", 403)
            conn.execute(
                "UPDATE pending_actions SET status='cancelled', updated_at=? WHERE id=?",
                (now(), action_id),
            )
        return self.get(action_id)

    def _mark(self, action_id: str, status: str, *, result=None, error: str | None = None) -> None:
        with self.store.write() as conn:
            conn.execute(
                """UPDATE pending_actions SET status=?, result_json=COALESCE(?, result_json),
                   error=COALESCE(?, error), executed_at=?, updated_at=? WHERE id=?""",
                (status, dumps(result) if result is not None else None, error,
                 now() if status == "executed" else None, now(), action_id),
            )

    @staticmethod
    def _status_label(status: str) -> str:
        return {"executed": "执行", "cancelled": "取消", "expired": "超时",
                "failed": "失败"}.get(status, status)

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "sessionId": row["session_id"],
            "runId": row["run_id"],
            "channel": row["channel"],
            "operator": row["operator"],
            "tool": row["tool"],
            "risk": row["risk"],
            "title": row["title"],
            "arguments": loads(row["arguments_json"], {}),
            "preview": loads(row["preview_json"], {}),
            "status": row["status"],
            "result": loads(row["result_json"]),
            "error": row["error"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
            "confirmedAt": row["confirmed_at"],
            "executedAt": row["executed_at"],
        }
