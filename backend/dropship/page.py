# -*- coding: utf-8 -*-
"""代发订单列表页：嵌进 epaas，只勾「代发订单未安排」后 FullSearch。"""
from __future__ import annotations

from ..erp.errors import ErpError

EPAAS_URL = "https://www.erp321.com/epaas"
LIST_URL = "https://www.erp321.com/app/order/order/list.aspx?_c=jst-epaas&epaas=true"
QUESTION_TYPE = "代发订单未安排"

OPEN_LIST_JS = """(url) => {
  const frames = Array.from(document.querySelectorAll('iframe'));
  const home = frames.find((el) => /erp-home|src\\.erp321|erp-web-group\\/home/i.test(el.src || ''))
    || frames[0];
  if (!home) return { ok: false, reason: 'no-iframe', count: frames.length };
  home.src = url;
  return { ok: true, src: home.src, count: frames.length };
}"""

FILTER_JS = """() => {
  const frame = Array.from(window.frames).find((item) => {
    try { return /filter\\/filter\\.aspx/i.test(item.location && item.location.href || ''); }
    catch (e) { return false; }
  });
  if (!frame) return { ok: false, reason: 'no-frame' };
  const doc = frame.document;
  Array.from(doc.querySelectorAll('input[name=status]:checked')).forEach((el) => {
    if (el.checked) el.click();
  });
  Array.from(doc.querySelectorAll('input[name=question_type]:checked')).forEach((el) => {
    if (el.checked) el.click();
  });
  const question = doc.querySelector('input[name=question_type][value="代发订单未安排"]');
  if (question && !question.checked) question.click();
  const dp = window.jTable && window.jTable.Data && window.jTable.Data.dp;
  if (dp) dp.PageSize = 200;
  const select = document.querySelector('select[name=PageSize], select#PageSize, select.pagesize');
  if (select) {
    const option = Array.from(select.options || []).find((item) => Number(item.value) >= 200)
      || Array.from(select.options || []).at(-1);
    if (option) select.value = option.value;
  }
  if (typeof frame.FullSearch === 'function') frame.FullSearch();
  return { ok: true, questionChecked: !!(question && question.checked), pageSize: dp && dp.PageSize };
}"""

LIST_META_JS = """() => {
  const rows = (window.jTable && window.jTable.Data && window.jTable.Data.datas) || [];
  const dropship = rows.filter((row) => String(row.question_type || '') === '代发订单未安排');
  const dp = window.jTable && window.jTable.Data && window.jTable.Data.dp;
  return {
    href: location.href,
    dataCount: dp && dp.DataCount,
    pageSize: dp && dp.PageSize,
    pageIndex: dp && dp.PageIndex,
    rowCount: rows.length,
    dropshipCount: dropship.length,
    shopSites: dropship.reduce((acc, row) => {
      acc[String(row.shop_site || '')] = (acc[String(row.shop_site || '')] || 0) + 1;
      return acc;
    }, {}),
    hasGetTop: (function () {
      try { return typeof window.top.GetTopDataByBX === 'function'; }
      catch (e) { return false; }
    })(),
    oIds: dropship.map((row) => String(row.o_id || '')).filter(Boolean),
  };
}"""

def _on_epaas(href: str) -> bool:
    text = str(href or "").lower()
    return "erp321.com/epaas" in text and "login.aspx" not in text


def _list_frame(page):
    for frame in getattr(page, "frames", []) or []:
        href = str(getattr(frame, "url", "") or "")
        if "app/order/order/list" in href.lower():
            return frame
    return None


def ensure_epaas_order_page(page, *, epaas_url: str = EPAAS_URL, list_url: str = LIST_URL) -> dict:
    """登录态下打开 epaas，再把订单列表嵌进外壳 iframe。"""
    href = str(getattr(page, "url", "") or "")
    if "login.aspx" in href.lower():
        raise ErpError("打开 epaas 被转到登录页，登录态已失效")
    if not _on_epaas(href):
        page.goto(epaas_url, wait_until="domcontentloaded", timeout=60000)
        href = str(page.url or "")
        if "login.aspx" in href.lower():
            raise ErpError("打开 epaas 被转到登录页，登录态已失效")
    opened = page.evaluate(OPEN_LIST_JS, list_url)
    if not opened or not opened.get("ok"):
        raise ErpError("epaas 没有可嵌订单列表的 iframe")
    page.wait_for_timeout(8000)
    frame = _list_frame(page)
    if frame is None:
        raise ErpError("epaas 外壳里没有订单列表 iframe")
    try:
        frame.wait_for_function("() => typeof window._ACP === 'function'", timeout=20000)
    except Exception as exc:
        raise ErpError("订单列表嵌在 epaas 后没有 _ACP") from exc
    top_sdk = page.evaluate(
        "() => ({ GetTopDataByBX: typeof window.GetTopDataByBX, href: location.href })"
    )
    return {
        "ok": True,
        "href": getattr(frame, "url", ""),
        "opened": opened,
        "hasGetTop": (top_sdk or {}).get("GetTopDataByBX") == "function",
    }


def filter_unscheduled_dropship(frame) -> dict:
    """只勾「代发订单未安排」，走筛选 iframe 的 FullSearch。"""
    setup = frame.evaluate(FILTER_JS)
    if not setup or not setup.get("ok"):
        raise ErpError("订单筛选 iframe 不在，无法组合查询代发未安排")
    frame.wait_for_timeout(8000)
    listing = frame.evaluate(LIST_META_JS) or {}
    return {"setup": setup, "list": listing}
