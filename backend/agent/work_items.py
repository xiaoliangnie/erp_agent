# -*- coding: utf-8 -*-
"""Work Item：把待确认动作、换货任务、品控待办收成一张可在 /workbench 处理的表。

`pending_actions` 仍是确认状态机的唯一事实来源。这里只做投影，确认/取消仍走原接口。
"""
from __future__ import annotations

import secrets

from .store import AgentStore, dumps, loads, now
from ..staff_names import VIEWER_WRITE_DENIED, WEB_OPERATOR_UNBOUND, buyer_names_equivalent


OPEN_STATUSES = ("open", "in_progress", "failed")
ACTION_STATUS = {
    "pending": "open",
    "confirmed": "in_progress",
    "executed": "resolved",
    "cancelled": "cancelled",
    "expired": "expired",
    "failed": "failed",
}
EXCHANGE_STATUS = {
    "pending": "open",
    "planning": "open",
    "awaiting_confirm": "open",
    "confirmed": "in_progress",
    "executing": "in_progress",
    "done": "resolved",
    "cancelled": "cancelled",
    "failed": "failed",
    "stuck": "failed",
}
QUALITY_STATUS = {
    "open": "open",
    "resolved": "resolved",
    "cancelled": "cancelled",
}


class WorkItems:
    def __init__(self, store: AgentStore):
        self.store = store

    def upsert_action(self, action: dict) -> dict:
        preview = action.get("preview") if isinstance(action.get("preview"), dict) else {}
        return self._upsert(
            kind="pending_action",
            source_table="pending_actions",
            source_id=str(action["id"]),
            status=ACTION_STATUS.get(str(action.get("status") or ""), "open"),
            title=str(action.get("title") or action.get("tool") or "待确认动作")[:200],
            operator=str(action.get("operator") or "")[:120],
            user_id=str(action.get("userId") or "")[:80],
            tool=str(action.get("tool") or ""),
            risk=str(action.get("risk") or ""),
            summary={
                "actionId": action.get("id"),
                "status": action.get("status"),
                "expiresAt": action.get("expiresAt"),
                "preview": preview,
                "error": action.get("error") or "",
            },
        )

    def upsert_exchange(self, job: dict) -> dict:
        rules = job.get("rules") if isinstance(job.get("rules"), dict) else {}
        targets = job.get("targets") if isinstance(job.get("targets"), dict) else {}
        replacements = rules.get("replacements") or []
        first = replacements[0] if replacements and isinstance(replacements[0], dict) else {}
        source = str(first.get("from") or "")
        target = str(first.get("to") or "")
        oids = list(targets.get("o_ids") or [])
        title = str(rules.get("name") or f"换货 {source} → {target}").strip() or "换货任务"
        return self._upsert(
            kind="exchange_job",
            source_table="exchange_jobs",
            source_id=str(job["id"]),
            status=EXCHANGE_STATUS.get(str(job.get("status") or ""), "open"),
            title=title[:200],
            operator=str(job.get("operator") or "")[:120],
            user_id="",
            tool="submit_exchange_dry_run",
            risk="L1",
            summary={
                "jobId": job.get("id"),
                "status": job.get("status"),
                "sourceSku": source,
                "targetSku": target,
                "orderCount": len(oids),
                "href": "/exchange",
                "error": job.get("error") or "",
            },
        )

    def upsert_quality(self, issue: dict) -> dict:
        title = str(issue.get("description") or "品控待办")[:200]
        return self._upsert(
            kind="quality_issue",
            source_table="quality_issues",
            source_id=str(issue["id"]),
            status=QUALITY_STATUS.get(str(issue.get("status") or ""), "open"),
            title=title,
            operator=str(issue.get("reporter") or "")[:120],
            user_id=str(issue.get("reporterUserId") or "")[:80],
            tool="record_quality_issue",
            risk="L1",
            summary={
                "issueId": issue.get("id"),
                "status": issue.get("status"),
                "supplier": issue.get("supplier") or "",
                "poId": issue.get("poId") or "",
            },
        )

    def decide_quality(self, ledger, *, issue_id: str, decision: str, operator: str,
                       resolution: str = "", directory=None) -> dict:
        """工作台关闭/撤销品控。工作台点击即确认，仍校验绑定和发起人。"""
        from .actions import ActionError
        if ledger is None or not getattr(ledger, "enabled", False):
            raise ActionError("品控台账未启用", 503)
        if directory is not None and not directory.known_operator(operator):
            raise ActionError(WEB_OPERATOR_UNBOUND, 403)
        binding = directory.find_binding(operator=operator) if directory is not None else None
        if binding and str(binding.get("role") or "") == "viewer":
            raise ActionError(VIEWER_WRITE_DENIED, 403)
        item = ledger.get(str(issue_id or "").strip())
        reporter = str(item.get("reporter") or "").strip()
        if reporter and operator and not buyer_names_equivalent(operator, reporter):
            raise ActionError("只能处理自己登记的品控。", 403)
        if decision == "resolve":
            item = ledger.resolve(item["id"], str(resolution or "").strip())
        elif decision == "cancel":
            item = ledger.cancel(item["id"])
        else:
            raise ActionError("品控待办只支持关闭或撤销", 400)
        return self.upsert_quality(item)

    def get_by_source(self, source_table: str, source_id: str) -> dict | None:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM work_items WHERE source_table = ? AND source_id = ?",
                (source_table, source_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(
        self,
        *,
        operator: str = "",
        statuses: tuple[str, ...] = OPEN_STATUSES,
        limit: int = 50,
    ) -> list[dict]:
        clauses = []
        params: list = []
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        if operator:
            clauses.append("operator = ?")
            params.append(operator[:120])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        with self.store.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM work_items {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def refresh_pending(self, actions) -> int:
        """把 pending_actions 的当前状态投影过来（过期批量更新后用）。"""
        count = 0
        for action in actions:
            self.upsert_action(action)
            count += 1
        return count

    def _upsert(
        self,
        *,
        kind: str,
        source_table: str,
        source_id: str,
        status: str,
        title: str,
        operator: str,
        user_id: str,
        tool: str,
        risk: str,
        summary: dict,
    ) -> dict:
        stamp = now()
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT id FROM work_items WHERE source_table = ? AND source_id = ?",
                (source_table, source_id),
            ).fetchone()
            item_id = existing["id"] if existing else secrets.token_hex(12)
            if existing:
                conn.execute(
                    """UPDATE work_items
                       SET kind=?, status=?, title=?, operator=?, user_id=?, tool=?, risk=?,
                           summary_json=?, updated_at=?
                       WHERE id=?""",
                    (kind, status, title, operator, user_id, tool, risk,
                     dumps(summary or {}), stamp, item_id),
                )
            else:
                conn.execute(
                    """INSERT INTO work_items
                       (id, kind, source_table, source_id, status, title, operator, user_id,
                        tool, risk, summary_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (item_id, kind, source_table, source_id, status, title, operator,
                     user_id, tool, risk, dumps(summary or {}), stamp, stamp),
                )
        return self.get_by_source(source_table, source_id) or {}

    def assemble(
        self,
        *,
        actions,
        exchange=None,
        quality=None,
        jobs=None,
        outbox=None,
        operator: str = "",
        limit: int = 50,
    ) -> dict:
        """刷新投影后给 /workbench 用。确认/取消仍走 pending_actions。"""
        if actions is not None:
            actions.expire_due()
        if exchange is not None:
            try:
                for job in exchange.list_jobs(limit=100):
                    self.upsert_exchange(job)
            except Exception:
                pass
        if quality is not None:
            try:
                for issue in quality.query(query="未关闭"):
                    self.upsert_quality(issue)
            except Exception:
                pass
        wanted = max(1, min(int(limit), 200))
        items = self.list(limit=wanted * 3 if operator else wanted)
        if operator:
            items = [
                item for item in items
                if not item["operator"] or buyer_names_equivalent(operator, item["operator"])
            ][:wanted]
        enriched = []
        for item in items:
            action = None
            if item["kind"] == "pending_action" and actions is not None:
                try:
                    action = actions.get(item["sourceId"])
                except Exception:
                    action = None
            enriched.append({**item, "action": action})
        return {
            "items": enriched,
            "jobs": jobs.list(limit=20) if jobs is not None else [],
            "outbox": {
                "pending": outbox.pending_count() if outbox is not None else 0,
                "recent": outbox.list(limit=10) if outbox is not None else [],
            },
        }

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "sourceTable": row["source_table"],
            "sourceId": row["source_id"],
            "status": row["status"],
            "title": row["title"],
            "operator": row["operator"],
            "userId": row["user_id"],
            "tool": row["tool"],
            "risk": row["risk"],
            "summary": loads(row["summary_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "actionId": row["source_id"] if row["kind"] == "pending_action" else "",
        }
