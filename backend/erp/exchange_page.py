# -*- coding: utf-8 -*-
"""在订单列表页注入换货核心，只调用 planJob / executeJob。"""
from __future__ import annotations

from pathlib import Path

from .errors import ErpError, ErpUnknownResult
from ..paths import ROOT

CORE_PATH = ROOT / "frontend" / "js" / "jst-order-exchange.core.js"


def read_core(path: Path | None = None) -> str:
    source = Path(path or CORE_PATH)
    if not source.exists():
        raise ErpError(f"找不到换货核心脚本：{source}")
    return source.read_text(encoding="utf-8")


def _on_order_list(href: str, order_list_url: str) -> bool:
    text = str(href or "").lower()
    if not text or "login.aspx" in text:
        return False
    if "app/order/order/list" in text or "order/order/list.aspx" in text:
        return True
    target = str(order_list_url or "").lower().split("?", 1)[0]
    return bool(target and target in text)


def _core_state(page) -> dict:
    return page.evaluate(
        """() => ({
            ready: !!(window.JstOrderExchange && window.JstOrderExchange.ready()),
            version: window.JstOrderExchange && window.JstOrderExchange.version,
            hasAcp: typeof window._ACP === 'function',
            href: location.href,
        })"""
    )


def ensure_order_page(page, order_list_url: str, core_js: str | None = None) -> dict:
    """已在订单列表且核心可用时不刷新，避免每批尺码都重开 ERP 页。"""
    href = str(getattr(page, "url", "") or "")
    if "login.aspx" in href.lower():
        raise ErpError("打开订单页被转到登录页，登录态已失效")
    if not _on_order_list(href, order_list_url):
        page.goto(order_list_url, wait_until="domcontentloaded", timeout=60000)
        href = str(page.url or "")
        if "login.aspx" in href.lower():
            raise ErpError("打开订单页被转到登录页，登录态已失效")
    try:
        page.wait_for_function(
            "() => typeof window._ACP === 'function'",
            timeout=8000 if _on_order_list(href, order_list_url) else 30000,
        )
    except Exception as exc:
        raise ErpError("订单列表页没有 _ACP，页面未就绪或选择器已变") from exc
    ready = _core_state(page)
    if ready.get("ready"):
        return ready
    script = core_js if core_js is not None else read_core()
    page.add_script_tag(content=script)
    ready = _core_state(page)
    if not ready.get("ready"):
        raise ErpError("换货核心已注入但 ready() 为 false")
    return ready


def plan_job(page, job: dict) -> dict:
    try:
        return page.evaluate(
            """async (job) => {
                if (!window.JstOrderExchange) throw new Error('换货核心未注入');
                return await window.JstOrderExchange.planJob(job);
            }""",
            {"rules": job.get("rules") or {}, "targets": job.get("targets") or {}},
        )
    except Exception as exc:
        raise ErpError(f"试算失败：{exc}") from exc


def execute_job(page, job: dict, *, delay_ms: int = 250) -> dict:
    """结果未知时抛 ErpUnknownResult，调用方不得重试。"""
    started = False
    payload = dict(job or {})
    plans = payload.get("plans") or (payload.get("plan") or {}).get("plans") or []
    if plans and not (payload.get("plan") or {}).get("plans"):
        payload["plan"] = {"plans": plans}
    try:
        started = True
        return page.evaluate(
            """async ({job, delayMs}) => {
                if (!window.JstOrderExchange) throw new Error('换货核心未注入');
                return await window.JstOrderExchange.executeJob(job, {
                    confirm: true,
                    delayMs: delayMs,
                    plans: job.plans || (job.plan && job.plan.plans) || [],
                });
            }""",
            {"job": payload, "delayMs": delay_ms},
        )
    except Exception as exc:
        if started:
            raise ErpUnknownResult(f"换货写入结果未知，未重试：{exc}") from exc
        raise ErpError(f"换货执行未能开始：{exc}") from exc
