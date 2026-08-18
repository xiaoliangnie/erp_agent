# -*- coding: utf-8 -*-
"""进程内 Job 队列：HTTP 只入队，后台线程领取执行。

第一阶段单进程。合同预览仍在确认线程里跑（员工要立刻拿到文件）；
催办补发、Outbox 刷新这类可恢复任务走这里。
"""
from __future__ import annotations

import logging
import secrets
import threading

from .store import AgentStore, dumps, later, loads, now


LEASE_SECONDS = 300


logger = logging.getLogger(__name__)


class JobError(RuntimeError):
    """队列或 handler 失败。"""


class JobQueue:
    def __init__(self, store: AgentStore):
        self.store = store

    def enqueue(
        self,
        kind: str,
        payload: dict | None = None,
        *,
        max_attempts: int = 5,
        delay_seconds: int = 0,
    ) -> dict:
        job_id = secrets.token_hex(12)
        stamp = now()
        available = later(max(0, int(delay_seconds))) if delay_seconds else stamp
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, kind, status, payload_json, attempts, max_attempts,
                    available_at, created_at, updated_at)
                   VALUES (?, ?, 'queued', ?, 0, ?, ?, ?, ?)""",
                (job_id, str(kind or "")[:80], dumps(payload or {}),
                 max(1, int(max_attempts)), available, stamp, stamp),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise JobError("任务不存在")
        return self._row(row)

    def list(self, *, status: str = "", kind: str = "", limit: int = 50) -> list[dict]:
        clauses = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        with self.store.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def recover_expired(self) -> int:
        """把租约过期的 running 退回 queued，避免进程重启后任务永久卡住。"""
        stamp = now()
        with self.store.write(immediate=True) as conn:
            expired = conn.execute(
                """SELECT id FROM jobs
                   WHERE status = 'running'
                     AND (lease_until = '' OR lease_until <= ?)""",
                (stamp,),
            ).fetchall()
            if expired:
                conn.execute(
                    """UPDATE jobs
                       SET status='queued', lease_token='', lease_until='',
                           error='running 租约过期，已回收', updated_at=?
                       WHERE status='running'
                         AND (lease_until = '' OR lease_until <= ?)""",
                    (stamp, stamp),
                )
        return len(expired)

    def claim(self) -> dict | None:
        self.recover_expired()
        stamp = now()
        token = secrets.token_hex(8)
        with self.store.write(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM jobs
                   WHERE status = 'queued' AND available_at <= ?
                   ORDER BY created_at ASC LIMIT 1""",
                (stamp,),
            ).fetchone()
            if not row:
                return None
            claimed = conn.execute(
                """UPDATE jobs SET status='running', attempts=attempts+1,
                   started_at=?, updated_at=?, lease_token=?, lease_until=?
                   WHERE id=? AND status='queued'""",
                (stamp, stamp, token, later(LEASE_SECONDS), row["id"]),
            )
            if claimed.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return self._row(row) if row else None

    def finish(self, job_id: str, *, result=None) -> dict:
        stamp = now()
        with self.store.write() as conn:
            conn.execute(
                """UPDATE jobs SET status='succeeded', result_json=?, error=NULL,
                   finished_at=?, updated_at=?, lease_token='', lease_until=''
                   WHERE id=?""",
                (dumps(result) if result is not None else None, stamp, stamp, job_id),
            )
        return self.get(job_id)

    def fail(self, job_id: str, error: str, *, retry_seconds: int = 30) -> dict:
        stamp = now()
        with self.store.write(immediate=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise JobError("任务不存在")
            attempts = int(row["attempts"] or 0)
            max_attempts = int(row["max_attempts"] or 1)
            if attempts < max_attempts:
                conn.execute(
                    """UPDATE jobs SET status='queued', error=?, available_at=?,
                       updated_at=?, lease_token='', lease_until='' WHERE id=?""",
                    (str(error)[:1000], later(max(1, int(retry_seconds))), stamp, job_id),
                )
            else:
                conn.execute(
                    """UPDATE jobs SET status='failed', error=?, finished_at=?,
                       updated_at=?, lease_token='', lease_until='' WHERE id=?""",
                    (str(error)[:1000], stamp, stamp, job_id),
                )
        return self.get(job_id)

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "payload": loads(row["payload_json"], {}),
            "result": loads(row["result_json"]),
            "error": row["error"],
            "attempts": row["attempts"],
            "maxAttempts": row["max_attempts"],
            "availableAt": row["available_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "leaseToken": row["lease_token"] if "lease_token" in row.keys() else "",
            "leaseUntil": row["lease_until"] if "lease_until" in row.keys() else "",
        }


class JobWorker:
    """领取 queued Job，并顺带刷新 Outbox。"""

    def __init__(self, queue: JobQueue, *, outbox=None, handlers=None,
                 poll_seconds: float = 2.0, expire=None):
        self.queue = queue
        self.outbox = outbox
        self.handlers = dict(handlers or {})
        self.expire = expire
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.enabled = False
        self.last_error = ""

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "lastError": self.last_error,
            "queued": len(self.queue.list(status="queued", limit=20)),
        }

    def start(self) -> dict:
        self.enabled = True
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agent-jobs", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self.enabled = False

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Job worker tick failed")

    def tick(self) -> dict:
        flushed = 0
        if self.outbox is not None:
            flushed = len(self.outbox.deliver_due())
        if callable(self.expire):
            try:
                self.expire()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("待确认过期扫描失败")
        job = self.queue.claim()
        if job is None:
            return {"flushed": flushed, "job": None}
        handler = self.handlers.get(job["kind"])
        if handler is None:
            self.queue.fail(job["id"], f"没有 handler：{job['kind']}", retry_seconds=60)
            return {"flushed": flushed, "job": job["id"], "error": "unknown kind"}
        try:
            result = handler(job["payload"] or {})
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.queue.fail(job["id"], self.last_error)
            return {"flushed": flushed, "job": job["id"], "error": self.last_error}
        self.queue.finish(job["id"], result=result)
        self.last_error = ""
        return {"flushed": flushed, "job": job["id"], "ok": True}
