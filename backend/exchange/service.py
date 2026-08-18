"""订单换货任务状态机。

ERP 登录态和写操作始终留在浏览器 worker 中；本模块只保存规则、明确的订单号、
dry-run 计划、人工确认和执行结果。SQLite 是第一阶段的本地业务队列，接口保持独立，
后续可无损替换为 Agent 业务 MySQL。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


TERMINAL_STATUSES = {"done", "failed", "cancelled", "stuck"}
ACTIVE_STATUSES = {"pending", "planning", "awaiting_confirm", "confirmed", "executing"}
PLAN_TIMEOUT_SECONDS = 5 * 60
EXECUTE_TIMEOUT_SECONDS = 15 * 60
MAX_CLAIM_ATTEMPTS = 3
OID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
logger = logging.getLogger(__name__)


class ExchangeError(ValueError):
    """可安全返回给调用方的业务错误。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_ts(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale(stamp, timeout_seconds: int, *, now: datetime | None = None) -> bool:
    parsed = _parse_ts(stamp)
    if parsed is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - parsed).total_seconds() >= timeout_seconds


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    names = {info[1] for info in conn.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


class ExchangeService:
    """持久化换货任务并强制 dry-run → 人工确认 → 单次执行。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        plan_timeout_seconds: int = PLAN_TIMEOUT_SECONDS,
        execute_timeout_seconds: int = EXECUTE_TIMEOUT_SECONDS,
        max_claim_attempts: int = MAX_CLAIM_ATTEMPTS,
        on_stuck=None,
    ):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.plan_timeout_seconds = max(1, int(plan_timeout_seconds))
        self.execute_timeout_seconds = max(1, int(execute_timeout_seconds))
        self.max_claim_attempts = max(1, int(max_claim_attempts))
        self.on_stuck = on_stuck
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS exchange_jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    plan_json TEXT,
                    progress_json TEXT NOT NULL DEFAULT '[]',
                    result_json TEXT,
                    worker_id TEXT,
                    execution_token_hash TEXT,
                    execution_token_delivered INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_exchange_jobs_status_created
                    ON exchange_jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS exchange_workers (
                    worker_id TEXT PRIMARY KEY,
                    page_url TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '',
                    ready INTEGER NOT NULL DEFAULT 0,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchange_searches (
                    id TEXT PRIMARY KEY,
                    sku TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    worker_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_exchange_searches_status_created
                    ON exchange_searches(status, created_at);
                CREATE TABLE IF NOT EXISTS erp_read_probes (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    worker_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_erp_read_probes_status_created
                    ON erp_read_probes(status, created_at);
                """
            )
            _ensure_column(conn, "exchange_jobs", "claimed_at", "TEXT")
            _ensure_column(conn, "exchange_jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "exchange_searches", "claimed_at", "TEXT")
            _ensure_column(conn, "exchange_searches", "attempts", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "erp_read_probes", "claimed_at", "TEXT")
            _ensure_column(conn, "erp_read_probes", "attempts", "INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _claim_stamp(row: sqlite3.Row) -> str | None:
        keys = row.keys()
        claimed = row["claimed_at"] if "claimed_at" in keys else None
        return str(claimed or row["updated_at"] or "") or None

    @staticmethod
    def _attempts(row: sqlite3.Row) -> int:
        keys = row.keys()
        if "attempts" not in keys:
            return 0
        try:
            return int(row["attempts"] or 0)
        except (TypeError, ValueError):
            return 0

    def _reclaim_queue_locked(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        claimed_status: str,
        now: str,
        fail_message: str,
    ) -> None:
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE status=?", (claimed_status,)
        ).fetchall()
        for row in rows:
            if not _is_stale(self._claim_stamp(row), self.plan_timeout_seconds, now=now_dt):
                continue
            attempts = self._attempts(row) + 1
            if attempts >= self.max_claim_attempts:
                conn.execute(
                    f"""UPDATE {table} SET status='failed', attempts=?, worker_id=NULL,
                        claimed_at=NULL, error=?, updated_at=?, finished_at=? WHERE id=?""",
                    (attempts, fail_message, now, now, row["id"]),
                )
            else:
                conn.execute(
                    f"""UPDATE {table} SET status='pending', attempts=?, worker_id=NULL,
                        claimed_at=NULL, updated_at=? WHERE id=?""",
                    (attempts, now, row["id"]),
                )

    def _reclaim_jobs_locked(self, conn: sqlite3.Connection, now: str) -> list[dict]:
        self._reclaim_queue_locked(
            conn, table="exchange_jobs", claimed_status="planning", now=now,
            fail_message="试算领取超时，已超过最大重试次数",
        )
        stuck_jobs: list[dict] = []
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
        rows = conn.execute("SELECT * FROM exchange_jobs WHERE status='executing'").fetchall()
        for row in rows:
            if not _is_stale(self._claim_stamp(row), self.execute_timeout_seconds, now=now_dt):
                continue
            conn.execute(
                """UPDATE exchange_jobs SET status='stuck', error=?, updated_at=? WHERE id=?""",
                ("执行超时，已中断且未自动重投，请人工核对 ERP", now, row["id"]),
            )
            fresh = conn.execute("SELECT * FROM exchange_jobs WHERE id=?", (row["id"],)).fetchone()
            stuck_jobs.append(self._row(fresh))
        return stuck_jobs

    def _reclaim_all_locked(self, conn: sqlite3.Connection, now: str) -> list[dict]:
        stuck = self._reclaim_jobs_locked(conn, now)
        self._reclaim_queue_locked(
            conn, table="exchange_searches", claimed_status="searching", now=now,
            fail_message="订单搜索领取超时，已超过最大重试次数",
        )
        self._reclaim_queue_locked(
            conn, table="erp_read_probes", claimed_status="reading", now=now,
            fail_message="只读探测领取超时，已超过最大重试次数",
        )
        return stuck

    def _emit_stuck(self, jobs: list[dict]) -> None:
        callback = self.on_stuck
        if not callback:
            return
        for job in jobs:
            try:
                callback(job)
            except Exception as exc:
                logger.error("Exchange stuck callback failed: %s", exc)

    def _reclaim_now(self) -> None:
        stuck: list[dict] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stuck = self._reclaim_all_locked(conn, _now())
        self._emit_stuck(stuck)

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        self._reclaim_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def create_probe(self, kind: str, reference: str) -> dict:
        kind = str(kind or "").strip()
        reference = str(reference or "").strip()
        if kind != "purchase_items" or not reference.isdigit():
            raise ExchangeError("只读探测参数不正确")
        now = _now()
        probe_id = secrets.token_hex(12)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO erp_read_probes (id,kind,reference,status,created_at,updated_at) VALUES (?,?,?,'pending',?,?)",
                (probe_id, kind, reference, now, now),
            )
            return self._probe_row(conn.execute("SELECT * FROM erp_read_probes WHERE id=?", (probe_id,)).fetchone())

    def get_probe(self, probe_id: str) -> dict:
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM erp_read_probes WHERE id=?", (probe_id,)).fetchone()
            probe = self._probe_row(row) if row else None
        if not probe:
            raise ExchangeError("只读探测任务不存在", 404)
        return probe

    def next_probe(self, worker_id: str) -> dict | None:
        worker_id = self._validate_worker_id(worker_id)
        now = _now()
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM erp_read_probes WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE erp_read_probes SET status='reading',worker_id=?,claimed_at=?,updated_at=? WHERE id=?",
                (worker_id, now, now, row["id"]),
            )
            return self._probe_row(conn.execute("SELECT * FROM erp_read_probes WHERE id=?", (row["id"],)).fetchone())

    def report_probe(self, probe_id: str, worker_id: str, result: dict) -> dict:
        worker_id = self._validate_worker_id(worker_id)
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM erp_read_probes WHERE id=?", (probe_id,)).fetchone()
            if not row or row["status"] != "reading" or row["worker_id"] != worker_id:
                raise ExchangeError("只读探测任务状态或 Worker 不匹配", 409)
            error = str(result.get("error") or "")[:1000] or None
            status = "failed" if error else "done"
            conn.execute("UPDATE erp_read_probes SET status=?,result_json=?,error=?,updated_at=?,finished_at=? WHERE id=?",
                         (status, _json(result), error, now, now, probe_id))
            return self._probe_row(conn.execute("SELECT * FROM erp_read_probes WHERE id=?", (probe_id,)).fetchone())

    @staticmethod
    def _probe_row(row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        return {
            "id": row["id"], "kind": row["kind"], "reference": row["reference"],
            "status": row["status"], "result": _loads(row["result_json"], None),
            "workerId": row["worker_id"], "error": row["error"],
            "attempts": ExchangeService._attempts(row),
            "claimedAt": row["claimed_at"] if "claimed_at" in row.keys() else None,
            "createdAt": row["created_at"], "updatedAt": row["updated_at"], "finishedAt": row["finished_at"],
        }

    def create_search(self, sku: str) -> dict:
        sku = str(sku or "").strip()
        if not sku or len(sku) > 128:
            raise ExchangeError("搜索 SKU 格式不正确")
        now = _now()
        search_id = secrets.token_hex(12)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO exchange_searches (id,sku,status,created_at,updated_at) VALUES (?,?,'pending',?,?)",
                (search_id, sku, now, now),
            )
            row = conn.execute("SELECT * FROM exchange_searches WHERE id=?", (search_id,)).fetchone()
        return self._search_row(row)

    def get_search(self, search_id: str) -> dict:
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM exchange_searches WHERE id=?", (search_id,)).fetchone()
            search = self._search_row(row) if row else None
        if not search:
            raise ExchangeError("订单搜索任务不存在", 404)
        return search

    def next_search(self, worker_id: str) -> dict | None:
        worker_id = self._validate_worker_id(worker_id)
        now = _now()
        with self._txn() as conn:
            row = conn.execute(
                "SELECT * FROM exchange_searches WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE exchange_searches SET status='searching',worker_id=?,claimed_at=?,updated_at=? WHERE id=?",
                (worker_id, now, now, row["id"]),
            )
            return self._search_row(conn.execute("SELECT * FROM exchange_searches WHERE id=?", (row["id"],)).fetchone())

    def report_search(self, search_id: str, worker_id: str, result: dict) -> dict:
        worker_id = self._validate_worker_id(worker_id)
        if not isinstance(result, dict):
            raise ExchangeError("搜索结果格式不正确")
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM exchange_searches WHERE id=?", (search_id,)).fetchone()
            if not row:
                raise ExchangeError("订单搜索任务不存在", 404)
            if row["status"] != "searching" or row["worker_id"] != worker_id:
                raise ExchangeError("搜索任务状态或 Worker 不匹配", 409)
            error = str(result.get("error") or "")[:1000] or None
            status = "failed" if error else "done"
            conn.execute(
                "UPDATE exchange_searches SET status=?,result_json=?,error=?,updated_at=?,finished_at=? WHERE id=?",
                (status, _json(result), error, now, now, search_id),
            )
            return self._search_row(conn.execute("SELECT * FROM exchange_searches WHERE id=?", (search_id,)).fetchone())

    @staticmethod
    def _search_row(row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        return {
            "id": row["id"], "sku": row["sku"], "status": row["status"],
            "result": _loads(row["result_json"], None), "workerId": row["worker_id"],
            "error": row["error"], "attempts": ExchangeService._attempts(row),
            "claimedAt": row["claimed_at"] if "claimed_at" in row.keys() else None,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "finishedAt": row["finished_at"],
        }

    @staticmethod
    def validate_submission(payload: dict[str, Any]) -> tuple[dict, dict]:
        if not isinstance(payload, dict):
            raise ExchangeError("请求内容必须是对象")
        rules = payload.get("rules") or {}
        if not isinstance(rules, dict):
            raise ExchangeError("rules 格式不正确")
        strategy = str(rules.get("strategy") or "direct")
        if strategy != "direct":
            raise ExchangeError("第一版页面只开放 direct 一对一换货规则")
        replacements = rules.get("replacements") or []
        if not isinstance(replacements, list) or len(replacements) != 1:
            raise ExchangeError("direct 规则必须且只能包含一组源 SKU 和目标 SKU")
        replacement = replacements[0] if isinstance(replacements[0], dict) else {}
        source = str(replacement.get("from") or "").strip()
        target = str(replacement.get("to") or "").strip()
        if not source or not target:
            raise ExchangeError("源 SKU 和目标 SKU 都不能为空")
        if source == target:
            raise ExchangeError("源 SKU 与目标 SKU 不能相同")
        if len(source) > 128 or len(target) > 128:
            raise ExchangeError("SKU 长度不能超过 128 个字符")
        source_style = str(replacement.get("sourceStyle") or "").strip()[:128]
        target_style = str(replacement.get("targetStyle") or "").strip()[:128]
        # 延迟导入避免 policy 引用 ExchangeError 时形成模块初始化环。
        from .policy import enforce_replacement
        enforced = enforce_replacement(source, target, source_style, target_style)

        targets = payload.get("targets") or {}
        if not isinstance(targets, dict):
            raise ExchangeError("targets 格式不正确")
        raw_oids = targets.get("o_ids") or []
        if isinstance(raw_oids, str):
            raw_oids = re.split(r"[\s,，;；]+", raw_oids.strip())
        if not isinstance(raw_oids, list):
            raise ExchangeError("o_ids 必须是订单号列表")
        oids: list[str] = []
        for raw in raw_oids:
            oid = str(raw or "").strip()
            if not oid:
                continue
            if not OID_RE.fullmatch(oid):
                raise ExchangeError(f"订单号格式不正确：{oid}")
            if oid not in oids:
                oids.append(oid)
        limit = int(targets.get("limit") or 500)
        if limit < 1 or limit > 500:
            raise ExchangeError("单个任务的订单上限必须在 1–500 之间")
        if not oids:
            raise ExchangeError("第一版必须提供明确的 ERP 内部订单号 o_id")
        if len(oids) > limit:
            raise ExchangeError(f"订单数 {len(oids)} 超过任务上限 {limit}")
        clean_rules = {
            "name": str(rules.get("name") or f"{source} → {target}")[:120],
            "strategy": "direct",
            "replacements": [{
                "from": source, "to": target,
                **enforced,
            }],
            "forbidden_status_regex": "取消|退款|关闭|Cancelled|Delete|Merged",
        }
        clean_targets = {"o_ids": oids, "limit": limit}
        return clean_rules, clean_targets

    def create_job(
        self,
        payload: dict[str, Any],
        *,
        operator: str,
        idempotency_key: str | None = None,
    ) -> dict:
        rules, targets = self.validate_submission(payload)
        operator = str(operator or "web").strip()[:120] or "web"
        idem = str(idempotency_key or "").strip()[:200] or None
        now = _now()
        with self._txn() as conn:
            if idem:
                existing = conn.execute(
                    "SELECT * FROM exchange_jobs WHERE idempotency_key = ?", (idem,)
                ).fetchone()
                if existing:
                    return self._row(existing)
            # 不同订单的任务可以由多个 Worker 并行；同一订单禁止同时进入两个活动任务，
            # 否则两个 ERP 标签页可能基于不同 dry-run 结果重复改同一张单。
            requested_oids = set(targets["o_ids"])
            placeholders = ",".join("?" * len(ACTIVE_STATUSES))
            active_rows = conn.execute(
                f"SELECT id, targets_json FROM exchange_jobs WHERE status IN ({placeholders})",
                tuple(ACTIVE_STATUSES),
            ).fetchall()
            for active in active_rows:
                active_oids = set((_loads(active["targets_json"], {}) or {}).get("o_ids") or [])
                overlap = sorted(requested_oids & active_oids)
                if overlap:
                    shown = ", ".join(overlap[:10])
                    suffix = "…" if len(overlap) > 10 else ""
                    raise ExchangeError(
                        f"订单 {shown}{suffix} 已存在进行中的换货任务 {active['id']}，"
                        "请先完成或取消原任务",
                        409,
                    )
            job_id = secrets.token_hex(12)
            conn.execute(
                """INSERT INTO exchange_jobs
                   (id, idempotency_key, status, operator, rules_json, targets_json,
                    created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (job_id, idem, operator, _json(rules), _json(targets), now, now),
            )
            row = conn.execute("SELECT * FROM exchange_jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row(row)

    def list_jobs(self, *, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT * FROM exchange_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row(row) for row in rows]

    def get_job(self, job_id: str) -> dict:
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM exchange_jobs WHERE id = ?", (job_id,)).fetchone()
            job = self._row(row) if row else None
        if not job:
            raise ExchangeError("换货任务不存在", 404)
        return job

    def heartbeat(self, worker_id: str, payload: dict[str, Any]) -> dict:
        worker_id = self._validate_worker_id(worker_id)
        now = _now()
        page_url = str(payload.get("pageUrl") or payload.get("page_url") or "")[:1000]
        version = str(payload.get("version") or "")[:80]
        ready = 1 if payload.get("ready") else 0
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO exchange_workers
                   (worker_id, page_url, version, ready, detail_json, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(worker_id) DO UPDATE SET
                     page_url=excluded.page_url, version=excluded.version,
                     ready=excluded.ready, detail_json=excluded.detail_json,
                     last_seen_at=excluded.last_seen_at""",
                (worker_id, page_url, version, ready, _json(detail), now),
            )
        return {"ok": True, "workerId": worker_id, "serverTime": now}

    def status(self) -> dict:
        with self._txn() as conn:
            workers = conn.execute(
                "SELECT * FROM exchange_workers ORDER BY last_seen_at DESC LIMIT 20"
            ).fetchall()
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM exchange_jobs GROUP BY status"
            ).fetchall()
            worker_rows = [dict(row) for row in workers]
            job_counts = {row["status"]: row["n"] for row in counts}
        now = datetime.now(timezone.utc)
        result = []
        for row in worker_rows:
            try:
                seen = datetime.fromisoformat(row["last_seen_at"])
                online = (now - seen).total_seconds() <= 45
            except (TypeError, ValueError):
                online = False
            result.append({
                "workerId": row["worker_id"],
                "pageUrl": row["page_url"],
                "version": row["version"],
                "ready": bool(row["ready"]),
                "online": online,
                "detail": _loads(row["detail_json"], {}),
                "lastSeenAt": row["last_seen_at"],
            })
        return {
            "workers": result,
            "onlineWorkers": sum(1 for item in result if item["online"] and item["ready"]),
            "jobs": job_counts,
        }

    def next_job(self, worker_id: str) -> dict | None:
        """领取只读试算或已确认执行任务。

        执行任务一旦从 confirmed 变为 executing 就不会自动再次投递，防止 worker
        断线后对 ERP 重复写入。异常中断需要人工核对后另建任务。
        """
        worker_id = self._validate_worker_id(worker_id)
        now = _now()
        with self._txn() as conn:
            # 已人工确认的执行优先，避免前面不断新增 dry-run 时真实任务长期排不到。
            row = conn.execute(
                """SELECT * FROM exchange_jobs
                   WHERE status = 'confirmed' ORDER BY confirmed_at, created_at LIMIT 1"""
            ).fetchone()
            action = "execute"
            token = None
            if row:
                token = secrets.token_urlsafe(32)
                conn.execute(
                    """UPDATE exchange_jobs SET status='executing', worker_id=?,
                       execution_token_hash=?, execution_token_delivered=1,
                       started_at=?, claimed_at=?, updated_at=? WHERE id=?""",
                    (worker_id, _token_hash(token), now, now, now, row["id"]),
                )
            else:
                row = conn.execute(
                    """SELECT * FROM exchange_jobs
                       WHERE status = 'pending' ORDER BY created_at LIMIT 1"""
                ).fetchone()
                action = "plan"
                if row:
                    conn.execute(
                        "UPDATE exchange_jobs SET status='planning', worker_id=?, claimed_at=?, updated_at=? WHERE id=?",
                        (worker_id, now, now, row["id"]),
                    )
            if not row:
                return None
            fresh = conn.execute("SELECT * FROM exchange_jobs WHERE id = ?", (row["id"],)).fetchone()
            job = self._row(fresh)
            job["action"] = action
            if token:
                job["executionToken"] = token
            return job

    def report_plan(self, job_id: str, worker_id: str, plan: dict[str, Any]) -> dict:
        worker_id = self._validate_worker_id(worker_id)
        clean = self._validate_plan(job_id, plan)
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._must_row(conn, job_id)
            if row["status"] == "awaiting_confirm":
                if _loads(row["plan_json"], {}) == clean:
                    return self._row(row)
                raise ExchangeError("任务已经完成试算，不能覆盖确认依据", 409)
            if row["status"] != "planning":
                raise ExchangeError(f"任务状态 {row['status']} 不能回报试算", 409)
            if row["worker_id"] and row["worker_id"] != worker_id:
                raise ExchangeError("任务已由另一台 worker 领取", 409)
            conn.execute(
                """UPDATE exchange_jobs SET status='awaiting_confirm', plan_json=?,
                   worker_id=?, updated_at=? WHERE id=?""",
                (_json(clean), worker_id, now, job_id),
            )
            return self._row(self._must_row(conn, job_id))

    def confirm(self, job_id: str, operator: str) -> dict:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._must_row(conn, job_id)
            if row["status"] in {"confirmed", "executing", "done"}:
                return self._row(row)
            if row["status"] != "awaiting_confirm":
                raise ExchangeError("只有完成 dry-run 的任务才能确认执行", 409)
            plan = _loads(row["plan_json"], {})
            if int(plan.get("exchangeable") or 0) <= 0:
                raise ExchangeError("试算没有可换订单，不能确认执行", 409)
            if str(operator or "").strip() != row["operator"]:
                raise ExchangeError("必须由创建该任务的操作人确认", 403)
            conn.execute(
                """UPDATE exchange_jobs SET status='confirmed', confirmed_at=?,
                   updated_at=? WHERE id=?""",
                (now, now, job_id),
            )
            return self._row(self._must_row(conn, job_id))

    def cancel(self, job_id: str, operator: str) -> dict:
        now = _now()
        operator = str(operator or "").strip()
        if not operator:
            raise ExchangeError("取消必须填写操作人姓名", 403)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._must_row(conn, job_id)
            if row["status"] == "cancelled":
                return self._row(row)
            if row["status"] not in {"pending", "planning", "awaiting_confirm", "confirmed"}:
                raise ExchangeError("执行中或已结束的任务不能取消", 409)
            if operator != row["operator"]:
                raise ExchangeError("必须由创建该任务的操作人取消", 403)
            conn.execute(
                "UPDATE exchange_jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?",
                (now, now, job_id),
            )
            return self._row(self._must_row(conn, job_id))

    def report_progress(
        self, job_id: str, worker_id: str, execution_token: str, event: dict[str, Any]
    ) -> dict:
        worker_id = self._validate_worker_id(worker_id)
        if not isinstance(event, dict):
            raise ExchangeError("progress 必须是对象")
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._must_executing(conn, job_id, worker_id, execution_token, allow_stuck=True)
            progress = _loads(row["progress_json"], [])
            progress.append({**event, "reportedAt": now})
            progress = progress[-1000:]
            conn.execute(
                "UPDATE exchange_jobs SET progress_json=?, updated_at=? WHERE id=?",
                (_json(progress), now, job_id),
            )
        return {"ok": True, "progressCount": len(progress)}

    def report_result(
        self, job_id: str, worker_id: str, execution_token: str, result: dict[str, Any]
    ) -> dict:
        worker_id = self._validate_worker_id(worker_id)
        if not isinstance(result, dict):
            raise ExchangeError("result 必须是对象")
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._must_row(conn, job_id)
            if row["status"] in {"done", "failed"}:
                return self._row(row)
            self._must_executing(conn, job_id, worker_id, execution_token, allow_stuck=True)
            failed = result.get("failed") or result.get("fail") or []
            succeeded = result.get("succeeded") or result.get("success") or []
            status = "done" if succeeded and not failed else "failed"
            error = str(result.get("error") or "")[:1000] or None
            conn.execute(
                """UPDATE exchange_jobs SET status=?, result_json=?, error=?,
                   execution_token_hash=NULL, finished_at=?, updated_at=? WHERE id=?""",
                (status, _json(result), error, now, now, job_id),
            )
            return self._row(self._must_row(conn, job_id))

    def _validate_plan(self, job_id: str, plan: dict[str, Any]) -> dict:
        if not isinstance(plan, dict) or not isinstance(plan.get("plans"), list):
            raise ExchangeError("试算结果必须包含 plans 列表")
        with self._connect() as conn:
            job = self._row(self._must_row(conn, job_id))
        allowed = set(job["targets"]["o_ids"])
        expected_replacement = job["rules"]["replacements"][0]
        expected_source = str(expected_replacement["from"])
        expected_target = str(expected_replacement["to"])
        seen: set[str] = set()
        clean_plans = []
        exchangeable = 0
        for raw in plan["plans"]:
            if not isinstance(raw, dict):
                raise ExchangeError("试算明细格式不正确")
            oid = str(raw.get("o_id") or "")
            if oid not in allowed or oid in seen:
                raise ExchangeError(f"试算包含非目标或重复订单号：{oid}")
            seen.add(oid)
            ok = bool(raw.get("ok"))
            if ok:
                if raw.get("mode") != "ChangeItem":
                    raise ExchangeError(f"订单 {oid} 的执行模式无效，direct 规则只能使用 ChangeItem")
                if (str(raw.get("src_sku_id") or "") != expected_source
                        or str(raw.get("new_sku_id") or "") != expected_target):
                    raise ExchangeError(f"订单 {oid} 的试算 SKU 与任务规则不一致")
                exchangeable += 1
            clean_plans.append(raw)
        if seen != allowed:
            missing = sorted(allowed - seen)
            raise ExchangeError("试算没有覆盖全部目标订单：" + ", ".join(missing))
        return {
            "total": len(clean_plans),
            "exchangeable": exchangeable,
            "skipped": len(clean_plans) - exchangeable,
            "plans": clean_plans,
        }

    @staticmethod
    def _validate_worker_id(worker_id: str) -> str:
        worker_id = str(worker_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", worker_id):
            raise ExchangeError("worker_id 格式不正确")
        return worker_id

    @staticmethod
    def _must_row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM exchange_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ExchangeError("换货任务不存在", 404)
        return row

    def _must_executing(
        self, conn: sqlite3.Connection, job_id: str, worker_id: str, token: str,
        *, allow_stuck: bool = False,
    ) -> sqlite3.Row:
        row = self._must_row(conn, job_id)
        allowed = {"executing", "stuck"} if allow_stuck else {"executing"}
        if row["status"] not in allowed:
            raise ExchangeError("任务当前不在执行状态", 409)
        if row["worker_id"] != worker_id:
            raise ExchangeError("执行 worker 不匹配", 403)
        expected = str(row["execution_token_hash"] or "")
        supplied = _token_hash(str(token or ""))
        if not expected or not secrets.compare_digest(expected, supplied):
            raise ExchangeError("一次性执行凭证无效", 403)
        return row

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        return {
            "id": row["id"],
            "status": row["status"],
            "operator": row["operator"],
            "rules": _loads(row["rules_json"], {}),
            "targets": _loads(row["targets_json"], {}),
            "plan": _loads(row["plan_json"], None),
            "progress": _loads(row["progress_json"], []),
            "result": _loads(row["result_json"], None),
            "workerId": row["worker_id"],
            "error": row["error"],
            "attempts": ExchangeService._attempts(row),
            "claimedAt": row["claimed_at"] if "claimed_at" in row.keys() else None,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "confirmedAt": row["confirmed_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
        }
