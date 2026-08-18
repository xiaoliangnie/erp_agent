# -*- coding: utf-8 -*-
"""在订单列表页注入换货核心，只调用 planJob / executeJob。"""
from __future__ import annotations

from pathlib import Path

from .errors import ErpError, ErpUnknownResult
from ..paths import ROOT

CORE_PATH = ROOT / "frontend" / "js" / "jst-order-exchange.core.js"
CORE_VERSION = "0.7.1"


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
    if ready.get("ready") and str(ready.get("version") or "") == CORE_VERSION:
        return ready
    script = core_js if core_js is not None else read_core()
    page.add_script_tag(content=script)
    ready = _core_state(page)
    if not ready.get("ready"):
        raise ErpError("换货核心已注入但 ready() 为 false")
    return ready


def load_order(page, oid: str) -> dict:
    """回读一张订单的当前明细。失败返回 load_error，不抛，由核验层决定停或 unknown。"""
    key = str(oid or "").strip()
    if not key:
        return {"o_id": "", "items": [], "load_error": "缺少内部单号"}
    return load_orders(page, [key]).get(key) or {
        "o_id": key, "items": [], "load_error": "ERP 未返回该订单",
    }


def load_orders(page, oids, *, concurrency: int = 3) -> dict:
    """一批回读，只进一次页面。失败单带 load_error，不抛。"""
    keys = []
    for raw in oids or []:
        oid = str(raw or "").strip()
        if oid and oid not in keys:
            keys.append(oid)
    if not keys:
        return {}
    width = max(1, min(8, int(concurrency or 3)))
    try:
        result = page.evaluate(
            """async (input) => {
                if (!window.JstOrderExchange) throw new Error('换货核心未注入');
                if (typeof window.JstOrderExchange.loadOrders === 'function') {
                    return await window.JstOrderExchange.loadOrders(input);
                }
                const orders = [];
                for (const oid of input.oids || []) {
                    try {
                        orders.push(await window.JstOrderExchange.loadOrder(oid));
                    } catch (error) {
                        orders.push({ o_id: oid, items: [], load_error: String(error) });
                    }
                }
                return { orders: orders, count: orders.length };
            }""",
            {"oids": keys, "concurrency": width},
        )
    except Exception as exc:
        return {
            oid: {"o_id": oid, "items": [], "load_error": str(exc)}
            for oid in keys
        }
    loaded = {}
    for item in (result or {}).get("orders") or []:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("o_id") or item.get("oId") or "").strip()
        if not oid:
            continue
        item.setdefault("o_id", oid)
        loaded[oid] = item
    for oid in keys:
        if oid not in loaded:
            loaded[oid] = {"o_id": oid, "items": [], "load_error": "ERP 未返回该订单"}
    return loaded


def search_sku(page, sku: str, *, limit: int = 500) -> dict:
    """只读扫描当前订单页数据集，反查含该 SKU 的内部单号。"""
    key = str(sku or "").strip()
    if not key:
        raise ErpError("搜索 SKU 不能为空")
    try:
        result = page.evaluate(
            """async (input) => {
                if (!window.JstOrderExchange) throw new Error('换货核心未注入');
                return await window.JstOrderExchange.searchSku(input);
            }""",
            {"sku": key, "limit": max(1, min(int(limit or 500), 500))},
        )
    except Exception as exc:
        raise ErpError(f"SKU 搜索失败：{exc}") from exc
    if not isinstance(result, dict):
        raise ErpError("SKU 搜索结果不是对象")
    return result


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


def execute_job(page, job: dict, *, delay_ms: int = 250, concurrency: int = 1) -> dict:
    """结果未知时抛 ErpUnknownResult，调用方不得重试。"""
    started = False
    payload = dict(job or {})
    plans = payload.get("plans") or (payload.get("plan") or {}).get("plans") or []
    if plans and not (payload.get("plan") or {}).get("plans"):
        payload["plan"] = {"plans": plans}
    width = max(1, min(8, int(concurrency or 1)))
    try:
        started = True
        return page.evaluate(
            """async ({job, delayMs, concurrency}) => {
                if (!window.JstOrderExchange) throw new Error('换货核心未注入');
                return await window.JstOrderExchange.executeJob(job, {
                    confirm: true,
                    delayMs: delayMs,
                    concurrency: concurrency,
                    plans: job.plans || (job.plan && job.plan.plans) || [],
                });
            }""",
            {"job": payload, "delayMs": delay_ms, "concurrency": width},
        )
    except Exception as exc:
        if started:
            raise ErpUnknownResult(f"换货写入结果未知，未重试：{exc}") from exc
        raise ErpError(f"换货执行未能开始：{exc}") from exc
