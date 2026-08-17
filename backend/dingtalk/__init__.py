# -*- coding: utf-8 -*-
"""钉钉通道：与网页共用同一个 Agent Core 和同一套确认流。

`DINGTALK_ENABLED=false` 时不起任何线程；定时催办推送可以单独开关，不依赖 LLM。
"""
from pathlib import Path

from .identity import StaffDirectory
from .reminders import DailyReminderScheduler, ReminderNotifier
from .sender import DingTalkError, DingTalkSender
from .stream import DingTalkStreamChannel, sdk_available


__all__ = [
    "DailyReminderScheduler", "DingTalkError", "DingTalkSender", "DingTalkStreamChannel",
    "ReminderNotifier", "StaffDirectory", "build_dingtalk", "sdk_available",
]


def build_dingtalk(*, setting, store, audit, flag, root=None):
    """按 `.env` 装配发送器、身份目录和催办通知器。

    返回的 `notifier` 会注入 Agent 工具上下文；未配置发送通道时 `notifier.enabled`
    为 False，`send_delivery_reminder` 工具不会被注册。
    """
    if not flag(setting("DINGTALK_ENABLED", "false")):
        sender = DingTalkSender()
    else:
        sender = DingTalkSender(
            webhook_url=setting("DINGTALK_WEBHOOK_URL", ""),
            webhook_secret=setting("DINGTALK_WEBHOOK_SECRET", ""),
            client_id=setting("DINGTALK_CLIENT_ID", ""),
            client_secret=setting("DINGTALK_CLIENT_SECRET", ""),
            robot_code=setting("DINGTALK_ROBOT_CODE", ""),
            group_conversation_id=setting("DINGTALK_GROUP_CONVERSATION_ID", ""),
        )
    directory = StaffDirectory(store)
    if root:
        from ..paths import local_dir
        directory.seed_from_json(local_dir("config", root=root) / "staff_bindings.json")
    notifier = ReminderNotifier(
        sender=sender, directory=directory, audit=audit,
        title=setting("DINGTALK_REMINDER_TITLE", "采购交期催办"),
        at_all_when_unbound=flag(setting("DINGTALK_REMINDER_AT_ALL", "false")),
    )
    return sender, directory, notifier
