# -*- coding: utf-8 -*-
"""ERP 登录态保活：服务在跑就维持浏览器和 cookie，进程退出再关。

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
    """兼容旧测试；登录态已改为随进程，不再按此时段关浏览器。"""
    current = (now.hour, now.minute)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


class ErpKeepAlive:
    """``server.py`` 启动后立刻暖机，运行期间周期性续 cookie，停止时关浏览器。"""

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
        del start_time, end_time
        self.runtime = runtime
        self.enabled = bool(enabled)
        self.interval_seconds = max(30, int(interval_seconds))
        self.poll_seconds = max(5, int(poll_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._warmed = False
        self.last_ok = ""
        self.last_error = ""
        self.last_skip = ""

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "window": "process",
            "inWindow": bool(self._thread and self._thread.is_alive()),
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
            self._thread.join(timeout=8)
        self._thread = None
        if self._warmed:
            self._close_browser()

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
        while True:
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("ERP 登录态保活失败")
            if self._stop.wait(self.poll_seconds):
                break

    def tick(self, *, now: datetime | None = None) -> dict:
        """执行一轮判断；测试可传入 ``now``。"""
        current = now or business_now()
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

    def _close_browser(self) -> None:
        closer = getattr(self.runtime, "close_browser", None) or getattr(self.runtime, "close", None)
        if not callable(closer):
            self._warmed = False
            return
        lock = getattr(self.runtime, "try_exclusive", None)
        held = callable(lock) and lock()
        if callable(lock) and not held:
            logger.info("ERP 登录态停止时写入占用中，留给 Digital Worker 关浏览器")
            return
        try:
            closer()
            self._warmed = False
            self.last_skip = "服务停止，浏览器已关"
            logger.info("ERP 登录态已随服务停止，浏览器已关闭（cookie 仍在本机）")
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("关闭 ERP 浏览器失败：%s", self.last_error)
        finally:
            if held:
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
