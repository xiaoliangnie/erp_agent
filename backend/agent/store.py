# -*- coding: utf-8 -*-
"""Agent 业务库的连接与表结构。

架构方案 §10 的目标形态是同一 MySQL 实例下的独立 schema。第一阶段沿用换货任务
队列已经验证过的做法——本地 SQLite，表名、字段名和状态机与方案一致，迁到 MySQL
只需要换掉这一层，`sessions.py` / `actions.py` / `audit.py` 的调用面不变。
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
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    run_id TEXT,
    pending_action_id TEXT,
    operator TEXT NOT NULL DEFAULT '',
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
"""


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


class AgentStore:
    """Agent 业务库的唯一写入点，串行化写事务。"""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.write() as conn:
            conn.executescript(SCHEMA)

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
