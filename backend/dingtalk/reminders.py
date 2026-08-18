# -*- coding: utf-8 -*-
"""交期催办推送：只私聊已绑定采购员（架构方案 §8）。

清单由 `backend/delivery_reminders.py` 算出，和台账页、Agent 工具是同一套口径。
这里只负责渲染、按钉钉身份归并、幂等和投递结果留痕。未绑定的人跳过，不再发群。
定时推送不依赖 LLM 和 Agent Core，可以独立上线。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from ..business_time import business_now, business_today
from ..delivery_reminders import (
    FOLLOWUP_URGENT, URGENT_BUCKETS, build_reminders, filter_orders, reminder_markdown,
)
from .identity import StaffDirectory
from .sender import DingTalkError, DingTalkSender


logger = logging.getLogger(__name__)


class ReminderNotifier:
    """把催办清单私聊给已绑定采购员，并保证同一批次只发一次。"""

    def __init__(self, *, sender: DingTalkSender, directory: StaffDirectory, audit,
                 title: str = "采购交期催办", at_all_when_unbound: bool = False,
                 outbox=None):
        self.sender = sender
        self.directory = directory
        self.audit = audit
        self.title = title
        self.at_all_when_unbound = bool(at_all_when_unbound)
        self.outbox = outbox

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
            "warning": ("这些采购员还没绑定钉钉 userId，催办只私聊已绑定的人，不会发群：" + "、".join(resolved["unbound"]))
            if resolved["unbound"] else "",
        }

    def _delivery_groups(self, orders: list) -> tuple[list[dict], list[str]]:
        """按钉钉 userId 归并同一人的单；未绑定或只有手机号的不进发送队列。"""
        groups: dict[str, dict] = {}
        unbound: list[str] = []
        for item in orders:
            buyer = str(item.get("buyer") or "").strip() or "未知"
            binding = self.directory.find_binding(operator=buyer)
            user_id = str((binding or {}).get("dingtalkUserId") or "").strip()
            if not user_id:
                if buyer not in unbound:
                    unbound.append(buyer)
                continue
            bucket = groups.get(user_id)
            if bucket is None:
                bucket = groups[user_id] = {
                    "userId": user_id,
                    "buyer": str((binding or {}).get("buyerName") or buyer),
                    "buyers": [],
                    "orders": [],
                }
            if buyer not in bucket["buyers"]:
                bucket["buyers"].append(buyer)
            bucket["orders"].append(item)
        return sorted(groups.values(), key=lambda item: item["buyer"]), unbound

    def send_reminders(self, reminders: dict, orders: list, *, idempotency_key: str | None = None,
                       operator: str = "", title: str = "", at_user_ids=None) -> dict:
        """按钉钉身份拆开发送：谁的单只私聊谁。未绑定的跳过，不发群。"""
        if not self.enabled:
            raise DingTalkError("钉钉发送通道未配置，无法推送催办")
        if not orders:
            raise DingTalkError("没有需要催办的采购单，不发送空提醒")
        grouped, unbound = self._delivery_groups(orders)
        allowed = {str(item).strip() for item in (at_user_ids or []) if str(item).strip()}
        if allowed:
            grouped = [item for item in grouped if item["userId"] in allowed]
        buyers = [item["buyer"] for item in grouped]
        batch_targets = self.describe_targets(buyers)
        batch_detail = {
            "today": reminders["today"],
            "orderCount": sum(len(item["orders"]) for item in grouped),
            "buyers": buyers,
            "atUserIds": [item["userId"] for item in grouped],
            "atMobiles": batch_targets["atMobiles"],
            "unboundBuyers": unbound,
            "operator": operator,
        }
        if not grouped:
            return {
                "skipped": True,
                "reason": "需催单的采购员都未绑定钉钉，已跳过群发",
                **batch_detail,
            }
        if idempotency_key and self.audit.has_successful_delivery(idempotency_key):
            return {"skipped": True, "reason": "同一批催办已经推送过", **batch_detail}
        if idempotency_key:
            self.audit.release_unsuccessful_key(idempotency_key)
        sent = []
        last_error = None
        for group in grouped:
            child_key = (
                f"{idempotency_key}-{group['userId']}"
                if idempotency_key and len(grouped) > 1 else idempotency_key
            )
            try:
                sent.append(self._send_one(
                    reminders, group["orders"], group["buyer"],
                    user_ids=[group["userId"]],
                    idempotency_key=child_key, operator=operator, title=title,
                ))
            except DingTalkError as exc:
                last_error = exc
                if len(grouped) == 1:
                    raise
                logger.warning("跟单催办发送失败（%s）：%s", group["buyer"], exc)
        if not sent and last_error:
            raise last_error
        if idempotency_key and len(grouped) > 1 and len(sent) == len(grouped):
            self.audit.record_delivery(
                channel="dingtalk", target="oto", kind="delivery_reminder",
                status="sent",
                detail={**batch_detail, "split": True, "messageCount": len(sent)},
                idempotency_key=idempotency_key,
            )
        first = sent[0]
        return {
            "sent": True,
            "channel": first.get("channel") or "oto",
            "messageCount": len(sent),
            "textPreview": first.get("textPreview") or "",
            **batch_detail,
        }

    def _send_one(self, reminders: dict, orders: list, buyer: str, *,
                  user_ids, idempotency_key: str | None, operator: str, title: str) -> dict:
        ids = [str(item).strip() for item in user_ids if str(item or "").strip()]
        if not ids:
            raise DingTalkError("私聊催办缺少钉钉 userId，请先绑定")
        heading = title or self.title
        if buyer and buyer not in heading:
            heading = f"{heading} · {buyer}"
        text = reminder_markdown(reminders, orders, title=heading)
        detail = {
            "today": reminders["today"], "orderCount": len(orders), "buyers": [buyer],
            "atUserIds": ids, "atMobiles": [],
            "unboundBuyers": [], "operator": operator,
        }
        if idempotency_key and self.audit.has_successful_delivery(idempotency_key):
            return {"skipped": True, "reason": "同一批催办已经推送过", **detail}
        if idempotency_key:
            self.audit.release_unsuccessful_key(idempotency_key)
        try:
            if self.outbox is not None:
                response = self.outbox.send_dingtalk(
                    title=heading, text=text, channel="oto",
                    user_ids=ids,
                    idempotency_key=idempotency_key,
                )
            elif getattr(self.sender, "send_oto_markdown", None):
                response = self.sender.send_oto_markdown(heading, text, user_ids=ids)
            else:
                raise DingTalkError("催办只发单聊，当前发送通道不支持")
        except (DingTalkError, RuntimeError) as exc:
            attempt_key = (
                self.audit.next_attempt_key(idempotency_key) if idempotency_key else None
            )
            if not self.audit.record_delivery(
                channel="dingtalk", target="oto", kind="delivery_reminder",
                status="failed", detail=detail, error=str(exc),
                idempotency_key=attempt_key,
            ) and attempt_key:
                self.audit.record_delivery(
                    channel="dingtalk", target="oto", kind="delivery_reminder",
                    status="failed", detail=detail, error=str(exc),
                )
            raise
        if response.get("skipped") and not response.get("sent"):
            return {"skipped": True, "reason": response.get("reason") or "同一批催办已经推送过", **detail}
        self.audit.record_delivery(
            channel="dingtalk", target="oto",
            kind="delivery_reminder",
            status="sent", detail={**detail, "response": response.get("channel") or "oto"},
            idempotency_key=idempotency_key,
        )
        return {"sent": True, "channel": response.get("channel") or "oto", "textPreview": text[:800], **detail}


class DailyReminderScheduler:
    """每天定时推一次催办清单。

    只在进程内起一个后台线程。发送成功才记 `last_run` 并占用当日幂等键；
    失败可按间隔重试，同日最多 ``max_attempts_per_day`` 次，避免钉钉限流时刷群。
    """

    def __init__(self, *, notifier: ReminderNotifier, fetch_rows, send_time: str = "08:30",
                 buckets=None, limit: int = 200, poll_seconds: int = 30,
                 max_attempts_per_day: int = 3, retry_interval_seconds: int = 15 * 60,
                 profile: str = "followup", fetch_followup=None):
        self.notifier = notifier
        self.fetch_rows = fetch_rows
        self.fetch_followup = fetch_followup
        self.send_time = self._parse_time(send_time)
        self.profile = str(profile or "followup")
        self.buckets = tuple(buckets) if buckets else (
            FOLLOWUP_URGENT if self.profile == "followup" else URGENT_BUCKETS
        )
        self.limit = int(limit)
        self.poll_seconds = max(5, int(poll_seconds))
        self.max_attempts_per_day = max(1, int(max_attempts_per_day))
        self.retry_interval_seconds = max(0, int(retry_interval_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.enabled = False
        self.last_run = ""
        self.last_error = ""

    @staticmethod
    def _parse_time(value) -> tuple:
        try:
            hour, minute = str(value or "08:30").split(":", 1)
            return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
        except (TypeError, ValueError):
            return 8, 30

    @staticmethod
    def daily_key(today: str) -> str:
        return f"daily-reminder-{today}"

    def status(self) -> dict:
        today = business_today().isoformat()
        failed = self._failed_attempts(today)
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "sendTime": f"{self.send_time[0]:02d}:{self.send_time[1]:02d}",
            "profile": self.profile,
            "buckets": list(self.buckets),
            "lastRun": self.last_run,
            "lastError": self.last_error,
            "failedAttemptsToday": len(failed),
            "maxAttemptsPerDay": self.max_attempts_per_day,
            "retryIntervalSeconds": self.retry_interval_seconds,
        }

    def start(self) -> None:
        self.enabled = True
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="delivery-reminder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Delivery reminder scheduler tick failed")

    def tick(self, *, now: datetime | None = None) -> dict:
        """执行一轮定时判断；测试可传入 `now`。手动 push 请走 `run_once`。"""
        try:
            current = now or business_now()
            today = current.date().isoformat()
            if (current.hour, current.minute) < self.send_time:
                return {"skipped": True, "reason": "未到推送时间", "today": today}
            if self.last_run == today:
                return {"skipped": True, "reason": "今日已成功推送", "today": today}
            decision = self._retry_decision(today, current=current)
            if not decision["allowed"]:
                return {"skipped": True, "reason": decision["reason"], "today": today}
            result = self.run_once(today=today)
            if result.get("sent") or result.get("skipped"):
                self.last_run = today
                self.last_error = ""
            return result
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("Delivery reminder push failed: %s", self.last_error)
            return {"failed": True, "reason": self.last_error, "today": today}

    def _failed_attempts(self, today: str) -> list[dict]:
        list_fn = getattr(self.notifier.audit, "list_deliveries", None)
        if not list_fn:
            return []
        return list_fn(
            key_prefix=f"{self.daily_key(today)}-attempt-",
            status="failed",
        )

    def _retry_decision(self, today: str, *, current: datetime) -> dict:
        attempts = self._failed_attempts(today)
        if len(attempts) >= self.max_attempts_per_day:
            return {
                "allowed": False,
                "reason": f"今日已失败 {len(attempts)} 次，等次日再试",
                "exhausted": True,
            }
        if attempts and self.retry_interval_seconds:
            last_at = _parse_created_at(attempts[-1].get("createdAt"))
            if last_at is not None:
                elapsed = (current.astimezone(timezone.utc) - last_at).total_seconds()
                if elapsed < self.retry_interval_seconds:
                    return {
                        "allowed": False,
                        "reason": "距上次失败不足重试间隔",
                        "exhausted": False,
                    }
        return {"allowed": True, "reason": ""}

    def run_once(self, *, today=None, buyer="", buckets=None, operator="scheduler",
                 idempotency_key=None, profile=None) -> dict:
        """立即执行一次推送，命令行、手动接口和台账按钮共用；只按「当日已成功」幂等。

        不带采购员时与定时任务共用 `daily-reminder-{today}`；带采购员时用
        `{daily_key}-web-{buyer}`，早上全量推过后仍可单独再催一个人。
        """
        used_profile = str(profile or self.profile or "followup")
        fetch = self.fetch_followup if used_profile == "followup" and self.fetch_followup else self.fetch_rows
        rows, _ = fetch(None)
        reminders = build_reminders(rows, today, profile=used_profile)
        used_buckets = tuple(buckets) if buckets else (
            FOLLOWUP_URGENT if used_profile == "followup" else URGENT_BUCKETS
        )
        buyer = str(buyer or "").strip()
        orders, _ = filter_orders(
            reminders, buckets=used_buckets, buyer=buyer, limit=self.limit,
        )
        if not orders:
            return {"skipped": True, "reason": "今天没有需要催办的采购单", "today": reminders["today"]}
        key = idempotency_key or self.daily_key(reminders["today"])
        if not idempotency_key and buyer:
            key = f"{key}-web-{buyer}"
        title = f"跟单催办 · {reminders['today']}"
        if buyer:
            title = f"跟单催办 · {buyer} · {reminders['today']}"
        return self.notifier.send_reminders(
            reminders, orders,
            idempotency_key=key,
            operator=operator or "scheduler",
            title=title,
        )


def _parse_created_at(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
