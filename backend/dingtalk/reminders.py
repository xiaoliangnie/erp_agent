# -*- coding: utf-8 -*-
"""交期催办推送：群内 @ 对应采购员（架构方案 §8）。

清单由 `backend/delivery_reminders.py` 算出，和台账页、Agent 工具是同一套口径。
这里只负责渲染、@ 到人、幂等和投递结果留痕。定时推送不依赖 LLM 和 Agent Core，
可以独立上线。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime

from ..business_time import business_now, business_today
from ..delivery_reminders import URGENT_BUCKETS, build_reminders, filter_orders, reminder_markdown
from .identity import StaffDirectory
from .sender import DingTalkError, DingTalkSender


class ReminderNotifier:
    """把催办清单发到钉钉群，并保证同一批次只发一次。"""

    def __init__(self, *, sender: DingTalkSender, directory: StaffDirectory, audit,
                 title: str = "采购交期催办", at_all_when_unbound: bool = False):
        self.sender = sender
        self.directory = directory
        self.audit = audit
        self.title = title
        self.at_all_when_unbound = bool(at_all_when_unbound)

    @property
    def enabled(self) -> bool:
        return self.sender.configured

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "sender": self.sender.status(),
            "bindings": len(self.directory.list()),
        }

    def describe_targets(self, buyers) -> dict:
        """确认前告诉员工这批消息会 @ 到谁、谁还没绑定。"""
        resolved = self.directory.resolve(buyers)
        return {
            "atUserIds": resolved["userIds"],
            "atMobiles": resolved["mobiles"],
            "matchedBuyers": resolved["matched"],
            "unboundBuyers": resolved["unbound"],
            "warning": ("这些采购员还没绑定钉钉，消息里不会 @ 到人：" + "、".join(resolved["unbound"]))
            if resolved["unbound"] else "",
        }

    def send_reminders(self, reminders: dict, orders: list, *, idempotency_key: str | None = None,
                       operator: str = "", title: str = "") -> dict:
        if not self.enabled:
            raise DingTalkError("钉钉发送通道未配置，无法推送催办")
        if not orders:
            raise DingTalkError("没有需要催办的采购单，不发送空提醒")
        buyers = sorted({item["buyer"] for item in orders})
        targets = self.describe_targets(buyers)
        text = reminder_markdown(reminders, orders, title=title or self.title)
        detail = {
            "today": reminders["today"], "orderCount": len(orders), "buyers": buyers,
            "atUserIds": targets["atUserIds"], "atMobiles": targets["atMobiles"],
            "unboundBuyers": targets["unboundBuyers"], "operator": operator,
        }
        if idempotency_key and not self.audit.record_delivery(
            channel="dingtalk", target="group", kind="delivery_reminder", status="sending",
            detail=detail, idempotency_key=idempotency_key,
        ):
            return {"skipped": True, "reason": "同一批催办已经推送过", **detail}
        try:
            response = self.sender.send_markdown(
                title or self.title, text,
                at_user_ids=targets["atUserIds"], at_mobiles=targets["atMobiles"],
                at_all=self.at_all_when_unbound and bool(targets["unboundBuyers"]),
            )
        except DingTalkError as exc:
            self.audit.record_delivery(
                channel="dingtalk", target="group", kind="delivery_reminder",
                status="failed", detail=detail, error=str(exc),
            )
            raise
        self.audit.record_delivery(
            channel="dingtalk", target="group", kind="delivery_reminder",
            status="sent", detail={**detail, "response": response.get("channel")},
        )
        return {"sent": True, "channel": response.get("channel"), "textPreview": text[:800], **detail}


class DailyReminderScheduler:
    """每天定时推一次催办清单。

    只在进程内起一个后台线程；发送失败不重试到死，写审计后等下一天，
    避免钉钉限流时把群刷爆。
    """

    def __init__(self, *, notifier: ReminderNotifier, fetch_rows, send_time: str = "08:30",
                 buckets=URGENT_BUCKETS, limit: int = 200, poll_seconds: int = 30):
        self.notifier = notifier
        self.fetch_rows = fetch_rows
        self.send_time = self._parse_time(send_time)
        self.buckets = tuple(buckets)
        self.limit = int(limit)
        self.poll_seconds = max(5, int(poll_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_run = ""
        self.last_error = ""

    @staticmethod
    def _parse_time(value) -> tuple:
        try:
            hour, minute = str(value or "08:30").split(":", 1)
            return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
        except (TypeError, ValueError):
            return 8, 30

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "sendTime": f"{self.send_time[0]:02d}:{self.send_time[1]:02d}",
            "buckets": list(self.buckets),
            "lastRun": self.last_run,
            "lastError": self.last_error,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="delivery-reminder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            today = business_today().isoformat()
            if self.last_run == today:
                continue
            current = business_now()
            if (current.hour, current.minute) < self.send_time:
                continue
            self.last_run = today
            try:
                self.run_once()
                self.last_error = ""
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                print(f"Delivery reminder push failed: {self.last_error}")

    def run_once(self, *, today=None) -> dict:
        """立即执行一次推送，命令行和定时线程共用。"""
        rows, _ = self.fetch_rows(None)
        reminders = build_reminders(rows, today)
        orders, _ = filter_orders(reminders, buckets=self.buckets, limit=self.limit)
        if not orders:
            return {"skipped": True, "reason": "今天没有需要催办的采购单", "today": reminders["today"]}
        return self.notifier.send_reminders(
            reminders, orders,
            idempotency_key=f"daily-reminder-{reminders['today']}",
            operator="scheduler",
            title=f"每日采购交期催办 · {reminders['today']}",
        )
