# -*- coding: utf-8 -*-
"""网页身份：钉钉已绑定员工在群里要码，网页用姓名+码绑定一次。"""
from __future__ import annotations

import hashlib
import secrets

from ..staff_names import buyer_names_equivalent
from .store import AgentStore, later, now


CODE_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 90 * 24 * 60 * 60
CODE_LENGTH = 20


class WebAuthError(ValueError):
    """网页绑定或会话错误。"""


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _new_code() -> str:
    return secrets.token_hex(CODE_LENGTH // 2)


class WebAuth:
    def __init__(self, store: AgentStore):
        self.store = store

    def issue_code(self, *, sender_id: str, buyer_name: str, user_id: str = "") -> dict:
        sender_id = str(sender_id or "").strip()
        buyer_name = str(buyer_name or "").strip()
        if not sender_id or not buyer_name:
            raise WebAuthError("还没绑定采购员姓名，不能发网页身份码")
        code = _new_code()
        stamp = now()
        item_id = secrets.token_hex(8)
        with self.store.write() as conn:
            conn.execute(
                """UPDATE web_bind_codes SET used_at=?
                   WHERE sender_id=? AND used_at IS NULL""",
                (stamp, sender_id),
            )
            conn.execute(
                """INSERT INTO web_bind_codes
                   (id, code_hash, user_id, sender_id, buyer_name,
                    created_at, expires_at, used_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    item_id, _hash(code), str(user_id or ""), sender_id, buyer_name,
                    stamp, later(CODE_TTL_SECONDS),
                ),
            )
        return {
            "id": item_id,
            "code": code,
            "buyerName": buyer_name,
            "expiresIn": CODE_TTL_SECONDS,
        }

    def consume_code(self, *, operator: str, code: str, directory=None) -> dict:
        operator = str(operator or "").strip()
        code = str(code or "").strip()
        if len(code) != CODE_LENGTH:
            raise WebAuthError("身份码必须是 20 位")
        if not operator:
            raise WebAuthError("请填写与钉钉绑定一致的采购员姓名")
        stamp = now()
        with self.store.write(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM web_bind_codes
                   WHERE code_hash=? AND used_at IS NULL""",
                (_hash(code),),
            ).fetchone()
            if row is None:
                raise WebAuthError("身份码无效或已使用")
            if str(row["expires_at"] or "") <= stamp:
                raise WebAuthError("身份码已过期，请到群里重新发「绑定网页」")
            buyer_name = str(row["buyer_name"] or "")
            names = [buyer_name]
            if directory is not None:
                getter = getattr(directory, "bound_buyer_names", None)
                bound = directory.get(buyer_name) or {}
                if not bound and row["sender_id"]:
                    bound = directory.get_by_dingtalk_user_id(row["sender_id"]) or {}
                if callable(getter) and bound:
                    names = list(getter(bound)) or names
            if not any(buyer_names_equivalent(operator, name, include_nick=True) for name in names if name):
                raise WebAuthError("网页署名与该身份码对应的采购员不一致")
            claimed = conn.execute(
                "UPDATE web_bind_codes SET used_at=? WHERE id=? AND used_at IS NULL",
                (stamp, row["id"]),
            )
            if claimed.rowcount != 1:
                raise WebAuthError("身份码无效或已使用")
            token = secrets.token_hex(16)
            session_id = secrets.token_hex(12)
            user_id = str(row["user_id"] or "")
            sender_id = str(row["sender_id"] or "")
            conn.execute(
                """INSERT INTO web_sessions
                   (id, token_hash, user_id, sender_id, buyer_name,
                    created_at, expires_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, _hash(token), user_id, sender_id, buyer_name,
                    stamp, later(SESSION_TTL_SECONDS), stamp,
                ),
            )
        return {
            "webToken": token,
            "operator": buyer_name,
            "userId": user_id,
            "senderId": sender_id,
        }

    def get_session(self, token: str) -> dict:
        token = str(token or "").strip()
        if not token:
            return {}
        stamp = now()
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT * FROM web_sessions WHERE token_hash=?", (_hash(token),),
            ).fetchone()
            if row is None:
                return {}
            if str(row["expires_at"] or "") <= stamp:
                return {}
            conn.execute(
                "UPDATE web_sessions SET last_seen_at=? WHERE id=?",
                (stamp, row["id"]),
            )
            session = {
                "id": row["id"],
                "userId": row["user_id"],
                "senderId": row["sender_id"],
                "buyerName": row["buyer_name"],
            }
        return session
