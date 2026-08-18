# -*- coding: utf-8 -*-
"""pending-action 确认状态机（架构方案 §5）。

L1/L2 工具不允许直接执行：先落一条 pending_action，渠道渲染确认，确认时以
`pending_action_id` 为幂等键执行且只执行一次。网页和钉钉走的是同一套状态机。

    pending → confirmed → executed
            ↘ cancelled / expired / failed
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from .store import AgentStore, dumps, later, loads, now
from .work_items import WorkItems
from ..staff_names import buyer_names_equivalent


DEFAULT_TTL_SECONDS = 30 * 60
DEFAULT_RESTART_CONFIRM_SECONDS = 5 * 60
FINAL_STATUSES = {"executed", "cancelled", "expired", "failed"}
# 这些工具共用一台 Playwright，同时只能有一条处于 confirmed（写入中）。
EXCLUSIVE_WRITE_TOOLS = frozenset({"process_insole_orders"})
DEFAULT_EXCLUSIVE_CLAIM_TIMEOUT = 15 * 60
DEFAULT_EXCLUSIVE_STALE_SECONDS = 20 * 60
DEFAULT_EXCLUSIVE_POLL = 1.0


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
    def __init__(
        self,
        store: AgentStore,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        exclusive_claim_timeout: int = DEFAULT_EXCLUSIVE_CLAIM_TIMEOUT,
        exclusive_stale_seconds: int = DEFAULT_EXCLUSIVE_STALE_SECONDS,
        exclusive_poll_seconds: float = DEFAULT_EXCLUSIVE_POLL,
    ):
        self.store = store
        self.ttl_seconds = int(ttl_seconds)
        self.exclusive_claim_timeout = int(exclusive_claim_timeout)
        self.exclusive_stale_seconds = int(exclusive_stale_seconds)
        self.exclusive_poll_seconds = float(exclusive_poll_seconds)
        self.work_items = WorkItems(store)

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
        actor_id: str = "",
        user_id: str = "",
    ) -> dict:
        action_id = secrets.token_hex(12)
        stamp = now()
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO pending_actions
                   (id, session_id, run_id, channel, operator, user_id, tool, risk, title,
                    arguments_json, preview_json, status, created_at, updated_at, expires_at,
                    actor_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (action_id, session_id, run_id, channel, str(operator or "")[:120],
                 str(user_id or "")[:80], tool, risk, str(title or tool)[:200],
                 dumps(arguments or {}), dumps(preview or {}),
                 stamp, stamp, later(self.ttl_seconds), str(actor_id or "")[:80]),
            )
        action = self.get(action_id)
        self.work_items.upsert_action(action)
        return action

    def get(self, action_id: str) -> dict:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            raise ActionError("待确认动作不存在", 404)
        if row["status"] == "pending" and _expired(row):
            self._mark(action_id, "expired", error="确认超时")
            return self.get(action_id)
        action = self._row(row)
        if action["status"] == "pending" and not self.work_items.get_by_source("pending_actions", action_id):
            self.work_items.upsert_action(action)
        return action

    def latest_open(self, *, session_id: str | None = None, actor_id: str = "",
                    tool: str = "") -> dict | None:
        """当前会话里最近一条待确认动作。钉钉回「确认」时用。"""
        self.expire_due()
        sql = "SELECT * FROM pending_actions WHERE status = 'pending'"
        params: list = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if actor_id:
            sql += " AND actor_id = ?"
            params.append(str(actor_id))
        if tool:
            sql += " AND tool = ?"
            params.append(tool)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self.store.read() as conn:
            row = conn.execute(sql, params).fetchone()
        return self._row(row) if row else None

    def list(self, *, session_id: str | None = None, status: str = "pending", limit: int = 20,
             operator: str = "", actor_id: str = "", user_id: str = "") -> list[dict]:
        self.expire_due()
        sql = "SELECT * FROM pending_actions WHERE status = ?"
        params: list = [status]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if actor_id and operator:
            sql += " AND (actor_id = ? OR operator = ?)"
            params.extend([str(actor_id), str(operator)])
        elif actor_id:
            sql += " AND actor_id = ?"
            params.append(str(actor_id))
        elif user_id:
            sql += " AND user_id = ?"
            params.append(str(user_id))
        elif operator:
            sql += " AND operator = ?"
            params.append(str(operator))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self.store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def expire_due(self) -> int:
        stamp = now()
        with self.store.write() as conn:
            rows = conn.execute(
                "SELECT id FROM pending_actions WHERE status = 'pending' AND expires_at <= ?",
                (stamp,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.execute(
                    """UPDATE pending_actions SET status = 'expired', error = '确认超时', updated_at = ?
                       WHERE status = 'pending' AND expires_at <= ?""",
                    (stamp, stamp),
                )
        for action_id in ids:
            try:
                self.work_items.upsert_action(self.get(action_id))
            except ActionError:
                continue
        return len(ids)

    @staticmethod
    def _canonical(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def _assert_preview_frozen(self, action: dict) -> None:
        preview = action.get("preview") if isinstance(action.get("preview"), dict) else {}
        frozen = preview.get("arguments")
        if not isinstance(frozen, dict):
            return
        if self._canonical(frozen) != self._canonical(action.get("arguments") or {}):
            raise ActionError("预览入参与待执行入参不一致，请重新发起", 409)

    def execute(self, action_id: str, operator: str, executor, *, actor_id: str = "") -> dict:
        """确认并执行一次。

        `executor(tool_name, arguments, action)` 由调用方提供；状态在事务里先推进到
        `confirmed`，所以并发的第二次确认拿不到执行权，重复确认直接回放已有结果。

        鞋垫写入再占一把跨动作锁：同时只允许一条 `process_insole_orders` 处于
        `confirmed`。后来的确认在后台等，不和前一批抢 Playwright。
        """
        current = self.get(action_id)
        self._assert_preview_frozen(current)
        action = self._claim(action_id, operator, actor_id=actor_id)
        if action["status"] == "executed":
            self.work_items.upsert_action(action)
            return action
        self.work_items.upsert_action(action)
        self._assert_preview_frozen(action)
        try:
            result = executor(action["tool"], action["arguments"], action)
        except Exception as exc:
            self._mark(action_id, "failed", error=f"{type(exc).__name__}: {exc}"[:1000])
            raise
        self._mark(action_id, "executed", result=result)
        executed = self.get(action_id)
        self.work_items.upsert_action(executed)
        return executed

    def recover_orphaned_writes(self, *, ttl_seconds: int | None = None) -> list[dict]:
        """进程启动时释放已死的写入锁。

        单进程里 ``confirmed`` 表示本进程正在写 ERP。重启后线程没了，
        结果未知不得自动重试；把动作退回 ``pending``，冻结清单保留，
        员工再回「确认」即可继续。已写入单靠 written / ERP 回读排除。
        """
        ttl = self.ttl_seconds if ttl_seconds is None else max(30, int(ttl_seconds))
        recovered = []
        with self.store.write(immediate=True) as conn:
            placeholders = ",".join("?" * len(EXCLUSIVE_WRITE_TOOLS))
            rows = conn.execute(
                f"""
                SELECT * FROM pending_actions
                WHERE tool IN ({placeholders}) AND status = 'confirmed'
                """,
                tuple(EXCLUSIVE_WRITE_TOOLS),
            ).fetchall()
            stamp = now()
            expires = later(ttl)
            for row in rows:
                conn.execute(
                    """UPDATE pending_actions
                       SET status='pending', error=?, confirmed_at='', confirmed_by='',
                           expires_at=?, updated_at=?
                       WHERE id=? AND status='confirmed'""",
                    ("进程重启，写入未完成，请再回确认", expires, stamp, row["id"]),
                )
                recovered.append(row["id"])
        actions = []
        for action_id in recovered:
            try:
                action = self.get(action_id)
            except ActionError:
                continue
            self.work_items.upsert_action(action)
            actions.append(action)
        return actions

    def refresh_open_after_restart(
        self, *, ttl_seconds: int = DEFAULT_RESTART_CONFIRM_SECONDS,
    ) -> list[dict]:
        """重启后重开未确认动作：5 分钟内不确认则释放占用。"""
        ttl = max(30, int(ttl_seconds))
        self.recover_orphaned_writes(ttl_seconds=ttl)
        self.expire_due()
        stamp = now()
        expires = later(ttl)
        minutes = max(1, ttl // 60)
        note = f"服务重启，请在 {minutes} 分钟内确认，超时将释放"
        with self.store.write(immediate=True) as conn:
            rows = conn.execute(
                "SELECT id FROM pending_actions WHERE status = 'pending'",
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.execute(
                    """UPDATE pending_actions
                       SET expires_at=?, error=?, updated_at=?
                       WHERE status='pending'""",
                    (expires, note, stamp),
                )
        opened = []
        for action_id in ids:
            try:
                action = self.get(action_id)
            except ActionError:
                continue
            self.work_items.upsert_action(action)
            opened.append(action)
        return opened

    @staticmethod
    def restart_notice(action: dict, *, ttl_seconds: int = DEFAULT_RESTART_CONFIRM_SECONDS) -> str:
        """重启后补发的待确认说明。"""
        preview = action.get("preview") if isinstance(action.get("preview"), dict) else {}
        count = preview.get("processableCount") or len(preview.get("oIds") or [])
        title = str(action.get("title") or action.get("tool") or "待确认动作")
        minutes = max(1, int(ttl_seconds) // 60)
        lines = [
            "【服务重启】有待确认动作尚未执行。",
            "",
            f"{title}：{count} 单" if count else title,
            f"请在 {minutes} 分钟内回复「确认」继续；超时将释放占用，需重新查询。",
        ]
        return "\n".join(lines)

    def _claim(self, action_id: str, operator: str, *, actor_id: str = "") -> dict:
        operator = str(operator or "").strip()
        deadline = time.monotonic() + max(0, self.exclusive_claim_timeout)
        while True:
            wait_for = None
            with self.store.write(immediate=True) as conn:
                row = conn.execute(
                    "SELECT * FROM pending_actions WHERE id = ?", (action_id,),
                ).fetchone()
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
                if not operator:
                    raise ActionError("确认必须填写操作人姓名", 403)
                stored_actor = str(row["actor_id"] or "") if "actor_id" in row.keys() else ""
                if stored_actor:
                    if str(actor_id or "") != stored_actor:
                        raise ActionError("必须由发起该动作的员工确认", 403)
                elif row["operator"] and operator != row["operator"] and not buyer_names_equivalent(
                    operator, row["operator"],
                ):
                    raise ActionError("必须由发起该动作的员工确认", 403)
                if row["tool"] in EXCLUSIVE_WRITE_TOOLS:
                    other = self._blocking_write(conn, row["tool"], action_id)
                    if other is not None:
                        wait_for = other
                    else:
                        self._mark_confirmed(conn, action_id, operator)
                        return self._row(row)
                else:
                    self._mark_confirmed(conn, action_id, operator)
                    return self._row(row)
            if wait_for is not None:
                if time.monotonic() >= deadline:
                    who = wait_for.get("operator") or "他人"
                    raise ActionError(
                        f"已有鞋垫写入进行中（{who}），请稍后再确认", 409,
                    )
                time.sleep(max(0.02, self.exclusive_poll_seconds))

    def _blocking_write(self, conn, tool: str, action_id: str) -> dict | None:
        """另一条同工具 confirmed 写入。过期未完成的视为中断，释放锁。"""
        rows = conn.execute(
            """
            SELECT id, operator, confirmed_at, updated_at
            FROM pending_actions
            WHERE tool = ? AND status = 'confirmed' AND id != ?
            ORDER BY confirmed_at ASC
            """,
            (tool, action_id),
        ).fetchall()
        for row in rows:
            if self._stale_confirmed(row):
                conn.execute(
                    """UPDATE pending_actions
                       SET status='failed', error=?, updated_at=?
                       WHERE id=? AND status='confirmed'""",
                    ("写入中断（超时未完成）", now(), row["id"]),
                )
                continue
            return {"id": row["id"], "operator": str(row["operator"] or "")}
        return None

    @staticmethod
    def _mark_confirmed(conn, action_id: str, operator: str) -> None:
        stamp = now()
        conn.execute(
            """UPDATE pending_actions
               SET status='confirmed', confirmed_at=?, confirmed_by=?, updated_at=? WHERE id=?""",
            (stamp, operator[:120], stamp, action_id),
        )

    def _stale_confirmed(self, row) -> bool:
        raw = str(row["confirmed_at"] or row["updated_at"] or "")
        if not raw:
            return True
        try:
            text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
        return age >= timedelta(seconds=max(1, self.exclusive_stale_seconds))

    def cancel(self, action_id: str, operator: str = "", *, actor_id: str = "") -> dict:
        operator = str(operator or "").strip()
        with self.store.write(immediate=True) as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
            if not row:
                raise ActionError("待确认动作不存在", 404)
            if row["status"] == "cancelled":
                return self._row(row)
            if row["status"] in FINAL_STATUSES:
                raise ActionError(f"该动作已{self._status_label(row['status'])}，不能取消", 409)
            if not operator:
                raise ActionError("取消必须填写操作人姓名", 403)
            stored_actor = str(row["actor_id"] or "") if "actor_id" in row.keys() else ""
            if stored_actor:
                if str(actor_id or "") != stored_actor:
                    raise ActionError("必须由发起该动作的员工取消", 403)
            elif row["operator"] and operator != row["operator"] and not buyer_names_equivalent(
                operator, row["operator"],
            ):
                raise ActionError("必须由发起该动作的员工取消", 403)
            conn.execute(
                "UPDATE pending_actions SET status='cancelled', updated_at=? WHERE id=?",
                (now(), action_id),
            )
        cancelled = self.get(action_id)
        self.work_items.upsert_action(cancelled)
        return cancelled

    def _mark(self, action_id: str, status: str, *, result=None, error: str | None = None) -> None:
        with self.store.write() as conn:
            conn.execute(
                """UPDATE pending_actions SET status=?, result_json=COALESCE(?, result_json),
                   error=COALESCE(?, error), executed_at=?, updated_at=? WHERE id=?""",
                (status, dumps(result) if result is not None else None, error,
                 now() if status == "executed" else None, now(), action_id),
            )
        try:
            self.work_items.upsert_action(self.get(action_id))
        except ActionError:
            pass

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
            "userId": row["user_id"] if "user_id" in row.keys() else "",
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
            "confirmedBy": row["confirmed_by"] if "confirmed_by" in row.keys() else "",
            "actorId": row["actor_id"] if "actor_id" in row.keys() else "",
            "executedAt": row["executed_at"],
        }
