# -*- coding: utf-8 -*-
"""当次请求的身份。Web / 钉钉 / CLI 都解析到同一个 user_id。

第一期不建独立账号表：有钉钉绑定时用 dingtalk_user_id；只有姓名绑定时用
``staff:<绑定名>``。共享 Token 不代表操作员。
角色只有 viewer / operator 两档；「我名下」用绑定采购员姓名，不做店铺矩阵。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass


ROLES = ("viewer", "operator")


@dataclass(frozen=True)
class RequestContext:
    operator: str
    channel: str
    user_id: str = ""
    actor_id: str = ""
    session_key: str = ""
    trace_id: str = ""
    role: str = "operator"
    buyer_names: tuple[str, ...] = ()

    def as_public(self) -> dict:
        return {
            "operator": self.operator,
            "channel": self.channel,
            "userId": self.user_id,
            "actorId": self.actor_id,
            "traceId": self.trace_id,
            "role": self.role,
            "buyerNames": list(self.buyer_names),
        }


def _normalize_role(value: str) -> str:
    role = str(value or "").strip().lower()
    return role if role in ROLES else "operator"


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


def resolve_user_id(directory, *, operator: str = "", actor_id: str = "",
                    channel: str = "web") -> str:
    """把署名 / 钉钉 sender 收成稳定 user_id，供审计和确认复用。"""
    operator = str(operator or "").strip()
    actor_id = str(actor_id or "").strip()
    bound = _binding_from_directory(directory, operator=operator, actor_id=actor_id)
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
                            trace_id: str = "") -> RequestContext:
    operator = str(operator or "").strip()[:120]
    actor_id = str(actor_id or "").strip()[:80]
    channel = str(channel or "web").strip() or "web"
    bound = _binding_from_directory(directory, operator=operator, actor_id=actor_id)
    return RequestContext(
        operator=operator,
        channel=channel,
        user_id=resolve_user_id(directory, operator=operator, actor_id=actor_id, channel=channel),
        actor_id=actor_id,
        session_key=str(session_key or "").strip(),
        trace_id=str(trace_id or "").strip() or secrets.token_hex(8),
        role=_normalize_role(bound.get("role") if bound else "operator"),
        buyer_names=_buyer_names_from_binding(directory, bound),
    )
