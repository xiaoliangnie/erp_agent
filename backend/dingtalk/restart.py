# -*- coding: utf-8 -*-
"""服务重启后：把未确认动作再私聊提醒一遍。不发群。"""
from __future__ import annotations

import logging

from ..agent.actions import DEFAULT_RESTART_CONFIRM_SECONDS, PendingActions
from .sender import DingTalkError


logger = logging.getLogger(__name__)


def notify_pending_after_restart(
    actions: list[dict],
    *,
    sender,
    directory=None,
    audit=None,
    ttl_seconds: int = DEFAULT_RESTART_CONFIRM_SECONDS,
) -> dict:
    """只私聊已绑定员工。没有 userId 的跳过，避免再刷群。"""
    sent = 0
    skipped = 0
    errors = []
    for action in actions or []:
        user_ids = _user_ids(action, directory)
        if not user_ids:
            skipped += 1
            logger.warning(
                "重启补发跳过 %s：%s 未绑定钉钉",
                action.get("id"), action.get("operator") or "未署名",
            )
            continue
        if sender is None or not getattr(sender, "app_ready", False):
            skipped += 1
            continue
        text = PendingActions.restart_notice(action, ttl_seconds=ttl_seconds)
        try:
            sender.send_oto_markdown("待确认动作提醒", text, user_ids=user_ids)
            sent += 1
            if audit is not None:
                audit.record_delivery(
                    channel="dingtalk", target="oto",
                    kind="restart_confirm", status="sent",
                    detail={
                        "actionId": action.get("id"),
                        "operator": action.get("operator"),
                        "userIds": user_ids,
                    },
                    idempotency_key=f"restart-confirm-{action.get('id')}-{action.get('expiresAt')}",
                )
        except DingTalkError as exc:
            errors.append(str(exc))
            logger.warning("重启补发失败 %s：%s", action.get("id"), exc)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            logger.exception("重启补发失败 %s", action.get("id"))
    return {
        "count": len(actions or []),
        "sent": sent,
        "skipped": skipped,
        "errors": errors,
    }


def _user_ids(action: dict, directory) -> list[str]:
    actor = str(action.get("actorId") or "").strip()
    if actor:
        return [actor]
    if directory is None:
        return []
    bound = directory.find_binding(operator=action.get("operator") or "")
    user_id = str((bound or {}).get("dingtalkUserId") or "").strip()
    return [user_id] if user_id else []
