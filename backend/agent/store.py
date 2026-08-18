# -*- coding: utf-8 -*-
"""Agent 业务库的连接与表结构。

架构方案 §10 的目标形态是同一 MySQL 实例下的独立 schema。第一阶段沿用换货任务
队列已经验证过的做法——本地 SQLite，表名、字段名和状态机与方案一致。迁到 MySQL
只需要换掉这一层，`sessions.py` / `actions.py` / `audit.py` 的调用面不变。
**换库是 P2，等迁到专用机器之后再做。**
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    session_key TEXT NOT NULL,
    operator TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel, session_key)
);
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    run_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    name TEXT,
    tool_call_id TEXT,
    tool_calls_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, id);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    channel TEXT NOT NULL,
    operator TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    request TEXT NOT NULL DEFAULT '',
    reply TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS tool_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    session_id TEXT,
    pending_action_id TEXT,
    tool TEXT NOT NULL,
    risk TEXT NOT NULL DEFAULT 'L0',
    operator TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    arguments_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    result_summary TEXT NOT NULL DEFAULT '',
    error TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_executions_created ON tool_executions(created_at);
CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    run_id TEXT,
    channel TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    risk TEXT NOT NULL DEFAULT 'L1',
    title TEXT NOT NULL DEFAULT '',
    arguments_json TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT,
    executed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status, expires_at);
CREATE TABLE IF NOT EXISTS staff_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_name TEXT NOT NULL UNIQUE,
    dingtalk_user_id TEXT NOT NULL DEFAULT '',
    mobile TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'operator',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    run_id TEXT,
    pending_action_id TEXT,
    operator TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    keys_json TEXT NOT NULL DEFAULT '[]',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_issues (
    id TEXT PRIMARY KEY,
    report_date TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    message_id TEXT UNIQUE,
    reporter TEXT NOT NULL DEFAULT '',
    reporter_user_id TEXT NOT NULL DEFAULT '',
    supplier TEXT NOT NULL DEFAULT '',
    po_id TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_issues_date ON quality_issues(report_date, status);
CREATE TABLE IF NOT EXISTS agent_session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    upto_message_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'preference',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_memories ON operator_memories(operator, active, updated_at);
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_table, source_id)
);
CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status, updated_at);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, available_at, created_at);
CREATE TABLE IF NOT EXISTS outbox_messages (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox_messages(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    real_name TEXT NOT NULL DEFAULT '',
    nickname TEXT NOT NULL DEFAULT '',
    canonical_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    erp_buyer_aliases_json TEXT NOT NULL DEFAULT '[]',
    dingtalk_userid TEXT NOT NULL DEFAULT '',
    dingtalk_unionid TEXT NOT NULL DEFAULT '',
    mobile TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT 'purchase_order_seed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_canonical ON users(canonical_name);
CREATE INDEX IF NOT EXISTS idx_users_dingtalk ON users(dingtalk_userid);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE TABLE IF NOT EXISTS bind_requests (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    names_json TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bind_requests_status ON bind_requests(status, requested_at);
CREATE TABLE IF NOT EXISTS web_bind_codes (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL,
    buyer_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_web_bind_codes_hash ON web_bind_codes(code_hash);
CREATE TABLE IF NOT EXISTS web_sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    buyer_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_hash ON web_sessions(token_hash);
"""

COLUMN_MIGRATIONS = (
    ("agent_sessions", "epoch", "INTEGER NOT NULL DEFAULT 0"),
    ("agent_messages", "epoch", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_actions", "actor_id", "TEXT NOT NULL DEFAULT ''"),
    ("pending_actions", "confirmed_by", "TEXT NOT NULL DEFAULT ''"),
    ("agent_runs", "prompt_tokens", "INTEGER"),
    ("agent_runs", "completion_tokens", "INTEGER"),
    ("agent_runs", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("tool_executions", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("pending_actions", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("forecast_runs", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("staff_bindings", "role", "TEXT NOT NULL DEFAULT 'operator'"),
    ("agent_sessions", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("operator_memories", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("staff_bindings", "user_id", "TEXT NOT NULL DEFAULT ''"),
    ("bind_requests", "conversation_id", "TEXT NOT NULL DEFAULT ''"),
    ("outbox_messages", "lease_token", "TEXT NOT NULL DEFAULT ''"),
    ("outbox_messages", "lease_until", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "lease_token", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "lease_until", "TEXT NOT NULL DEFAULT ''"),
)

INDEX_MIGRATIONS = (
    "CREATE INDEX IF NOT EXISTS idx_operator_memories_user ON operator_memories(user_id, active, updated_at)",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def later(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _migrate_columns(conn) -> None:
    """SQLite 的 executescript 不会给已有表补列。"""
    for table, column, decl in COLUMN_MIGRATIONS:
        names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in names:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


class AgentStore:
    """Agent 业务库的唯一写入点，串行化写事务。"""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.write() as conn:
            conn.executescript(SCHEMA)
            _migrate_columns(conn)
            for statement in INDEX_MIGRATIONS:
                conn.execute(statement)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def read(self):
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self, *, immediate: bool = False):
        with self.lock:
            conn = self._open()
            try:
                if immediate:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
