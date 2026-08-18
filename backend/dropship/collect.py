# -*- coding: utf-8 -*-
"""用换鞋垫同一套 DigitalRuntime 打开代发池。

登录、cookie（storage_state）、Playwright 线程和写锁全部复用
``backend.erp.runtime.DigitalRuntime``，不另开浏览器账号。
"""
from __future__ import annotations

from typing import Any

from ..erp.errors import ErpError
from .page import _list_frame, ensure_epaas_order_page, filter_unscheduled_dropship


def prepare_dropship_list(runtime: Any) -> dict:
    """登录保态后，在 epaas 里筛出「代发订单未安排」。

    返回单号和店铺计数，不含收货明文。揭开地址仍走页面小眼睛同款 SDK，
    下一步再写入 ``YYMMDD-代发.xlsx``。
    """
    if runtime is None:
        raise ErpError(
            "ERP Digital Worker 未装配。请先 scripts/run_erp_worker.py login，"
            "与换鞋垫共用同一套 cookie"
        )
    run_browser = getattr(runtime, "run_browser", None)
    if not callable(run_browser):
        raise ErpError("DigitalRuntime 没有 run_browser，无法打开代发列表")
    return run_browser(_prepare_on_page)


def _prepare_on_page(page) -> dict:
    ready = ensure_epaas_order_page(page)
    frame = _list_frame(page)
    if frame is None:
        raise ErpError("epaas 外壳里没有订单列表 iframe")
    filtered = filter_unscheduled_dropship(frame)
    listing = filtered.get("list") or {}
    return {
        "ok": True,
        "command": "erp.dropship_list",
        "hasGetTop": ready.get("hasGetTop"),
        "href": listing.get("href") or ready.get("href"),
        "dataCount": listing.get("dataCount"),
        "dropshipCount": listing.get("dropshipCount"),
        "shopSites": listing.get("shopSites") or {},
        "oIds": listing.get("oIds") or [],
        "setup": filtered.get("setup") or {},
    }
