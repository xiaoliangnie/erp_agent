# -*- coding: utf-8 -*-
"""ERP 登录态保活：工作时段维持浏览器和 cookie，不固定停在某一页。

鞋垫写入、代发抓取、以后合同页自动化都复用同一套登录态。
保活只做 ``login_if_needed`` + 落 ``storage_state``，不去订单列表。
写入占用 ``DigitalRuntime.exclusive`` 时本轮跳过，不和换货抢页。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from ..business_time import business_now
from .session import playwright_available

logger = logging.getLogger(__name__)

DEFAULT_START = (9, 30)
DEFAULT_END = (18, 30)


def parse_hhmm(value, default=DEFAULT_START) -> tuple[int, int]:
    try:
        hour, minute = str(value or "").split(":", 1)
        return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
    except (TypeError, ValueError):
        return default


def in_keepalive_window(now: datetime, *, start=DEFAULT_START, end=DEFAULT_END) -> bool:
    """北京时间闭区间；跨日窗口（例如 22:00–06:00）也支持。"""
    current = (now.hour, now.minute)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


class ErpKeepAlive:
    """工作时段内周期性确认仍在 ERP 内；时段外关掉浏览器，cookie 留在本机。"""

    def __init__(
        self,
        runtime,
        *,
        enabled: bool = True,
        start_time: str = "09:30",
        end_time: str = "18:30",
        interval_seconds: int = 180,
        poll_seconds: int = 30,
    ):
        self.runtime = runtime
        self.enabled = bool(enabled)
        self.start_at = parse_hhmm(start_time, DEFAULT_START)
        self.end_at = parse_hhmm(end_time, DEFAULT_END)
        self.interval_seconds = max(30, int(interval_seconds))
        self.poll_seconds = max(5, int(poll_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._warmed = False
        self._closed_after_hours = False
        self.last_ok = ""
        self.last_error = ""
        self.last_skip = ""

    def status(self) -> dict:
        now = business_now()
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "window": f"{self.start_at[0]:02d}:{self.start_at[1]:02d}-{self.end_at[0]:02d}:{self.end_at[1]:02d}",
            "inWindow": in_keepalive_window(now, start=self.start_at, end=self.end_at),
            "warmed": self._warmed,
            "intervalSeconds": self.interval_seconds,
            "lastOk": self.last_ok,
            "lastError": self.last_error,
            "lastSkip": self.last_skip,
        }

    def start(self) -> dict:
        if not self.enabled:
            return self.status()
        if not self._can_run():
            self.last_error = self._disabled_reason()
            logger.info("ERP 登录态保活未启动：%s", self.last_error)
            return self.status()
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="erp-keepalive", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def _can_run(self) -> bool:
        if not playwright_available():
            return False
        config = getattr(self.runtime, "config", {}) or {}
        secrets = getattr(self.runtime, "secrets", {}) or {}
        if config.get("username") and secrets.get("password"):
            return True
        path = str(config.get("storageStatePath") or "")
        if path:
            from pathlib import Path
            return Path(path).is_file()
        return False

    def _disabled_reason(self) -> str:
        if not playwright_available():
            return "未安装 Playwright"
        return "没有 ERP 账号密码，也没有已保存的登录态"

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("ERP 登录态保活失败")

    def tick(self, *, now: datetime | None = None) -> dict:
        """执行一轮判断；测试可传入 ``now``。"""
        current = now or business_now()
        if not in_keepalive_window(current, start=self.start_at, end=self.end_at):
            return self._leave_window()
        self._closed_after_hours = False
        if self._warmed and self.last_ok:
            elapsed = (current - _parse_stamp(self.last_ok)).total_seconds()
            if elapsed < self.interval_seconds:
                self.last_skip = "未到保活间隔"
                return {"skipped": True, "reason": self.last_skip}
        lock = getattr(self.runtime, "try_exclusive", None)
        if callable(lock) and not lock():
            self.last_skip = "写入中"
            return {"skipped": True, "reason": self.last_skip}
        try:
            result = self.runtime.keep_session()
            self._warmed = True
            self.last_ok = current.isoformat(timespec="seconds")
            self.last_error = ""
            self.last_skip = ""
            return {"ok": True, **(result if isinstance(result, dict) else {})}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("ERP 登录态保活失败：%s", self.last_error)
            return {"failed": True, "reason": self.last_error}
        finally:
            release = getattr(self.runtime, "release_exclusive", None)
            if callable(release):
                release()

    def _leave_window(self) -> dict:
        if not self._warmed or self._closed_after_hours:
            self.last_skip = "非保活时段"
            return {"skipped": True, "reason": self.last_skip}
        lock = getattr(self.runtime, "try_exclusive", None)
        if callable(lock) and not lock():
            self.last_skip = "写入中，稍后关浏览器"
            return {"skipped": True, "reason": self.last_skip}
        try:
            closer = getattr(self.runtime, "close_browser", None) or getattr(self.runtime, "close")
            closer()
            self._warmed = False
            self._closed_after_hours = True
            self.last_skip = "已过保活时段，浏览器已关"
            logger.info("ERP 登录态保活结束，浏览器已关闭（cookie 仍在本机）")
            return {"closed": True, "reason": self.last_skip}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"failed": True, "reason": self.last_error}
        finally:
            release = getattr(self.runtime, "release_exclusive", None)
            if callable(release):
                release()


def _parse_stamp(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return business_now()
