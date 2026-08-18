# -*- coding: utf-8 -*-
"""操作权限。

权限表尚未落地：现在只拦 viewer，以及钉钉写操作必须已绑定。
以后在 `staff_bindings` / users 表维护 capabilities 时，只改这一处。
"""
from __future__ import annotations

from .tools import PermissionDenied


CAPABILITY_INSOLE_PROCESS = "erp.exchange_insole"


def _capabilities(binding: dict) -> list[str] | None:
    raw = binding.get("capabilities")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return None


def check_capability(
    directory,
    *,
    operator: str,
    actor_id: str = "",
    channel: str = "web",
    role: str = "operator",
    capability: str,
) -> None:
    """无权限则抛 PermissionDenied。directory 为空时（离线单测）不拦绑定。"""
    if str(role or "operator") == "viewer":
        raise PermissionDenied(
            "当前身份是只读，不能执行写操作",
            role=role, permission="write", channel=channel,
        )
    binding = {}
    if directory is not None:
        if channel == "dingtalk":
            getter = getattr(directory, "get_by_dingtalk_user_id", None)
            if getter is not None:
                binding = getter(actor_id) or {}
            if not actor_id or not binding:
                raise PermissionDenied(
                    "还没绑定采购员姓名。请到群里发「绑定 利特」或「绑定 利特、李佳冬（利特）」，管理员同意后生效。",
                    role=role, permission="write", channel=channel,
                )
        elif operator and hasattr(directory, "get"):
            binding = directory.get(operator) or {}
    caps = _capabilities(binding)
    if caps is not None and capability not in caps and "*" not in caps:
        raise PermissionDenied(
            f"没有 {capability} 权限，请让管理员在权限表开通后再从钉钉操作",
            role=role, permission="write", channel=channel,
        )
