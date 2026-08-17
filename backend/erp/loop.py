# -*- coding: utf-8 -*-
"""领取换货队列：试算和已确认执行改走后端 Playwright。"""
from __future__ import annotations

import logging
import threading
import time

from .errors import ErpError, ErpUnknownResult
from .runtime import DigitalRuntime
from .session import playwright_available

logger = logging.getLogger(__name__)


class DigitalWorkerLoop:
    """ERP 写并发固定为 1。启用后油猴不再领取换货任务，避免双写。"""

    def __init__(self, runtime: DigitalRuntime, exchange, *, poll_seconds: float = 3.0):
        self.runtime = runtime
        self.exchange = exchange
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.claims_jobs = False
        self._stop = threading.Event()
        self._thread = None
        self.last_error = ""

    @property
    def worker_id(self) -> str:
        return str(self.runtime.config.get("workerId") or "erp-ai-procurement")

    def status(self) -> dict:
        return {
            **self.runtime.status(),
            "running": bool(self._thread and self._thread.is_alive()),
            "claimsJobs": self.claims_jobs,
            "lastError": self.last_error,
        }

    def start(self) -> dict:
        if not self.runtime.config.get("enabled"):
            self.claims_jobs = False
            return self.status()
        if not playwright_available():
            self.last_error = "未安装 Playwright"
            self.claims_jobs = False
            logger.error("ERP_AI_ENABLED 但未安装 playwright，换货仍走浏览器 Worker")
            return self.status()
        self.claims_jobs = True
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return self.status()
        self._thread = threading.Thread(target=self._loop, name="erp-digital-worker", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> None:
        self.claims_jobs = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.runtime.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("ERP Digital Worker tick failed")
            self._stop.wait(self.poll_seconds)

    def _tick(self) -> None:
        self.exchange.heartbeat(self.worker_id, {
            "pageUrl": self.runtime.config.get("orderListUrl") or "",
            "version": "playwright-1",
            "ready": True,
            "detail": {"executor": "backend", "playwright": True},
        })
        job = self.exchange.next_job(self.worker_id)
        if not job:
            return
        action = job.get("action")
        if action == "plan":
            plan = self.runtime.run("erp.exchange_items", job)
            self.exchange.report_plan(job["id"], self.worker_id, plan)
            return
        if action != "execute":
            return
        try:
            result = self.runtime.run("erp.exchange_items", {**job, "confirm": True})
        except ErpUnknownResult as exc:
            self.last_error = str(exc)
            logger.error("ERP 换货结果未知，不重试：%s", exc)
            return
        except ErpError as exc:
            self.last_error = str(exc)
            self.exchange.report_result(
                job["id"], self.worker_id, job.get("executionToken") or "",
                {"succeeded": [], "failed": [], "error": str(exc)},
            )
            return
        self.exchange.report_result(
            job["id"], self.worker_id, job.get("executionToken") or "", result,
        )
