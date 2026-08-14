# -*- coding: utf-8 -*-
"""品控台账：登记 / 关闭 / 撤销 / 查询。只写 Agent SQLite，不碰镜像库。"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta
from pathlib import Path

from ..agent.store import AgentStore, now
from ..business_time import business_today
from .parse import parse_quality_command, parse_quality_fields


def load_supplier_names(root: Path) -> set[str]:
    path = Path(root) / "config" / "suppliers.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(key).strip() for key in payload if str(key).strip()}


class QualityError(ValueError):
    """可回给员工的品控错误。"""


class QualityLedger:
    def __init__(self, store: AgentStore, *, suppliers: set[str] | None = None,
                 lookup_po=None, enabled: bool = True):
        self.store = store
        self.suppliers = set(suppliers or ())
        self.lookup_po = lookup_po
        self.enabled = bool(enabled)

    def handle_text(self, text: str, *, reporter: str = "", reporter_user_id: str = "",
                    channel: str = "dingtalk", conversation_id: str = "",
                    message_id: str | None = None) -> str | None:
        """处理一条品控指令；不是品控指令返回 None。"""
        if not self.enabled:
            return None
        command = parse_quality_command(text)
        if command is None:
            return None
        action = command["action"]
        if action == "record":
            return self._handle_record(
                command["raw"], reporter=reporter, reporter_user_id=reporter_user_id,
                channel=channel, conversation_id=conversation_id, message_id=message_id,
                raw_text=text,
            )
        if action == "resolve":
            item = self.resolve(command["issueId"], command.get("resolution") or "")
            return f"已关闭品控 #{item['id']}。{item.get('resolution') or ''}".strip()
        if action == "cancel":
            item = self.cancel(command["issueId"])
            return f"已撤销品控 #{item['id']}。"
        return self.format_query(command.get("query") or "今天")

    def record(self, *, description: str, supplier: str = "", po_id: str = "", sku: str = "",
               severity: str = "", reporter: str = "", reporter_user_id: str = "",
               channel: str = "web", conversation_id: str = "", message_id: str | None = None,
               raw_text: str = "", run_id: str | None = None) -> dict:
        description = str(description or "").strip()
        if not description:
            raise QualityError("品控问题描述不能为空")
        if message_id:
            existing = self.get_by_message_id(message_id)
            if existing:
                return existing
        issue_id = secrets.token_hex(3)
        stamp = now()
        today = business_today().isoformat()
        with self.store.write() as conn:
            for _ in range(5):
                try:
                    conn.execute(
                        """INSERT INTO quality_issues
                           (id, report_date, source_channel, conversation_id, message_id,
                            reporter, reporter_user_id, supplier, po_id, sku, severity,
                            description, raw_text, status, resolution, run_id, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', '', ?, ?, ?)""",
                        (issue_id, today, channel, conversation_id or "", message_id,
                         reporter[:120], reporter_user_id[:80], str(supplier or "")[:80],
                         str(po_id or "")[:32], str(sku or "")[:80], str(severity or "")[:20],
                         description[:2000], str(raw_text or "")[:2000], run_id, stamp, stamp),
                    )
                    break
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    detail = str(exc).lower()
                    if message_id and "message_id" in detail:
                        existing = self.get_by_message_id(message_id)
                        if existing:
                            return existing
                    if "quality_issues.id" in detail:
                        issue_id = secrets.token_hex(3)
                        continue
                    raise QualityError("品控登记冲突，请重试") from exc
            else:
                raise QualityError("无法分配品控编号，请重试")
        return self.get(issue_id)

    def get(self, issue_id: str) -> dict:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM quality_issues WHERE id = ?", (issue_id,)).fetchone()
        if not row:
            raise QualityError(f"没有编号为 {issue_id} 的品控记录")
        return self._row(row)

    def get_by_message_id(self, message_id: str) -> dict:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM quality_issues WHERE message_id = ?", (message_id,),
            ).fetchone()
        return self._row(row) if row else {}

    def cancel(self, issue_id: str) -> dict:
        return self._set_status(issue_id, "cancelled")

    def resolve(self, issue_id: str, resolution: str = "") -> dict:
        return self._set_status(issue_id, "resolved", resolution=resolution)

    def query(self, *, query: str = "今天", status: str = "") -> list[dict]:
        today = business_today()
        text = str(query or "今天").strip()
        sql = "SELECT * FROM quality_issues WHERE 1=1"
        params: list = []
        if text in ("未关闭", "open"):
            sql += " AND status = 'open'"
        elif text in ("今天", "今日"):
            sql += " AND report_date = ?"
            params.append(today.isoformat())
        elif text in ("本周",):
            start = (today - timedelta(days=today.weekday())).isoformat()
            sql += " AND report_date >= ?"
            params.append(start)
        elif text:
            sql += " AND (supplier = ? OR reporter = ? OR po_id = ?)"
            params.extend([text, text, text])
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC"
        with self.store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def list_for_report(self, report_date: str) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT * FROM quality_issues
                   WHERE report_date = ? AND status <> 'cancelled'
                   ORDER BY created_at, id""",
                (report_date,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def open_count(self, *, before: str = "") -> int:
        sql = "SELECT COUNT(*) AS n FROM quality_issues WHERE status = 'open'"
        params: list = []
        if before:
            sql += " AND report_date < ?"
            params.append(before)
        with self.store.read() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)

    def summary(self, issues: list[dict]) -> dict:
        by_supplier: dict[str, int] = {}
        open_count = 0
        for item in issues:
            key = item["supplier"] or "未填供应商"
            by_supplier[key] = by_supplier.get(key, 0) + 1
            if item["status"] == "open":
                open_count += 1
        return {
            "total": len(issues),
            "open": open_count,
            "bySupplier": by_supplier,
        }

    def format_query(self, query: str) -> str:
        issues = self.query(query=query)
        if not issues:
            return f"品控查询「{query}」没有记录。"
        lines = [f"品控查询「{query}」共 {len(issues)} 条："]
        for item in issues[:20]:
            bits = [f"#{item['id']}", item["status"], item["supplier"] or "未填供应商"]
            if item["poId"]:
                bits.append(item["poId"])
            bits.append(item["description"])
            lines.append(" · ".join(bits))
        if len(issues) > 20:
            lines.append(f"另有 {len(issues) - 20} 条未列出。")
        return "\n".join(lines)

    def _handle_record(self, raw: str, **kwargs) -> str:
        fields = parse_quality_fields(raw, suppliers=self.suppliers, lookup_po=self.lookup_po)
        if not fields["description"]:
            raise QualityError("品控问题描述不能为空。用法：品控 佰特 604264 鞋垫开胶 3 双")
        item = self.record(
            description=fields["description"], supplier=fields["supplier"],
            po_id=fields["po_id"], sku=fields["sku"], raw_text=kwargs.get("raw_text") or raw,
            reporter=kwargs.get("reporter") or "", reporter_user_id=kwargs.get("reporter_user_id") or "",
            channel=kwargs.get("channel") or "dingtalk",
            conversation_id=kwargs.get("conversation_id") or "",
            message_id=kwargs.get("message_id"),
        )
        bits = [f"已登记品控 #{item['id']}："]
        bits.append(f"供应商={item['supplier'] or '未解析'}")
        bits.append(f"单号={item['poId'] or '未解析'}")
        if item["sku"]:
            bits.append(f"SKU={item['sku']}")
        bits.append(f"描述={item['description']}")
        return " ".join(bits) + f"。有误可「撤销品控 {item['id']}」。"

    def _set_status(self, issue_id: str, status: str, *, resolution: str = "") -> dict:
        item = self.get(issue_id)
        if item["status"] == status:
            return item
        if item["status"] == "cancelled" and status == "resolved":
            raise QualityError(f"品控 #{issue_id} 已撤销，不能再关闭")
        with self.store.write() as conn:
            conn.execute(
                """UPDATE quality_issues SET status=?, resolution=?, updated_at=? WHERE id=?""",
                (status, str(resolution or item["resolution"] or "")[:500], now(), issue_id),
            )
        return self.get(issue_id)

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "reportDate": row["report_date"],
            "channel": row["source_channel"],
            "conversationId": row["conversation_id"],
            "messageId": row["message_id"],
            "reporter": row["reporter"],
            "reporterUserId": row["reporter_user_id"],
            "supplier": row["supplier"],
            "poId": row["po_id"],
            "sku": row["sku"],
            "severity": row["severity"],
            "description": row["description"],
            "rawText": row["raw_text"],
            "status": row["status"],
            "resolution": row["resolution"],
            "runId": row["run_id"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
