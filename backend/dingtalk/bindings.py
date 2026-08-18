# -*- coding: utf-8 -*-
"""钉钉绑定申请。员工申请，管理员私聊同意后才写入 staff_bindings。"""
from __future__ import annotations

import re
import secrets

from ..agent.store import dumps, loads, now
from ..agent.users import is_confirmed_admin_name, link_binding_to_user
from ..staff_names import parse_buyer_names


TOKEN_SPLIT = re.compile(r"[,，、\s]+")
PRIVATE_TYPES = {"1", "private", "dm"}


def is_private_conversation(conversation_type: str) -> bool:
    return str(conversation_type or "").strip().lower() in PRIVATE_TYPES


def admin_user_ids(directory, extra_ids=()) -> list[str]:
    """能收绑定审批私信的管理员钉钉 userId。只通知已绑定的管理员。"""
    del extra_ids
    ids: list[str] = []
    if directory is None:
        return ids
    for item in directory.list():
        user_id = str(item.get("dingtalkUserId") or "").strip()
        if not user_id or user_id in ids:
            continue
        if item.get("role") == "admin" or is_confirmed_admin_name(item.get("buyerName") or ""):
            ids.append(user_id)
    return ids


def _bound_names(directory, bound: dict) -> list[str]:
    if not bound:
        return []
    getter = getattr(directory, "bound_buyer_names", None) if directory is not None else None
    if callable(getter):
        return [name for name in getter(bound) if name]
    name = str(bound.get("buyerName") or "").strip()
    return [name] if name else []


def is_admin(directory, sender_id: str, *, extra_ids=(), sender_name: str = "") -> bool:
    """管理员只认已绑定且 role=admin（或韩立）的钉钉 userId，不认显示名和 extra_ids。"""
    del extra_ids, sender_name
    sender_id = str(sender_id or "").strip()
    if directory is None or not sender_id:
        return False
    bound = directory.get_by_dingtalk_user_id(sender_id)
    if not bound:
        return False
    if bound.get("role") == "admin":
        return True
    return any(is_confirmed_admin_name(name) for name in _bound_names(directory, bound))


def is_super_admin(directory, sender_id: str, *, extra_ids=(), sender_name: str = "") -> bool:
    """最高管理员：已绑定的韩立 / ERP「管理员」。只有他能设/取消管理员。"""
    del extra_ids, sender_name
    sender_id = str(sender_id or "").strip()
    if directory is None or not sender_id:
        return False
    bound = directory.get_by_dingtalk_user_id(sender_id)
    return any(is_confirmed_admin_name(name) for name in _bound_names(directory, bound))


def parse_bind_tokens(raw: str, pending: list) -> list[str]:
    """把「全部 / 1 2 / a1b2c3d4」收成申请编号。空或全部 = 当前全部待审批。"""
    text = str(raw or "").strip()
    if not text or text in ("全部", "all"):
        return [item["id"] for item in pending]
    ids: list[str] = []
    for token in TOKEN_SPLIT.split(text):
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= len(pending):
            ids.append(pending[int(token) - 1]["id"])
            continue
        ids.append(token.lower())
    return ids


def format_pending_binds(pending: list) -> str:
    if not pending:
        return "没有待审批的绑定申请。"
    lines = [f"待审批绑定 {len(pending)} 条："]
    for index, item in enumerate(pending, start=1):
        names = "、".join(item.get("names") or [])
        sender = item.get("senderName") or item.get("senderId") or "未知"
        extra = f"；{item['note']}" if item.get("note") else ""
        lines.append(f"{index}. {item['id']}  钉钉「{sender}」申请绑定「{names}」{extra}")
    lines.append("回复「同意绑定」或「确认绑定」同意全部。拒绝用「拒绝绑定 1」或「拒绝绑定全部」。")
    return "\n".join(lines)


def apply_binding(directory, *, names, sender_id: str, sender_name: str = "",
                  users=None) -> list[str]:
    """管理员同意后写入绑定。匹配不到 users 不建用户。姓名已被别人占用则拒绝，不覆盖。"""
    conflict = conflict_note(directory, names=names, sender_id=sender_id)
    if conflict:
        raise ValueError(f"绑定冲突，未写入：{conflict}。请拒绝该申请，或先让原绑定人改绑。")
    bound = []
    for name in names:
        existing = directory.get(name) or {}
        role = existing.get("role") or "operator"
        if is_confirmed_admin_name(name):
            role = "admin"
        binding = directory.upsert(
            name,
            dingtalk_user_id=sender_id,
            mobile=existing.get("mobile") or "",
            note=existing.get("note") or sender_name,
            role=role,
        )
        if users is not None:
            link_binding_to_user(
                users, name, dingtalk_userid=sender_id,
                mobile=existing.get("mobile") or "",
            )
        bound.append(binding["buyerName"])
    promote = getattr(directory, "promote_builtin_admins", None)
    if callable(promote):
        promote()
    return bound


def already_bound(directory, *, names, sender_id: str) -> bool:
    if directory is None or not sender_id:
        return False
    for name in names:
        existing = directory.get(name) or {}
        if existing.get("dingtalkUserId") != sender_id:
            return False
    return bool(names)


def conflict_note(directory, *, names, sender_id: str) -> str:
    if directory is None:
        return ""
    parts = []
    for name in names:
        existing = directory.get(name) or {}
        other = str(existing.get("dingtalkUserId") or "").strip()
        if other and other != sender_id:
            parts.append(f"{name} 现绑 {other}")
    return "；".join(parts)


class BindRequests:
    def __init__(self, store):
        self.store = store

    def create(self, *, sender_id: str, sender_name: str, names, note: str = "",
               conversation_id: str = "") -> dict:
        names = [str(item).strip() for item in names if str(item or "").strip()]
        if not names:
            raise ValueError("绑定姓名不能为空")
        existing = self.find_open(sender_id=sender_id, names=names)
        if existing:
            return existing
        request_id = secrets.token_hex(4)
        stamp = now()
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO bind_requests
                   (id, sender_id, sender_name, names_json, status,
                    requested_at, decided_at, decided_by, note, conversation_id)
                   VALUES (?, ?, ?, ?, 'pending', ?, NULL, '', ?, ?)""",
                (
                    request_id, sender_id, sender_name or "", dumps(names), stamp,
                    note or "", str(conversation_id or "").strip(),
                ),
            )
        return self.get(request_id)

    def find_open(self, *, sender_id: str, names) -> dict:
        wanted = [str(item).strip() for item in names if str(item or "").strip()]
        sender_id = str(sender_id or "").strip()
        if not sender_id or not wanted:
            return {}
        for item in self.list_pending():
            if item.get("senderId") == sender_id and item.get("names") == wanted:
                return item
        return {}

    def get(self, request_id: str) -> dict:
        request_id = str(request_id or "").strip().lower()
        if not request_id:
            return {}
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM bind_requests WHERE id = ?", (request_id,),
            ).fetchone()
        return self._row(row) if row else {}

    def list_pending(self) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM bind_requests WHERE status = 'pending' ORDER BY requested_at, id",
            ).fetchall()
        return [self._row(row) for row in rows]

    def decide(self, request_id: str, *, status: str, decided_by: str) -> dict:
        if status not in ("approved", "rejected"):
            raise ValueError("绑定申请只能同意或拒绝")
        request_id = str(request_id or "").strip().lower()
        with self.store.write(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM bind_requests WHERE id = ?", (request_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到绑定申请 {request_id}")
            if row["status"] != "pending":
                return self._row(row)
            claimed = conn.execute(
                """UPDATE bind_requests
                   SET status=?, decided_at=?, decided_by=?
                   WHERE id=? AND status='pending'""",
                (status, now(), str(decided_by or "").strip(), request_id),
            )
            if claimed.rowcount != 1:
                return self._row(
                    conn.execute(
                        "SELECT * FROM bind_requests WHERE id = ?", (request_id,),
                    ).fetchone()
                )
        return self.get(request_id)

    def reopen(self, request_id: str) -> dict:
        """同意后写入失败时退回 pending，避免申请被吃掉却没绑上。"""
        request_id = str(request_id or "").strip().lower()
        if not request_id:
            return {}
        with self.store.write(immediate=True) as conn:
            conn.execute(
                """UPDATE bind_requests
                   SET status='pending', decided_at=NULL, decided_by=''
                   WHERE id=? AND status='approved'""",
                (request_id,),
            )
        return self.get(request_id)

    @staticmethod
    def _row(row) -> dict:
        if row is None:
            return {}
        return {
            "id": row["id"],
            "senderId": row["sender_id"],
            "senderName": row["sender_name"],
            "names": [str(item) for item in (loads(row["names_json"], []) or []) if str(item).strip()],
            "status": row["status"],
            "requestedAt": row["requested_at"],
            "decidedAt": row["decided_at"] or "",
            "decidedBy": row["decided_by"],
            "note": row["note"],
            "conversationId": row["conversation_id"] if "conversation_id" in row.keys() else "",
        }
