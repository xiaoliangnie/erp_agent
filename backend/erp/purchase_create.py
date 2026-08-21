# -*- coding: utf-8 -*-
"""在采购单编辑页调用页面自己的 Confirm，写成待审核采购单。"""
from __future__ import annotations

import json
import time

from .errors import ErpError, ErpUnknownResult

PURCHASE_URL = (
    "https://www.erp321.com/app/scm/purchase/purchasemode.aspx"
    "?_c=jst-epaas&epaas=true&owner_co_id={owner}&authorize_co_id={owner}"
)
EPAAS_URL = "https://www.erp321.com/epaas"

OPEN_PURCHASE_JS = """(url) => {
  const frames = Array.from(document.querySelectorAll('iframe'));
  const home = frames.find((el) => /erp-home|src\\.erp321|erp-web-group\\/home/i.test(el.src || ''))
    || frames[0];
  if (!home) return { ok: false, reason: 'no-iframe', count: frames.length };
  home.src = url;
  return { ok: true, src: home.src };
}"""

FILL_SAVE_JS = """(payload) => new Promise((resolve) => {
  const $ = window.$;
  if (!$) { resolve({ ok: false, error: '编辑页没有 jQuery' }); return; }
  let lastMsg = '';
  const oldMsg = $.msg;
  $.msg = function (text) {
    lastMsg = String(text == null ? '' : text);
    return oldMsg.apply(this, arguments);
  };
  $('#seller').val(payload.seller || '');
  $('#seller_id').val(payload.sellerId || '');
  if (payload.poDate) {
    $('#po_date').val(payload.poDate).trigger('change');
  }
  $('#purchaser_name').val(payload.purchaserName || '').trigger('change');
  if (payload.paymentMethod) $('#payment_method').val(payload.paymentMethod).trigger('change');
  if (payload.taxRate !== undefined && payload.taxRate !== null && payload.taxRate !== '') {
    $('#tax_rate').val(String(payload.taxRate)).trigger('change');
  }
  if (payload.remark) $('#remark').val(payload.remark);
  if (payload.arriveDate) $('#arrive_date').val(payload.arriveDate).trigger('change');
  if (payload.wmsCoId) {
    $('#wms_co_id').val(String(payload.wmsCoId));
    if ($('#wms_co_ids').length) $('#wms_co_ids').val(payload.wmsCoName || '');
  }
  const rows = (payload.items || []).map((item) => ({
    sku_id: item.sku,
    qty: item.qty,
    price: item.price,
    name: item.name || '',
    properties_value: item.spec || '',
    i_id: item.styleId || '',
    remark: item.remark || '',
    priceFromSource: 'HandFill',
  }));
  if (typeof ParseItems === 'function') {
    ParseItems(JSON.stringify(rows));
  }
  const itemArray = rows.map((item) => ({
    sku_id: item.sku_id,
    qty: item.qty,
    price: item.price,
    remark: item.remark || '',
    piceFromSource: 'HandFill',
  }));
  if (typeof _ACP !== 'function') {
    resolve({ ok: false, error: '编辑页没有 _ACP' });
    return;
  }
  const timer = setTimeout(() => {
    resolve({ ok: false, error: lastMsg || '保存超时，ERP 没有回结果' });
  }, 28000);
  _ACP('Confirm', function (rv) {
    clearTimeout(timer);
    if (!rv) {
      resolve({ ok: false, error: lastMsg || 'ERP 拒绝保存' });
      return;
    }
    let parsed = rv;
    if (typeof rv === 'string') {
      try { parsed = JSON.parse(rv); } catch (e) { parsed = { raw: rv }; }
    }
    resolve({ ok: true, result: parsed, lastMsg: lastMsg });
  }, JSON.stringify(itemArray), String(payload.sellerId || ''), JSON.stringify([]), 'false');
})"""


def _purchase_frame(page):
    for frame in getattr(page, "frames", []) or []:
        href = str(getattr(frame, "url", "") or "").lower()
        if "purchasemode.aspx" in href:
            return frame
    return None


def _editor_frame(page):
    for frame in getattr(page, "frames", []) or []:
        href = str(getattr(frame, "url", "") or "").lower()
        if "purchase/editor.aspx" in href:
            return frame
    return None


def _open_editor(page, owner_co_id: str) -> None:
    href = str(getattr(page, "url", "") or "")
    if "login.aspx" in href.lower():
        raise ErpError("打开 epaas 被转到登录页，登录态已失效")
    if "erp321.com/epaas" not in href.lower():
        page.goto(EPAAS_URL, wait_until="domcontentloaded", timeout=60000)
        href = str(page.url or "")
        if "login.aspx" in href.lower():
            raise ErpError("打开 epaas 被转到登录页，登录态已失效")
    opened = page.evaluate(OPEN_PURCHASE_JS, PURCHASE_URL.format(owner=owner_co_id))
    if not opened or not opened.get("ok"):
        raise ErpError("epaas 没有可嵌采购单的 iframe")
    deadline = time.monotonic() + 20
    purchase = None
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        purchase = _purchase_frame(page)
        if purchase is not None:
            break
    if purchase is None:
        raise ErpError("采购单管理页没有打开")
    purchase.wait_for_function("() => typeof window.New === 'function'", timeout=15000)
    purchase.evaluate("() => New('po')")
    deadline = time.monotonic() + 20
    editor = None
    while time.monotonic() < deadline:
        page.wait_for_timeout(400)
        editor = _editor_frame(page)
        if editor is not None:
            break
    if editor is None:
        raise ErpError("采购单编辑页没有打开")
    editor.wait_for_function(
        "() => typeof window._ACP === 'function' && typeof window.$ === 'function' && document.getElementById('seller_id')",
        timeout=15000,
    )


def _po_id_from_result(result) -> str:
    if isinstance(result, dict):
        for key in ("po_id", "poId", "id"):
            text = str(result.get(key) or "").strip()
            if text.isdigit():
                return text
        data = result.get("data")
        if isinstance(data, dict):
            text = str(data.get("po_id") or data.get("poId") or "").strip()
            if text.isdigit():
                return text
    return ""


def create_purchase_order(page, payload: dict, *, owner_co_id: str) -> dict:
    """打开手工下单编辑页，填单头和明细，调用页面 Confirm。不点审核。"""
    items = [
        item for item in (payload.get("items") or [])
        if str((item or {}).get("sku") or "").strip() and float((item or {}).get("qty") or 0) > 0
    ]
    if not items:
        raise ErpError("没有可写入的明细")
    seller_id = str(payload.get("sellerId") or "").strip()
    if not seller_id:
        raise ErpError("缺少供应商编号")
    _open_editor(page, owner_co_id)
    editor = _editor_frame(page)
    if editor is None:
        raise ErpError("找不到采购单编辑 iframe")
    job = {
        "seller": payload.get("seller") or "",
        "sellerId": seller_id,
        "poDate": payload.get("poDate") or "",
        "purchaserName": payload.get("purchaserName") or "",
        "paymentMethod": payload.get("paymentMethod") or "",
        "taxRate": payload.get("taxRate"),
        "remark": payload.get("remark") or "",
        "arriveDate": payload.get("arriveDate") or "",
        "wmsCoId": payload.get("wmsCoId") or "",
        "wmsCoName": payload.get("wmsCoName") or "",
        "items": items,
    }
    try:
        reply = editor.evaluate(FILL_SAVE_JS, job)
    except Exception as exc:
        raise ErpUnknownResult(f"采购单保存结果未知：{exc}") from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise ErpError(str((reply or {}).get("error") or "ERP 保存失败"))
    po_id = _po_id_from_result(reply.get("result"))
    if not po_id:
        raise ErpUnknownResult("ERP 已回保存，但没有采购单号，不能当成功")
    return {
        "ok": True,
        "command": "erp.create_purchase_order",
        "poId": po_id,
        "result": reply.get("result"),
    }
