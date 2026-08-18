# -*- coding: utf-8 -*-
"""审计：每轮对话、每次工具调用、每次预测建议都要留痕。

工具入参和结果摘要都截断后落库，避免把整份合同或整批订单塞进审计表；
需要复现的东西（模型版本、输入快照）单独进 `forecast_runs`。
"""
from __future__ import annotations

import hashlib
import secrets

from .store import AgentStore, dumps, loads, now


SUMMARY_LIMIT = 2000
JSON_STORE_LIMIT = 100_000


def store_json(value, *, limit: int = JSON_STORE_LIMIT) -> str:
    """过长时改成仍合法的摘要对象，禁止截断半截 JSON。"""
    payload = value if value is not None else {}
    text = dumps(payload)
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if isinstance(payload, dict):
        return dumps({"truncated": True, "sha256": digest, "keys": list(payload)[:40]})
    if isinstance(payload, list):
        return dumps({"truncated": True, "sha256": digest, "length": len(payload)})
    return dumps({"truncated": True, "sha256": digest})


def summarize(value) -> str:
    """把工具结果压成一行摘要，长内容截断。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = dumps(value)
    text = text.replace("\n", " ")
    return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT] + "…"


class AuditLog:
    def __init__(self, store: AgentStore):
        self.store = store

    def start_run(self, *, session_id: str, channel: str, operator: str, request: str,
                  model: str = "", user_id: str = "") -> str:
        run_id = secrets.token_hex(12)
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO agent_runs
                   (id, session_id, channel, operator, user_id, request, status, model, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                (run_id, session_id, channel, str(operator or "")[:120],
                 str(user_id or "")[:80], str(request or "")[:4000], model, now()),
            )
        return run_id

    def finish_run(self, run_id: str, *, status: str, reply: str = "", steps: int = 0,
                   duration_ms: int = 0, error: str | None = None,
                   prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        with self.store.write() as conn:
            conn.execute(
                """UPDATE agent_runs SET status=?, reply=?, steps=?, duration_ms=?,
                   error=?, finished_at=?, prompt_tokens=?, completion_tokens=? WHERE id=?""",
                (status, str(reply or "")[:8000], int(steps), int(duration_ms),
                 str(error)[:1000] if error else None, now(),
                 int(prompt_tokens or 0), int(completion_tokens or 0), run_id),
            )

    def record_tool(self, *, tool: str, risk: str = "L0", status: str = "ok",
                    arguments: dict | None = None, result=None, error: str | None = None,
                    duration_ms: int = 0, run_id: str | None = None, session_id: str | None = None,
                    pending_action_id: str | None = None, operator: str = "",
                    user_id: str = "", channel: str = "") -> None:
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO tool_executions
                   (run_id, session_id, pending_action_id, tool, risk, operator, user_id, channel,
                    arguments_json, status, result_summary, error, duration_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, session_id, pending_action_id, tool, risk, str(operator or "")[:120],
                 str(user_id or "")[:80], channel, store_json(arguments or {}), status,
                 summarize(result), str(error)[:1000] if error else None, int(duration_ms), now()),
            )

    def record_forecast(self, *, model_name: str, model_version: str, keys: list,
                        inputs: dict, output: dict, operator: str = "", user_id: str = "",
                        session_id: str | None = None, run_id: str | None = None,
                        pending_action_id: str | None = None) -> str:
        """记录一次订货建议引用的模型版本与输入快照，事后可复现。"""
        forecast_id = secrets.token_hex(12)
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO forecast_runs
                   (id, session_id, run_id, pending_action_id, operator, user_id, model_name,
                    model_version, keys_json, input_json, output_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (forecast_id, session_id, run_id, pending_action_id, str(operator or "")[:120],
                 str(user_id or "")[:80], model_name, model_version, dumps(list(keys)),
                 store_json(inputs), store_json(output), now()),
            )
        return forecast_id

    def record_delivery(self, *, channel: str, target: str, kind: str, status: str,
                        detail: dict | None = None, idempotency_key: str | None = None,
                        error: str | None = None) -> bool:
        """记录一次外发通知；同一 `idempotency_key` 只允许登记一次。

        钉钉入站消息用这个方法立刻占键去重。催办出站不要在发送前用业务键调用它，
        改走 `has_successful_delivery` / 成功后再写入，否则失败会把当天锁死。
        """
        with self.store.write(immediate=True) as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id FROM notification_deliveries WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return False
            conn.execute(
                """INSERT INTO notification_deliveries
                   (channel, target, kind, idempotency_key, status, detail_json, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, str(target or "")[:200], kind, idempotency_key, status,
                 store_json(detail or {}, limit=20000), str(error)[:1000] if error else None, now()),
            )
        return True

    def has_successful_delivery(self, idempotency_key: str) -> bool:
        """只认带该键且 status=sent 的记录；sending/failed 不算已送达。"""
        if not idempotency_key:
            return False
        with self.store.read() as conn:
            row = conn.execute(
                """SELECT id FROM notification_deliveries
                   WHERE idempotency_key = ? AND status = 'sent'""",
                (idempotency_key,),
            ).fetchone()
        return row is not None

    def release_unsuccessful_key(self, idempotency_key: str) -> int:
        """清掉占用该键但未发送成功的记录，让失败后的重试能再写 sent。"""
        if not idempotency_key:
            return 0
        with self.store.write(immediate=True) as conn:
            cursor = conn.execute(
                """UPDATE notification_deliveries
                   SET idempotency_key = NULL
                   WHERE idempotency_key = ? AND status != 'sent'""",
                (idempotency_key,),
            )
            return cursor.rowcount or 0

    def next_attempt_key(self, base_key: str) -> str:
        rows = self.list_deliveries(key_prefix=f"{base_key}-attempt-")
        return f"{base_key}-attempt-{len(rows) + 1}"

    def list_deliveries(self, *, key_prefix: str = "", kind: str = "",
                        status: str = "", limit: int = 50) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if key_prefix:
            clauses.append("idempotency_key LIKE ?")
            params.append(f"{key_prefix}%")
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self.store.read() as conn:
            rows = conn.execute(
                f"""SELECT * FROM notification_deliveries {where}
                    ORDER BY created_at ASC, id ASC LIMIT ?""",
                params,
            ).fetchall()
        return [{
            "id": row["id"], "channel": row["channel"], "target": row["target"],
            "kind": row["kind"], "idempotencyKey": row["idempotency_key"],
            "status": row["status"], "detail": loads(row["detail_json"], {}),
            "error": row["error"], "createdAt": row["created_at"],
        } for row in rows]

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [{
            "id": row["id"], "sessionId": row["session_id"], "channel": row["channel"],
            "operator": row["operator"],
            "userId": row["user_id"] if "user_id" in row.keys() else "",
            "request": row["request"], "reply": row["reply"],
            "status": row["status"], "steps": row["steps"], "model": row["model"],
            "error": row["error"], "startedAt": row["started_at"],
            "durationMs": row["duration_ms"],
        } for row in rows]

    def recent_tools(self, limit: int = 50) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_executions ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [{
            "tool": row["tool"], "risk": row["risk"], "status": row["status"],
            "operator": row["operator"],
            "userId": row["user_id"] if "user_id" in row.keys() else "",
            "channel": row["channel"],
            "arguments": loads(row["arguments_json"], {}), "resultSummary": row["result_summary"],
            "error": row["error"], "durationMs": row["duration_ms"], "createdAt": row["created_at"],
        } for row in rows]
