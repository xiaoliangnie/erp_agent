# -*- coding: utf-8 -*-
"""网页身份：钉钉发「绑定网页」开通花名+密码，网页登录后会话维持 30 天。"""
from __future__ import annotations

import hashlib
import os
import secrets

from ..staff_names import buyer_names_equivalent
from .store import AgentStore, later, now


CODE_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
CODE_LENGTH = 20
PASSWORD_LENGTH = 10
# 去掉 0/O/1/l，私信里好念好抄。
_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


class WebAuthError(ValueError):
    """网页绑定、登录或会话错误。"""


def web_login_url(base_url: str = "") -> str:
    """员工能打开的工作台地址。本机回环地址不写进私信。"""
    base = str(base_url or os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return ""
    host = base.lower()
    if "127.0.0.1" in host or "localhost" in host or "0.0.0.0" in host:
        return ""
    return base + "/status"


def format_web_login_notice(*, username: str, password: str, login_url: str = "",
                            preamble: str = "") -> str:
    """私信正文。密码只出现在这里，不要写进群回复。"""
    lines = []
    if preamble:
        lines.append(str(preamble).rstrip())
        lines.append("")
    lines.extend([
        "【网页登录】",
        "",
        f"花名：{username}",
        f"密码：{password}",
        "",
    ])
    if login_url:
        lines.append(f"打开 {login_url} ，点右上角「登录」，或进「工作台」。")
    else:
        lines.append("打开采购系统，点右上角「登录」，或进「工作台」。")
    lines.extend([
        "用上面的花名和密码登录，登录一次 30 天不用再输。",
        "",
        "这条私信只发给你。密码不要发到群里，也不要转给别人。",
        "这是新密码，网页上已经登录的会退出。",
        "以后忘记了，到群里发「绑定网页」，会再私信一封。",
    ])
    return "\n".join(lines)


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _new_code() -> str:
    return secrets.token_hex(CODE_LENGTH // 2)


def _new_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


class WebAuth:
    def __init__(self, store: AgentStore):
        self.store = store

    def has_account(self, *, sender_id: str = "", buyer_name: str = "") -> bool:
        sender_id = str(sender_id or "").strip()
        buyer_name = str(buyer_name or "").strip()
        if not sender_id and not buyer_name:
            return False
        with self.store.read() as conn:
            if sender_id:
                row = conn.execute(
                    "SELECT 1 FROM web_accounts WHERE sender_id=? LIMIT 1", (sender_id,),
                ).fetchone()
                if row:
                    return True
            if buyer_name:
                row = conn.execute(
                    "SELECT 1 FROM web_accounts WHERE buyer_name=? LIMIT 1", (buyer_name,),
                ).fetchone()
                return row is not None
        return False

    def issue_account(self, *, sender_id: str, buyer_name: str, user_id: str = "") -> dict:
        """开通或重置网页密码。明文密码只在这一次返回，给钉钉私信。"""
        sender_id = str(sender_id or "").strip()
        buyer_name = str(buyer_name or "").strip()
        if not sender_id or not buyer_name:
            raise WebAuthError("还没绑定采购员姓名，不能开通网页账号")
        password = _new_password()
        stamp = now()
        with self.store.write() as conn:
            prior = conn.execute(
                "SELECT buyer_name FROM web_accounts WHERE sender_id=?", (sender_id,),
            ).fetchone()
            if prior and prior["buyer_name"]:
                buyer_name = str(prior["buyer_name"])
                reset = True
            else:
                named = conn.execute(
                    "SELECT sender_id FROM web_accounts WHERE buyer_name=?", (buyer_name,),
                ).fetchone()
                reset = named is not None
            conn.execute(
                """INSERT INTO web_accounts
                   (buyer_name, user_id, sender_id, password_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(buyer_name) DO UPDATE SET
                       user_id=excluded.user_id,
                       sender_id=excluded.sender_id,
                       password_hash=excluded.password_hash,
                       updated_at=excluded.updated_at""",
                (buyer_name, str(user_id or ""), sender_id, _hash(password), stamp, stamp),
            )
            conn.execute("DELETE FROM web_sessions WHERE sender_id=?", (sender_id,))
        return {
            "username": buyer_name,
            "password": password,
            "buyerName": buyer_name,
            "reset": reset,
        }

    def login(self, *, username: str, password: str, directory=None) -> dict:
        username = str(username or "").strip()
        password = str(password or "").strip()
        if not username or not password:
            raise WebAuthError("请填写花名和密码")
        stamp = now()
        digest = _hash(password)
        with self.store.write(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM web_accounts WHERE buyer_name=?", (username,),
            ).fetchone()
            if row is None and directory is not None:
                finder = getattr(directory, "find_binding", None)
                bound = finder(operator=username) if callable(finder) else {}
                name = str((bound or {}).get("buyerName") or "").strip()
                if name:
                    row = conn.execute(
                        "SELECT * FROM web_accounts WHERE buyer_name=?", (name,),
                    ).fetchone()
                sender = str((bound or {}).get("dingtalkUserId") or "").strip()
                if row is None and sender:
                    row = conn.execute(
                        "SELECT * FROM web_accounts WHERE sender_id=?", (sender,),
                    ).fetchone()
            if row is None or str(row["password_hash"] or "") != digest:
                raise WebAuthError("花名或密码不对")
            names = [str(row["buyer_name"] or "")]
            if directory is not None:
                getter = getattr(directory, "bound_buyer_names", None)
                bound = {}
                if hasattr(directory, "get"):
                    bound = directory.get(row["buyer_name"]) or {}
                if not bound and row["sender_id"] and hasattr(directory, "get_by_dingtalk_user_id"):
                    bound = directory.get_by_dingtalk_user_id(row["sender_id"]) or {}
                if callable(getter) and bound:
                    names = list(getter(bound)) or names
            if not any(buyer_names_equivalent(username, name, include_nick=True) for name in names if name):
                raise WebAuthError("花名或密码不对")
            return self._open_session(
                conn,
                user_id=str(row["user_id"] or ""),
                sender_id=str(row["sender_id"] or ""),
                buyer_name=str(row["buyer_name"] or ""),
                stamp=stamp,
            )

    def issue_code(self, *, sender_id: str, buyer_name: str, user_id: str = "") -> dict:
        """旧的 20 位身份码，测试和应急仍可用。钉钉「绑定网页」已改发密码。"""
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
            return self._open_session(
                conn,
                user_id=str(row["user_id"] or ""),
                sender_id=str(row["sender_id"] or ""),
                buyer_name=buyer_name,
                stamp=stamp,
            )

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

    def revoke(self, token: str) -> bool:
        token = str(token or "").strip()
        if not token:
            return False
        with self.store.write() as conn:
            deleted = conn.execute(
                "DELETE FROM web_sessions WHERE token_hash=?", (_hash(token),),
            )
            return deleted.rowcount > 0

    def _open_session(self, conn, *, user_id: str, sender_id: str,
                      buyer_name: str, stamp: str) -> dict:
        token = secrets.token_hex(16)
        session_id = secrets.token_hex(12)
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
            "expiresIn": SESSION_TTL_SECONDS,
        }
