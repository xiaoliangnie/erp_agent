# -*- coding: utf-8 -*-
"""钉钉出站 Outbox：先落库再发送，失败可补发。

语义 at-least-once：进程在「已生成、未发出」处重启只补发，不重跑工具。
重复投递时调用方应提示员工可能看到两条。
"""
from __future__ import annotations

import logging
import secrets

from .store import AgentStore, dumps, later, loads, now


logger = logging.getLogger(__name__)


class OutboxError(RuntimeError):
    """出站台账错误。"""


class Outbox:
    def __init__(self, store: AgentStore, *, sender=None, max_attempts: int = 5):
        self.store = store
        self.sender = sender
        self.max_attempts = max(1, int(max_attempts))

    def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        idempotency_key: str | None = None,
    ) -> dict:
        key = str(idempotency_key or "").strip()[:200] or None
        stamp = now()
        with self.store.write(immediate=True) as conn:
            if key:
                existing = conn.execute(
                    "SELECT * FROM outbox_messages WHERE idempotency_key = ?", (key,),
                ).fetchone()
                if existing:
                    return self._row(existing)
            item_id = secrets.token_hex(12)
            conn.execute(
                """INSERT INTO outbox_messages
                   (id, kind, status, idempotency_key, payload_json, attempts,
                    next_attempt_at, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?, 0, ?, ?, ?)""",
                (item_id, str(kind or "")[:80], key, dumps(payload or {}),
                 stamp, stamp, stamp),
            )
            row = conn.execute("SELECT * FROM outbox_messages WHERE id = ?", (item_id,)).fetchone()
        return self._row(row)

    def get(self, item_id: str) -> dict:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM outbox_messages WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise OutboxError("出站消息不存在")
        return self._row(row)

    def pending_count(self) -> int:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox_messages WHERE status IN ('pending', 'failed')",
            ).fetchone()
        return int(row["n"] if row else 0)

    def list(self, *, status: str = "", limit: int = 20) -> list[dict]:
        clauses = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 100)))
        with self.store.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM outbox_messages {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._public(self._row(row)) for row in rows]

    def send_dingtalk(
        self,
        *,
        title: str,
        text: str,
        channel: str = "markdown",
        user_ids=(),
        at_user_ids=(),
        at_mobiles=(),
        at_all: bool = False,
        idempotency_key: str | None = None,
    ) -> dict:
        """入队并立刻尝试发送；失败留在台账里等 JobWorker 补发。"""
        payload = {
            "title": title,
            "text": text,
            "channel": channel,
            "userIds": [str(item) for item in user_ids if str(item or "").strip()],
            "atUserIds": [str(item) for item in at_user_ids if str(item or "").strip()],
            "atMobiles": [str(item) for item in at_mobiles if str(item or "").strip()],
            "atAll": bool(at_all),
        }
        item = self.enqueue("dingtalk", payload, idempotency_key=idempotency_key)
        if item["status"] == "delivered":
            return {
                "skipped": True,
                "reason": "同一条出站消息已经送达",
                "duplicatePossible": False,
                "outboxId": item["id"],
                "channel": (item.get("result") or {}).get("channel") or channel,
            }
        return self.deliver(item["id"])

    def deliver(self, item_id: str) -> dict:
        claimed = self._claim(item_id)
        if claimed is None:
            item = self.get(item_id)
            if item["status"] == "delivered":
                return {
                    "skipped": True,
                    "reason": "已经送达",
                    "duplicatePossible": True,
                    "outboxId": item_id,
                    "channel": (item.get("result") or {}).get("channel"),
                }
            return {
                "skipped": True,
                "reason": "已被其他投递占用",
                "duplicatePossible": False,
                "outboxId": item_id,
            }
        if self.sender is None:
            self._mark(item_id, "failed", error="发送通道未装配")
            raise OutboxError("发送通道未装配")
        payload = claimed.get("payload") or {}
        try:
            response = self._dispatch(payload)
        except Exception as exc:
            attempts = int(claimed.get("attempts") or 0) + 1
            status = "failed" if attempts >= self.max_attempts else "pending"
            self._mark(
                item_id, status, error=f"{type(exc).__name__}: {exc}"[:1000],
                attempts=attempts, retry=status == "pending",
            )
            raise
        self._mark(item_id, "delivered", result=response, attempts=int(claimed.get("attempts") or 0) + 1)
        return {
            "sent": True,
            "outboxId": item_id,
            "channel": response.get("channel"),
            "duplicatePossible": int(claimed.get("attempts") or 0) > 0,
            "response": response,
        }

    def _claim(self, item_id: str, *, lease_seconds: int = 60) -> dict | None:
        """BEGIN IMMEDIATE 里把 pending/failed/过期 sending 改成 sending；只有一个调用方能拿到。"""
        item_id = str(item_id or "").strip()
        if not item_id:
            return None
        stamp = now()
        token = secrets.token_hex(8)
        lease_until = later(max(5, int(lease_seconds)))
        with self.store.write(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM outbox_messages WHERE id = ?", (item_id,),
            ).fetchone()
            if row is None:
                return None
            status = row["status"]
            if status == "delivered":
                return None
            if status == "sending":
                lease = str(row["lease_until"] if "lease_until" in row.keys() else "") or ""
                if lease and lease > stamp:
                    return None
            elif status not in ("pending", "failed"):
                return None
            attempts = int(row["attempts"] or 0)
            conn.execute(
                """UPDATE outbox_messages
                   SET status='sending', lease_token=?, lease_until=?,
                       attempts=?, updated_at=?
                   WHERE id=? AND status=?""",
                (token, lease_until, attempts, stamp, item_id, status),
            )
            claimed = conn.execute(
                "SELECT * FROM outbox_messages WHERE id = ? AND lease_token = ?",
                (item_id, token),
            ).fetchone()
        return self._row(claimed) if claimed else None

    def deliver_due(self, *, limit: int = 20) -> list[dict]:
        stamp = now()
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT id FROM outbox_messages
                   WHERE (
                       status IN ('pending', 'failed') AND next_attempt_at <= ?
                   ) OR (
                       status = 'sending' AND lease_until != '' AND lease_until <= ?
                   )
                   ORDER BY created_at ASC LIMIT ?""",
                (stamp, stamp, max(1, min(int(limit), 50))),
            ).fetchall()
        delivered = []
        for row in rows:
            try:
                delivered.append(self.deliver(row["id"]))
            except Exception as exc:
                logger.warning("Outbox 补发失败 %s：%s", row["id"], exc)
        return delivered

    def _dispatch(self, payload: dict) -> dict:
        channel = str(payload.get("channel") or "markdown")
        title = str(payload.get("title") or "")
        text = str(payload.get("text") or "")
        if channel == "oto" and getattr(self.sender, "send_oto_markdown", None):
            return self.sender.send_oto_markdown(
                title, text, user_ids=payload.get("userIds") or (),
            )
        return self.sender.send_markdown(
            title, text,
            at_user_ids=payload.get("atUserIds") or (),
            at_mobiles=payload.get("atMobiles") or (),
            at_all=bool(payload.get("atAll")),
        )

    def _mark(
        self,
        item_id: str,
        status: str,
        *,
        result=None,
        error: str | None = None,
        attempts: int | None = None,
        retry: bool = False,
    ) -> None:
        stamp = now()
        next_at = later(30) if retry else stamp
        with self.store.write() as conn:
            conn.execute(
                """UPDATE outbox_messages
                   SET status=?, result_json=COALESCE(?, result_json), error=?,
                       attempts=COALESCE(?, attempts), next_attempt_at=?,
                       delivered_at=?, updated_at=?
                   WHERE id=?""",
                (
                    status,
                    dumps(result) if result is not None else None,
                    error,
                    attempts,
                    next_at,
                    stamp if status == "delivered" else None,
                    stamp,
                    item_id,
                ),
            )

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "idempotencyKey": row["idempotency_key"],
            "payload": loads(row["payload_json"], {}),
            "result": loads(row["result_json"]),
            "error": row["error"],
            "attempts": row["attempts"],
            "nextAttemptAt": row["next_attempt_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "deliveredAt": row["delivered_at"],
        }

    @staticmethod
    def _public(item: dict) -> dict:
        """工作台只看投递状态，不回 userId / 手机号。"""
        payload = item.get("payload") or {}
        return {
            "id": item["id"],
            "kind": item["kind"],
            "status": item["status"],
            "channel": payload.get("channel") or "",
            "title": str(payload.get("title") or "")[:80],
            "attempts": item["attempts"],
            "error": item["error"] or "",
            "createdAt": item["createdAt"],
            "deliveredAt": item["deliveredAt"],
            "duplicatePossible": int(item.get("attempts") or 0) > 1,
        }
