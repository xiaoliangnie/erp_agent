# -*- coding: utf-8 -*-
"""领取换货与只读队列：试算、执行、探测、搜 SKU、商品图片都走后端 Playwright。"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import exchange_page, purchase_page
from .errors import ErpError, ErpUnknownResult
from .runtime import DigitalRuntime
from .session import playwright_available

logger = logging.getLogger(__name__)


class DigitalWorkerLoop:
    """ERP 写并发固定为 1。油猴领取口已关闭，避免双写。"""

    def __init__(self, runtime: DigitalRuntime, exchange, *, images=None,
                 poll_seconds: float = 3.0):
        self.runtime = runtime
        self.exchange = exchange
        self.images = images
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
            "hasLogin": self._has_login(),
            "lastError": self.last_error,
        }

    def _has_login(self) -> bool:
        path = str(self.runtime.config.get("storageStatePath") or "")
        if path and Path(path).exists():
            return True
        return bool(
            self.runtime.config.get("username")
            and self.runtime.config.get("hasPassword")
        )

    def start(self) -> dict:
        if not self.runtime.config.get("enabled"):
            self.claims_jobs = False
            self.last_error = "ERP_AI_ENABLED 已关闭"
            return self.status()
        if not playwright_available():
            self.last_error = "未安装 Playwright"
            self.claims_jobs = False
            logger.error("ERP Digital Worker 已启用但未安装 playwright，任务停在队列")
            return self.status()
        if not self._has_login():
            self.last_error = "未配置 ERP 登录态或账号密码"
            self.claims_jobs = False
            logger.error("ERP Digital Worker 已启用但没有登录态，任务停在队列")
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
        closer = getattr(self.runtime, "close", None)
        if callable(closer):
            closer()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("ERP Digital Worker tick failed")
            self._stop.wait(self.poll_seconds)

    def _owner_co_id(self) -> str:
        return str(self.runtime.config.get("ownerCoId") or "10235039")

    def _tick(self) -> None:
        self.exchange.heartbeat(self.worker_id, {
            "pageUrl": self.runtime.config.get("orderListUrl") or "",
            "version": "playwright-1",
            "ready": True,
            "detail": {"executor": "backend", "playwright": True},
        })
        if self._tick_job():
            return
        if self._tick_probe():
            return
        if self._tick_search():
            return
        self._tick_images()

    def _tick_job(self) -> bool:
        job = self.exchange.next_job(self.worker_id)
        if not job:
            return False
        action = job.get("action")
        if action == "plan":
            plan = self.runtime.run("erp.exchange_items", job)
            self.exchange.report_plan(job["id"], self.worker_id, plan)
            return True
        if action != "execute":
            return True
        try:
            result = self.runtime.run("erp.exchange_items", {**job, "confirm": True})
        except ErpUnknownResult as exc:
            self.last_error = str(exc)
            logger.error("ERP 换货结果未知，不重试：%s", exc)
            return True
        except ErpError as exc:
            self.last_error = str(exc)
            self.exchange.report_result(
                job["id"], self.worker_id, job.get("executionToken") or "",
                {"succeeded": [], "failed": [], "error": str(exc)},
            )
            return True
        self.exchange.report_result(
            job["id"], self.worker_id, job.get("executionToken") or "", result,
        )
        return True

    def _tick_probe(self) -> bool:
        next_probe = getattr(self.exchange, "next_probe", None)
        report_probe = getattr(self.exchange, "report_probe", None)
        if not callable(next_probe) or not callable(report_probe):
            return False
        probe = next_probe(self.worker_id)
        if not probe:
            return False
        try:
            if probe.get("kind") != "purchase_items":
                raise ErpError("不支持的只读探测类型")
            result = self.runtime.run_browser(self._probe_on_page, probe.get("reference") or "")
        except Exception as exc:
            self.last_error = str(exc)
            result = {"error": str(exc)}
        report_probe(probe["id"], self.worker_id, result)
        return True

    def _probe_on_page(self, page, po_id: str) -> dict:
        rows = purchase_page.fetch_purchase_items(
            page, po_id, owner_co_id=self._owner_co_id(),
            origin=self.runtime.config.get("baseUrl") or "",
        )
        return {"poId": str(po_id), "count": len(rows), "items": rows}

    def _tick_search(self) -> bool:
        next_search = getattr(self.exchange, "next_search", None)
        report_search = getattr(self.exchange, "report_search", None)
        if not callable(next_search) or not callable(report_search):
            return False
        search = next_search(self.worker_id)
        if not search:
            return False
        try:
            result = self.runtime.run_browser(self._search_on_page, search.get("sku") or "")
        except Exception as exc:
            self.last_error = str(exc)
            result = {"error": str(exc)}
        report_search(search["id"], self.worker_id, result)
        return True

    def _search_on_page(self, page, sku: str) -> dict:
        exchange_page.ensure_order_page(page, self.runtime.config["orderListUrl"])
        return exchange_page.search_sku(page, sku)

    def _tick_images(self) -> bool:
        if self.images is None:
            return False
        job = self.images.next(self.worker_id)
        if not job:
            return False
        try:
            self.runtime.run_browser(self._images_on_page, job)
        except Exception as exc:
            self.last_error = str(exc)
            self.images.finish(
                job["id"], self.worker_id,
                {"failed": job.get("targets") or [], "error": str(exc)},
            )
        return True

    def _images_on_page(self, page, job: dict) -> dict:
        return purchase_page.sync_images(
            page, job, self.images, self.worker_id,
            owner_co_id=self._owner_co_id(),
            origin=self.runtime.config.get("baseUrl") or "",
        )
