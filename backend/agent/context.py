# -*- coding: utf-8 -*-
"""当次请求的身份。Web / 钉钉 / CLI 都解析到同一个 user_id。

有 `users` 表命中时用 `usr_...`；否则回退 staff_bindings 上的 user_id，
再回退钉钉 userId / `staff:<绑定名>`。共享 Token 不代表操作员。
角色是 viewer / operator / admin；「我名下」用绑定采购员姓名或 User 别名。
钉钉员工私聊不能走完整对话，管理员私聊可以。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .users import is_confirmed_admin_name


ROLES = ("viewer", "operator", "admin")


@dataclass(frozen=True)
class RequestContext:
    operator: str
    channel: str
    user_id: str = ""
    actor_id: str = ""
    session_key: str = ""
    session_id: str = ""
    conversation_id: str = ""
    tenant_id: str = ""
    trace_id: str = ""
    role: str = "operator"
    buyer_names: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    data_scope: str = ""

    def as_public(self) -> dict:
        return {
            "operator": self.operator,
            "channel": self.channel,
            "userId": self.user_id,
            "actorId": self.actor_id,
            "sessionId": self.session_id,
            "conversationId": self.conversation_id,
            "tenantId": self.tenant_id,
            "traceId": self.trace_id,
            "role": self.role,
            "buyerNames": list(self.buyer_names),
            "roles": list(self.roles),
            "permissions": list(self.permissions),
            "dataScope": self.data_scope,
        }


def _normalize_role(value: str) -> str:
    role = str(value or "").strip().lower()
    return role if role in ROLES else "operator"


def _role_from_binding(bound: dict, operator: str) -> str:
    del operator
    if not bound:
        return "operator"
    role = _normalize_role(bound.get("role"))
    if role == "admin":
        return role
    if is_confirmed_admin_name(bound.get("buyerName") or ""):
        return "admin"
    return role


def _binding_from_directory(directory, *, operator: str, actor_id: str) -> dict:
    if directory is None:
        return {}
    finder = getattr(directory, "find_binding", None)
    if callable(finder):
        return finder(operator=operator, actor_id=actor_id) or {}
    if actor_id:
        return directory.get_by_dingtalk_user_id(actor_id) or {}
    if operator and hasattr(directory, "get"):
        return directory.get(operator) or {}
    return {}


def _buyer_names_from_binding(directory, bound: dict) -> tuple[str, ...]:
    if not bound:
        return ()
    names_fn = getattr(directory, "bound_buyer_names", None) if directory is not None else None
    if callable(names_fn):
        return tuple(name for name in names_fn(bound) if name)
    name = str(bound.get("buyerName") or "").strip()
    return (name,) if name else ()


def _resolve_user_record(users, *, operator: str, actor_id: str):
    if users is None:
        return None
    if actor_id:
        hit = users.resolve_by_dingtalk(actor_id)
        if hit.matched:
            return hit
    if operator:
        hit = users.resolve_by_erp_buyer(operator)
        if hit.matched:
            return hit
    return None


def resolve_user_id(directory, *, operator: str = "", actor_id: str = "",
                    channel: str = "web", users=None) -> str:
    """把署名 / 钉钉 sender 收成稳定 user_id，供审计和确认复用。"""
    operator = str(operator or "").strip()
    actor_id = str(actor_id or "").strip()
    bound = _binding_from_directory(directory, operator=operator, actor_id=actor_id)
    if bound.get("userId"):
        return str(bound["userId"])
    hit = _resolve_user_record(users, operator=operator, actor_id=actor_id)
    if hit is not None:
        return hit.user_id
    if bound.get("dingtalkUserId"):
        return str(bound["dingtalkUserId"])
    if bound.get("buyerName"):
        return f"staff:{bound['buyerName']}"
    if actor_id:
        return actor_id
    if channel == "cli" and operator:
        return f"cli:{operator}"
    return ""


def resolve_request_context(directory=None, *, operator: str = "", channel: str = "web",
                            actor_id: str = "", session_key: str = "",
                            session_id: str = "", conversation_id: str = "",
                            tenant_id: str = "", trace_id: str = "",
                            users=None) -> RequestContext:
    operator = str(operator or "").strip()[:120]
    actor_id = str(actor_id or "").strip()[:80]
    channel = str(channel or "web").strip() or "web"
    bound = _binding_from_directory(directory, operator=operator, actor_id=actor_id)
    hit = _resolve_user_record(users, operator=operator, actor_id=actor_id)
    names = list(_buyer_names_from_binding(directory, bound))
    if hit is not None:
        for alias in hit.aliases:
            if alias not in names:
                names.append(alias)
    return RequestContext(
        operator=operator,
        channel=channel,
        user_id=resolve_user_id(
            directory, operator=operator, actor_id=actor_id, channel=channel, users=users,
        ),
        actor_id=actor_id,
        session_key=str(session_key or "").strip(),
        session_id=str(session_id or "").strip(),
        conversation_id=str(conversation_id or session_key or "").strip(),
        tenant_id=str(tenant_id or "").strip(),
        trace_id=str(trace_id or "").strip() or secrets.token_hex(8),
        role=_role_from_binding(bound, operator),
        buyer_names=tuple(names),
        roles=(_role_from_binding(bound, operator),),
        permissions=(),
        data_scope="",
    )


def identity_block(request: RequestContext) -> str:
    """上下文第二层：渠道、角色、绑定范围。不含业务数字。"""
    buyers = "、".join(request.buyer_names) if request.buyer_names else "未绑定"
    return (
        f"当前请求：渠道={request.channel}，角色={request.role}，"
        f"员工={request.operator or '未署名'}，绑定采购员={buyers}。"
        "记忆和摘要里的编号只作指代；金额、数量、交期、状态必须重新调用工具。"
    )
