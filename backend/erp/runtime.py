# -*- coding: utf-8 -*-
"""Digital Worker 运行时：命令白名单 + 写并发 1。

Playwright Sync API 不能在正在跑的 asyncio 循环里调用（钉钉 Stream 的
ChatbotHandler.process 就在那个循环上）。所有浏览器操作固定到本进程
唯一的 `erp-playwright` 线程，钉钉确认、领取循环和 CLI 共用这一条。
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading

from . import exchange_page
from .config import ALLOWED_COMMANDS, load_digital_worker, load_worker_secrets
from .errors import ErpError
from .session import BrowserSession, playwright_available


class PlaywrightThread:
    """把同步 Playwright 钉在一条线程上，避免 Sync API 撞上 asyncio。"""

    def __init__(self, name: str = "erp-playwright"):
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise ErpError("ERP Playwright 线程未能启动")

    @property
    def name(self) -> str:
        return self._thread.name

    def _loop(self) -> None:
        self._ready.set()
        while True:
            job = self._jobs.get()
            if job is None:
                return
            func, args, kwargs, future = job
            try:
                future.set_result(func(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)

    def call(self, func, *args, **kwargs):
        if threading.current_thread() is self._thread:
            return func(*args, **kwargs)
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((func, args, kwargs, future))
        return future.result()


class DigitalRuntime:
    def __init__(self, config: dict, secrets: dict | None = None, *, session=None):
        self.config = config
        self.secrets = secrets or {}
        self.session = session or BrowserSession(config, self.secrets)
        self._lock = threading.Lock()
        self._pw = PlaywrightThread()

    @classmethod
    def from_settings(cls, setting, *, root=None):
        return cls(load_digital_worker(setting, root=root), load_worker_secrets(setting))

    def status(self) -> dict:
        state_exists = False
        path = self.config.get("storageStatePath") or ""
        if path:
            from pathlib import Path
            state_exists = Path(path).exists()
        return {
            **{key: self.config[key] for key in (
                "enabled", "workerId", "baseUrl", "orderListUrl", "username",
                "hasPassword", "hasTotp", "headless", "writeDelayMs",
                "storageStatePath", "allowedCommands", "loginFields",
            ) if key in self.config},
            "playwright": playwright_available(),
            "hasStorageState": state_exists,
            "browserOpen": self.session.page is not None,
        }

    def login(self, *, headed=True) -> dict:
        return self._pw.call(self._login, headed)

    def _login(self, headed: bool) -> dict:
        with self._lock:
            return self.session.login_if_needed(headed=headed)

    def ping(self) -> dict:
        return self._pw.call(self._ping)

    def prepare(self) -> dict:
        """打开一次订单页。后续 run 复用，不再每组尺码重进首页。"""
        return self._pw.call(self._prepare)

    def _ensure_page(self) -> dict:
        self.session.login_if_needed(headed=not self.config.get("headless", True))
        ready = exchange_page.ensure_order_page(
            self.session.page, self.config["orderListUrl"],
        )
        self.session.save_state()
        return ready

    def _ping(self) -> dict:
        with self._lock:
            return {"ok": True, "command": "erp.query_ready", **self._ensure_page()}

    def _prepare(self) -> dict:
        with self._lock:
            return {"ok": True, "command": "erp.prepare", **self._ensure_page()}

    def run(self, command: str, payload: dict | None = None) -> dict:
        command = str(command or "").strip()
        if command not in ALLOWED_COMMANDS:
            raise ErpError(f"未注册的 ERP 命令：{command}。当前只开放 {', '.join(ALLOWED_COMMANDS)}")
        return self._pw.call(self._run, command, payload or {})

    def _run(self, command: str, payload: dict) -> dict:
        with self._lock:
            self._ensure_page()
            if payload.get("confirm"):
                return exchange_page.execute_job(
                    self.session.page, payload,
                    delay_ms=int(self.config.get("writeDelayMs") or 250),
                )
            return exchange_page.plan_job(self.session.page, payload)

    def close(self) -> None:
        self._pw.call(self._close)

    def _close(self) -> None:
        with self._lock:
            self.session.close()
