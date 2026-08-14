# -*- coding: utf-8 -*-
"""每日品控日报调度。骨架与催办调度相同，含 R0-4 抗错。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..business_time import business_now, business_today
from .report import build_quality_workbook, quality_report_markdown


logger = logging.getLogger(__name__)


class DailyQualityReportScheduler:
    def __init__(self, *, ledger, sender, audit, output_dir: Path,
                 send_time: str = "17:30", empty_mode: str = "skip",
                 link_secret: str = "", public_base: str = "",
                 poll_seconds: int = 30, max_attempts_per_day: int = 3,
                 retry_interval_seconds: int = 15 * 60):
        self.ledger = ledger
        self.sender = sender
        self.audit = audit
        self.output_dir = Path(output_dir)
        self.send_time = self._parse_time(send_time)
        self.empty_mode = str(empty_mode or "skip").strip().lower()
        self.link_secret = str(link_secret or "").strip()
        self.public_base = str(public_base or "").rstrip("/")
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
            hour, minute = str(value or "17:30").split(":", 1)
            return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
        except (TypeError, ValueError):
            return 17, 30

    @staticmethod
    def daily_key(today: str) -> str:
        return f"quality-report-{today}"

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "sendTime": f"{self.send_time[0]:02d}:{self.send_time[1]:02d}",
            "lastRun": self.last_run,
            "lastError": self.last_error,
        }

    def start(self) -> None:
        self.enabled = True
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="quality-report", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Quality report scheduler tick failed")

    def tick(self, *, now: datetime | None = None) -> dict:
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
            logger.error("Quality report push failed: %s", self.last_error)
            return {"failed": True, "reason": self.last_error, "today": business_today().isoformat()}

    def _failed_attempts(self, today: str) -> list[dict]:
        list_fn = getattr(self.audit, "list_deliveries", None)
        if not list_fn:
            return []
        return list_fn(key_prefix=f"{self.daily_key(today)}-attempt-", status="failed")

    def _retry_decision(self, today: str, *, current: datetime) -> dict:
        attempts = self._failed_attempts(today)
        if len(attempts) >= self.max_attempts_per_day:
            return {"allowed": False, "reason": f"今日已失败 {len(attempts)} 次，等次日再试"}
        if attempts and self.retry_interval_seconds:
            last_at = _parse_created_at(attempts[-1].get("createdAt"))
            if last_at is not None:
                elapsed = (current.astimezone(timezone.utc) - last_at).total_seconds()
                if elapsed < self.retry_interval_seconds:
                    return {"allowed": False, "reason": "距上次失败不足重试间隔"}
        return {"allowed": True, "reason": ""}

    def run_once(self, *, today=None, operator="scheduler", idempotency_key=None) -> dict:
        today = str(today or business_today().isoformat())
        issues = self.ledger.list_for_report(today)
        if not issues and self.empty_mode != "notice":
            return {"skipped": True, "reason": "今日无品控登记", "today": today}
        key = idempotency_key or self.daily_key(today)
        if self.audit.has_successful_delivery(key):
            return {"skipped": True, "reason": "同一批日报已经推送过", "today": today}
        self.audit.release_unsuccessful_key(key)
        historic = self.ledger.open_count(before=today)
        compact = today.replace("-", "")
        path = self.output_dir / f"品控台账-{compact}.xlsx"
        if issues:
            build_quality_workbook(issues, path)
        title = f"品控日报 · {today}"
        text = quality_report_markdown(issues, today=today, historic_open=historic)
        if not issues:
            text = f"今日无品控登记（{today}）。"
        detail = {"today": today, "count": len(issues), "operator": operator, "path": str(path)}
        try:
            response = self._send(title, text, path if issues else None, today)
        except Exception as exc:
            attempt_key = self.audit.next_attempt_key(key)
            self.audit.record_delivery(
                channel="dingtalk", target="group", kind="quality_report",
                status="failed", detail=detail, error=str(exc),
                idempotency_key=attempt_key,
            )
            raise
        self.audit.record_delivery(
            channel="dingtalk", target="group", kind="quality_report",
            status="sent", detail={**detail, "response": response.get("channel")},
            idempotency_key=key,
        )
        return {"sent": True, "today": today, "count": len(issues), **response}

    def _send(self, title: str, text: str, path: Path | None, today: str) -> dict:
        if self.sender is None or not getattr(self.sender, "configured", False):
            raise RuntimeError("钉钉发送通道未配置，无法推送品控日报")
        if getattr(self.sender, "app_ready", False) and getattr(self.sender, "group_conversation_id", ""):
            markdown = self.sender.send_markdown(title, text)
            file_info = {}
            if path and path.exists() and hasattr(self.sender, "upload_media"):
                media = self.sender.upload_media(path, filetype="file")
                file_info = self.sender.send_file(
                    self.sender.group_conversation_id, media["mediaId"],
                    path.name, file_type="xlsx",
                )
            return {"channel": "app", "markdown": markdown, "file": file_info}
        if path and path.exists() and self.link_secret and self.public_base:
            from . import report_link_sig
            compact = today.replace("-", "")
            sig = report_link_sig(self.link_secret, compact)
            text = f"{text}\n\n下载：{self.public_base}/api/quality/reports/{compact}/{sig}.xlsx"
        return self.sender.send_markdown(title, text)


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
