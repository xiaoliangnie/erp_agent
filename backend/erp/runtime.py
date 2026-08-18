# -*- coding: utf-8 -*-
"""Digital Worker 运行时：命令白名单 + 写并发 1。

Playwright Sync API 不能在正在跑的 asyncio 循环里调用（钉钉 Stream 的
ChatbotHandler.process 就在那个循环上）。所有浏览器操作固定到本进程
唯一的 `erp-playwright` 线程，钉钉确认、领取循环和 CLI 共用这一条。
"""
from __future__ import annotations

import concurrent.futures
import queue
import secrets
import threading
import time
from pathlib import Path

from . import evidence, exchange_page
from .config import ALLOWED_COMMANDS, load_digital_worker, load_worker_secrets
from .errors import ErpError, ErpUnknownResult
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
        self._job_lock = threading.RLock()
        self._pw = PlaywrightThread()

    def exclusive(self):
        """整段 ERP 作业（试算+写入）持有，避免两组计划在同一页上交错。"""
        return self._job_lock

    def try_exclusive(self) -> bool:
        """保活用：写入占用时本轮跳过，不排队。"""
        return self._job_lock.acquire(blocking=False)

    def release_exclusive(self) -> None:
        try:
            self._job_lock.release()
        except RuntimeError:
            pass

    @classmethod
    def from_settings(cls, setting, *, root=None):
        return cls(load_digital_worker(setting, root=root), load_worker_secrets(setting))

    def status(self) -> dict:
        state_exists = False
        path = self.config.get("storageStatePath") or ""
        if path:
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
        with self._job_lock:
            return self._pw.call(self._login, headed)

    def _login(self, headed: bool) -> dict:
        with self._lock:
            return self.session.login_if_needed(headed=headed)

    def keep_session(self) -> dict:
        """只维持 ERP 登录态，不打开订单列表或其他业务页。"""
        with self._job_lock:
            return self._pw.call(self._keep_session)

    def ping(self) -> dict:
        with self._job_lock:
            return self._pw.call(self._ping)

    def prepare(self) -> dict:
        """打开一次订单页。后续 run 复用，不再每组尺码重进首页。"""
        with self._job_lock:
            return self._pw.call(self._prepare)

    def run_browser(self, func, *args, **kwargs):
        """在 Playwright 线程和同一把锁里跑只读页操作。

        换鞋垫写入仍走 ``run()`` 白名单；代发抓取走这条，复用登录 cookie。
        """
        with self._job_lock:
            return self._pw.call(self._run_browser, func, args, kwargs)

    def _run_browser(self, func, args, kwargs):
        with self._lock:
            self.session.login_if_needed(headed=not self.config.get("headless", True))
            self.session.save_state()
            return func(self.session.page, *args, **kwargs)

    def _ensure_page(self) -> dict:
        self.session.login_if_needed(headed=not self.config.get("headless", True))
        ready = exchange_page.ensure_order_page(
            self.session.page, self.config["orderListUrl"],
        )
        self.session.save_state()
        return ready

    def _keep_session(self) -> dict:
        with self._lock:
            result = self.session.login_if_needed(headed=not self.config.get("headless", True))
            self.session.save_state()
            href = ""
            page = getattr(self.session, "page", None)
            if page is not None:
                href = str(getattr(page, "url", "") or "")
            return {"ok": True, "command": "erp.keep_session", "url": href, **(result or {})}

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
        with self._job_lock:
            return self._pw.call(self._run, command, payload or {})

    def _run(self, command: str, payload: dict) -> dict:
        with self._lock:
            self._ensure_page()
            if payload.get("confirm"):
                delay = payload.get("delayMs", payload.get("delay_ms"))
                if delay is None:
                    delay = self.config.get("writeDelayMs") or 250
                concurrency = payload.get("concurrency", payload.get("writeConcurrency"))
                if concurrency is None:
                    concurrency = 1
                return self._execute_with_evidence(
                    command, payload,
                    delay_ms=max(0, int(delay)),
                    concurrency=max(1, int(concurrency)),
                )
            return exchange_page.plan_job(self.session.page, payload)

    def _execute_with_evidence(self, command: str, payload: dict, *,
                               delay_ms: int, concurrency: int) -> dict:
        """写入前快照 → 已换过的跳过 → 执行 → 回读 → 对不上不记成功。"""
        plans = [item for item in evidence.plans_from_payload(payload) if item.get("ok")]
        expected = [evidence.expected_from_plan(item) for item in plans]
        oids = [item["oId"] for item in expected if item["oId"]]
        read_width = payload.get("readConcurrency", payload.get("read_concurrency"))
        if read_width is None:
            read_width = concurrency
        read_width = max(1, min(8, int(read_width or 1)))
        before = {}
        unread = []
        before_started = time.monotonic()
        loaded = exchange_page.load_orders(
            self.session.page, oids, concurrency=read_width,
        )
        for oid in oids:
            snap = evidence.snapshot_from_order(loaded.get(oid))
            before[oid] = snap
            if snap.get("loadError"):
                unread.append(oid)
        before_ms = int((time.monotonic() - before_started) * 1000)
        if unread:
            raise ErpError(f"写入前无法回读订单：{', '.join(unread)}")

        to_write = []
        already = []
        for plan, exp in zip(plans, expected):
            if evidence.already_exchanged(before.get(exp["oId"], {}), exp):
                already.append(plan)
            else:
                to_write.append(plan)

        evidence_root = self.config.get("evidenceDir") or ""
        command_id = str(payload.get("commandId") or payload.get("id") or "").strip() or secrets.token_hex(12)
        folder = Path(evidence_root) / command_id if evidence_root else None
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
        start_trace = getattr(self.session, "start_trace", None)
        stop_trace = getattr(self.session, "stop_trace", None)
        screenshot = getattr(self.session, "screenshot", None)
        tracing = False
        if folder is not None and callable(start_trace):
            tracing = bool(start_trace())
        if folder is not None and callable(screenshot):
            screenshot(folder / "before.png")

        result = {"succeeded": [], "failed": [], "attempted": 0}
        write_error = None
        if to_write:
            write_payload = {**payload, "plans": to_write, "plan": {"plans": to_write}}
            try:
                result = dict(exchange_page.execute_job(
                    self.session.page, write_payload,
                    delay_ms=delay_ms, concurrency=concurrency,
                ) or {})
            except ErpUnknownResult as exc:
                write_error = exc
        for plan in already:
            oid = evidence.oid_of(plan)
            result.setdefault("succeeded", []).append({"o_id": oid, "alreadyDone": True})

        after = {}
        after_started = time.monotonic()
        loaded = exchange_page.load_orders(
            self.session.page, oids, concurrency=read_width,
        )
        for oid in oids:
            after[oid] = evidence.snapshot_from_order(loaded.get(oid))
        after_ms = int((time.monotonic() - after_started) * 1000)
        if folder is not None and callable(screenshot):
            screenshot(folder / "after.png")
        if tracing and folder is not None and callable(stop_trace):
            stop_trace(folder / "trace.zip")

        recon = evidence.reconcile(before, after, expected)
        result = evidence.apply_reconciliation(result, recon)
        bundle = evidence.write_evidence(
            evidence_root or None,
            command=command,
            command_id=command_id,
            before=before,
            after=after,
            result=result,
            reconciliation=recon,
            summary={
                "oIds": oids,
                "skippedWrite": [evidence.oid_of(item) for item in already],
                "wrote": [evidence.oid_of(item) for item in to_write],
            },
        )
        result["evidence"] = bundle
        result["readMs"] = before_ms + after_ms
        result["readConcurrency"] = read_width
        if write_error is not None or recon.get("status") == "unknown":
            raise ErpUnknownResult(
                str(write_error or "写入后回读失败，结果未知，未重试")
            )
        return result

    def close_browser(self) -> None:
        """关掉浏览器，保留 Playwright 线程和本机 cookie，供进程退出时释放。"""
        with self._job_lock:
            self._pw.call(self._close)

    def close(self) -> None:
        with self._job_lock:
            self._pw.call(self._close)

    def _close(self) -> None:
        with self._lock:
            self.session.close()


def public_worker_status(status: dict | None) -> dict:
    """给无鉴权 /api/health 用：只留开关和有无凭证，不回账号、路径、登录选择器。"""
    raw = dict(status or {})
    keep_alive = raw.get("keepAlive")
    if isinstance(keep_alive, dict):
        keep_alive = {
            key: keep_alive.get(key)
            for key in (
                "enabled", "running", "window", "inWindow", "warmed",
                "intervalSeconds", "lastOk", "lastError", "lastSkip",
            )
        }
    return {
        "enabled": bool(raw.get("enabled")),
        "running": bool(raw.get("running")),
        "claimsJobs": bool(raw.get("claimsJobs")),
        "workerId": bool(raw.get("workerId")),
        "hasUsername": bool(raw.get("username") or raw.get("hasUsername")),
        "hasPassword": bool(raw.get("hasPassword")),
        "hasTotp": bool(raw.get("hasTotp")),
        "hasStorageState": bool(raw.get("hasStorageState")),
        "playwright": bool(raw.get("playwright")),
        "browserOpen": bool(raw.get("browserOpen")),
        "headless": raw.get("headless"),
        "lastError": raw.get("lastError") or "",
        "keepAlive": keep_alive,
    }
