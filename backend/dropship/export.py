# -*- coding: utf-8 -*-
"""把代发未安排写成当日 YYMMDD-代发.xlsx。收货明文只进工作簿，不进日志。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..erp.errors import ErpError
from .page import _list_frame, ensure_epaas_order_page, filter_unscheduled_dropship
from .products import apply_sku_facts, default_env_path, fetch_sku_facts, missing_sku_ids
from .workbook import dropship_output_path, write_dropship_cutoff, write_dropship_workbook

EXTRACT_JS = r"""() => {
  const text = (value) => String(value == null ? '' : value).trim();
  const itemsOf = (row) => {
    if (!row) return [{}];
    if (Array.isArray(row.items) && row.items.length) return row.items;
    if (Array.isArray(row.its) && row.its.length) return row.its;
    return [{}];
  };
  const pickPrice = (item) => {
    if (item.price != null && item.price !== '') return item.price;
    if (item.base_price != null && item.base_price !== '') return item.base_price;
    if (item.sale_price != null && item.sale_price !== '') return item.sale_price;
    return '';
  };
  const pickCost = (item, row) => {
    if (item.cost_price != null && item.cost_price !== '') return item.cost_price;
    if (item.cost != null && item.cost !== '') return item.cost;
    if (item.sku_cost != null && item.sku_cost !== '') return item.sku_cost;
    if (row && row._sku_cost != null && row._sku_cost !== '' && itemsOf(row).length === 1) {
      return row._sku_cost;
    }
    return '';
  };
  const rows = (window.jTable && window.jTable.Data && window.jTable.Data.datas) || [];
  const dropship = rows.filter((row) => String(row.question_type || '') === '代发订单未安排');
  const firstItems = itemsOf(dropship[0]);
  const itemKeys = Object.keys(firstItems[0] || {});
  const orderKeys = Object.keys(dropship[0] || {});
  const occupancy = {};
  ['l_id', 'plat_l_id', 'logistics_company', '_sku_cost', 'cus_name'].forEach((key) => {
    occupancy[key] = dropship.filter((row) => text(row[key]) && text(row[key]) !== '0').length;
  });
  occupancy.item_price = dropship.reduce((n, row) => n + itemsOf(row).filter((item) => pickPrice(item) !== '').length, 0);
  occupancy.item_cost = dropship.reduce((n, row) => n + itemsOf(row).filter((item) => pickCost(item, row) !== '').length, 0);
  occupancy.item_supplier = dropship.reduce((n, row) => (
    n + itemsOf(row).filter((item) => text(item.supplier_name || item.supplier)).length
  ), 0);
  return {
    itemKeys: itemKeys.slice(0, 60),
    orderKeys: orderKeys.slice(0, 80),
    occupancy,
    orders: dropship.map((row) => ({
      o_id: text(row.o_id),
      so_id: text(row.so_id || row.raw_so_id || row.outer_so_id),
      shop_name: text(row.shop_name),
      shop_short: text(row.shop_short_name || row.shop_nick || ''),
      shop_group: text(row.shop_group || row.sellerGroup || row.seller_group),
      shop_account: text(row.shop_owner || row.shop_account || ''),
      order_type: text(row.type).split(',')[0],
      shop_site: text(row.shop_site),
      pay_date: text(row.pay_date || row.order_date),
      buyer_message: text(row.buyer_message),
      remark: text(row.remark),
      logistics: text(row.logistics_company),
      l_id: text(row.l_id),
      plat_l_id: text(row.plat_l_id),
      multi_l_id: text(row.multiWaybillLid),
      purchase_lid: text(row.purchase_lid),
      receiver_state: text(row.receiver_state),
      receiver_city: text(row.receiver_city),
      receiver_district: text(row.receiver_district),
      receiver_town: text(row.receiver_town),
      receiver_name: text(row.receiver_name),
      receiver_mobile: text(row.receiver_mobile),
      receiver_address: text(row.receiver_address),
      shop_id: row.shop_id,
      raw_so_id: text(row.raw_so_id || row.so_id),
      co_id: row.co_id,
      isSecEn: typeof window.IsSecEnShopSite === 'function' ? window.IsSecEnShopSite(row.shop_site) : false,
      items: itemsOf(row).map((item) => ({
        sku: text(item.sku_id || item.sku),
        style: text(item.i_id),
        name: text(item.name || item.sku_name || row._standard_sku_name),
        props: text(item.properties_value || item.properties || item.prop),
        qty: item.qty != null ? item.qty : (item.qty_item != null ? item.qty_item : ''),
        price: pickPrice(item),
        cost: pickCost(item, row),
        supplier: text(item.supplier_name || item.supplier),
        supplier_sku: text(item.supplier_sku_id || item.supplier_sku),
        supplier_style: text(item.supplier_i_id || item.supplier_style),
      })),
    })),
  };
}"""

RELOAD_JS = r"""(oid) => {
  const text = (value) => String(value == null ? '' : value).trim();
  const normalize = (value) => {
    let source = value;
    if (source === 'searchfrequently') return { rateLimited: true };
    if (Array.isArray(source)) source = source[0];
    if (typeof source === 'string') {
      try { source = JSON.parse(source); } catch (e) { return null; }
    }
    return source && (source.data || source);
  };
  if (typeof window._CallPage !== 'function') return { o_id: oid, error: 'no-call' };
  const raw = window._CallPage('ReloadOrdersV2', String(oid), true);
  if (raw === 'searchfrequently') return { o_id: oid, rateLimited: true };
  const order = normalize(raw);
  if (!order) return { o_id: oid, error: 'empty' };
  const items = Array.isArray(order.items) ? order.items : (Array.isArray(order.its) ? order.its : []);
  return {
    o_id: text(order.o_id || oid),
    l_id: text(order.l_id),
    plat_l_id: text(order.plat_l_id),
    logistics: text(order.logistics_company),
    itemKeys: Object.keys(items[0] || {}).slice(0, 60),
    items: items.map((item) => ({
      sku: text(item.sku_id || item.sku),
      style: text(item.i_id),
      name: text(item.name || item.sku_name),
      props: text(item.properties_value || item.properties || item.prop),
      qty: item.qty != null ? item.qty : '',
      price: item.price != null && item.price !== '' ? item.price : (item.base_price != null ? item.base_price : ''),
      cost: item.cost_price != null && item.cost_price !== '' ? item.cost_price : (item.cost != null ? item.cost : ''),
      supplier: text(item.supplier_name || item.supplier),
      supplier_sku: text(item.supplier_sku_id || item.supplier_sku),
      supplier_style: text(item.supplier_i_id || item.supplier_style),
    })),
  };
}"""

GET_SKU_JS = r"""(sku) => {
  const text = (value) => String(value == null ? '' : value).trim();
  if (typeof window._CallPage !== 'function') return { sku, error: 'no-call' };
  const raw = window._CallPage('GetSku', String(sku));
  if (raw === 'searchfrequently') return { sku, rateLimited: true };
  const item = raw && typeof raw === 'object' ? (raw.data || raw) : null;
  if (!item || typeof item !== 'object') return { sku, error: 'empty' };
  return {
    sku: text(item.sku_id || sku),
    style: text(item.i_id),
    name: text(item.name),
    supplier: text(item.supplier_name || item.supplier),
    supplier_sku: text(item.supplier_sku_id),
    supplier_style: text(item.supplier_i_id),
    cost: item.cost_price != null && item.cost_price !== '' ? item.cost_price : '',
  };
}"""

UNMASK_JS = r"""async (spec) => {
  const text = (value) => String(value == null ? '' : value).trim();
  const usable = (value, kind) => {
    const raw = text(value);
    if (!raw || /\*|＊/.test(raw) || raw.includes('****')) return false;
    if (kind === 'mobile') return (raw.match(/\d/g) || []).length >= 11;
    return raw.length >= 1;
  };
  const asText = (raw) => {
    if (raw == null || raw === '' || raw === 'searchfrequently') return raw;
    if (typeof raw === 'number' || typeof raw === 'boolean') return String(raw);
    if (typeof raw === 'string') {
      try {
        const parsed = JSON.parse(raw);
        if (typeof parsed === 'number' || typeof parsed === 'string') return String(parsed);
      } catch (e) {}
      return raw;
    }
    return '';
  };
  const rows = (window.jTable && window.jTable.Data && window.jTable.Data.datas) || [];
  const row = rows.find((item) => String(item.o_id) === String(spec.o_id));
  if (!row) return { o_id: spec.o_id, error: 'not-in-page' };
  const site = String(row.shop_site || '');
  const wantName = spec.wantName !== false;
  const wantMobile = spec.wantMobile !== false;
  const wantStreet = spec.wantStreet !== false;
  const result = { o_id: String(row.o_id), shop_site: site, method: '' };
  try {
    if (site === '拼多多') {
      result.method = 'pdd';
      if (!row.shopSessionUid) {
        try { row.shopSessionUid = window._CallPage('GetShopSessionUid', row.shop_id); } catch (e) {}
      }
      if (wantName) {
        try { result.name = text(window.DePinDuoDuoReceiverName(row)); } catch (e) { result.nameError = String(e); }
      }
      if (wantMobile) {
        try { result.mobile = text(window.DePinDuoDuoReceiverMobile(row)); } catch (e) { result.mobileError = String(e); }
      }
      if (wantStreet) {
        try { result.street = text(window.DePinDuoDuoReceiverAddress(row)); } catch (e) { result.streetError = String(e); }
      }
    } else if (typeof window.IsSecEnShopSite === 'function' && window.IsSecEnShopSite(site) && site !== '头条放心购') {
      result.method = 'tb';
      const raw = window._CallPage('ShowReceiverInfo', row.o_id, 'all', false);
      if (raw === 'searchfrequently') return { o_id: result.o_id, shop_site: site, method: 'tb', rateLimited: true };
      let oaid = null;
      try { oaid = (typeof raw === 'string' ? JSON.parse(raw) : raw).oaid; } catch (e) {}
      if (oaid && window.top && typeof window.top.GetTopDataByBX === 'function') {
        const decrypted = await new Promise((resolve) => {
          let done = false;
          const timer = setTimeout(() => { if (!done) resolve(null); }, 12000);
          try {
            window.top.GetTopDataByBX(row, oaid, function (item) {
              done = true;
              clearTimeout(timer);
              resolve(item || null);
            }, '', 'receiver_info');
          } catch (error) {
            done = true;
            clearTimeout(timer);
            resolve(null);
          }
        });
        if (decrypted) {
          result.name = text(decrypted.name);
          result.mobile = text(decrypted.mobile);
          result.street = text(decrypted.address_detail);
          result.state = text(decrypted.state);
          result.city = text(decrypted.city);
          result.district = text(decrypted.district);
          result.town = text(decrypted.town);
        }
      }
    } else {
      result.method = 'fields';
      if (wantName) result.name = asText(window._CallPage('ShowReceiverName', row.o_id, false));
      if (wantMobile) result.mobile = asText(window._CallPage('ShowReceiverMobile', row.o_id, false));
      if (wantStreet) result.street = asText(window._CallPage('ShowReceiverAddress', row.o_id, false));
      if (result.name === 'searchfrequently' || result.mobile === 'searchfrequently' || result.street === 'searchfrequently') {
        return { o_id: result.o_id, shop_site: site, method: 'fields', rateLimited: true };
      }
    }
  } catch (error) {
    result.error = String(error);
  }
  result.usableName = usable(result.name, 'name');
  result.usableMobile = usable(result.mobile, 'mobile');
  result.usableStreet = usable(result.street, 'street');
  return result;
}"""


def _starred(value) -> bool:
    text = str(value or "").strip()
    return (not text) or ("*" in text) or ("＊" in text) or ("****" in text)


def _usable(value, kind: str) -> bool:
    text = str(value or "").strip()
    if _starred(text):
        return False
    if kind == "mobile":
        return sum(ch.isdigit() for ch in text) >= 11
    return len(text) >= 1


def _full_address(state, city, district, town, street) -> str:
    prefix = "".join(str(part or "") for part in (state, city, district, town))
    street = str(street or "").strip()
    if prefix and street.startswith(prefix):
        return street
    return f"{prefix}{street}"


def _as_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    try:
        return int(text) if text.isdigit() else float(text)
    except ValueError:
        return None


def _tracking(order: dict) -> str:
    for key in ("l_id", "multi_l_id", "plat_l_id", "purchase_lid"):
        text = str(order.get(key) or "").strip()
        if text and text != "0":
            return text
    return ""


def _blank(value) -> bool:
    if value is None:
        return True
    return str(value).strip() == ""


def _blank_id(value) -> bool:
    text = str(value or "").strip()
    return text == "" or text == "0"


def _merge_item(base: dict, extra: dict) -> dict:
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if key == "itemKeys":
            continue
        if _blank(merged.get(key)) and not _blank(value):
            merged[key] = value
    return merged


def _merge_reloaded(order: dict, extra: dict) -> dict:
    if not extra or extra.get("rateLimited") or extra.get("error"):
        return order
    if extra.get("l_id") and _blank_id(order.get("l_id")):
        order["l_id"] = extra.get("l_id")
    if extra.get("plat_l_id") and _blank_id(order.get("plat_l_id")):
        order["plat_l_id"] = extra.get("plat_l_id")
    if extra.get("logistics") and _blank(order.get("logistics")):
        order["logistics"] = extra.get("logistics")
    extras = list(extra.get("items") or [])
    if not extras:
        return order
    current = list(order.get("items") or [])
    if not current:
        order["items"] = extras
        return order
    by_sku = {}
    for item in extras:
        sku = str(item.get("sku") or "").strip()
        if sku:
            by_sku.setdefault(sku, item)
    order["items"] = [
        _merge_item(item, by_sku.get(str(item.get("sku") or "").strip()) or {})
        for item in current
    ]
    return order


def _needs_reload(order: dict) -> bool:
    for item in order.get("items") or [{}]:
        if _blank(item.get("price")):
            return True
    return False


def rows_from_orders(orders: list[dict], unmasked: dict[str, dict]) -> list[dict]:
    """订单 + 揭开结果 → Excel 行（一 SKU 一行，对齐 8.15）。"""
    lines = []
    for order in orders:
        oid = str(order.get("o_id") or "")
        opened = unmasked.get(oid) or {}
        name = opened.get("name") if _usable(opened.get("name"), "name") else order.get("receiver_name")
        mobile = opened.get("mobile") if _usable(opened.get("mobile"), "mobile") else order.get("receiver_mobile")
        street = opened.get("street") if _usable(opened.get("street"), "street") else order.get("receiver_address")
        state = opened.get("state") or order.get("receiver_state")
        city = opened.get("city") or order.get("receiver_city")
        district = opened.get("district") or order.get("receiver_district")
        town = opened.get("town") or order.get("receiver_town")
        address = _full_address(state, city, district, town, street)
        items = list(order.get("items") or [{}])
        for item in items:
            lines.append({
                "内部订单号": oid,
                "线上订单号": order.get("so_id") or "",
                "店铺名称": order.get("shop_name") or "",
                "店铺简称": order.get("shop_short") or "",
                "店铺分组": order.get("shop_group") or "",
                "商品编码": item.get("sku") or "",
                "店铺主账号": order.get("shop_account") or "",
                "订单类型": order.get("order_type") or "",
                "平台站点": order.get("shop_site") or "",
                "付款日期": order.get("pay_date") or "",
                "收货人": name or "",
                "省份": state or "",
                "城市": city or "",
                "区县": district or "",
                "地址(包含省市区)": address,
                "手机": mobile or "",
                "买家留言": order.get("buyer_message") or "",
                "订单备注": order.get("remark") or "",
                "供应商": item.get("supplier") or "",
                "标准商品名": item.get("name") or "",
                "供应商款号": item.get("supplier_style") or "",
                "供应商商品编码": item.get("supplier_sku") or "",
                "颜色及规格": item.get("props") or "",
                "快递公司": order.get("logistics") or "",
                "快递单号": _tracking(order),
                "数量": _as_number(item.get("qty")),
                "商品裸价": _as_number(item.get("price")),
                "成本价": _as_number(item.get("cost")),
            })
    return lines


def receiver_rest_complete(rows: list[dict], rate_limited) -> bool:
    """限流单之外，其余订单的收货人/手机/地址是否都揭开了。"""
    limited = {str(item).strip() for item in (rate_limited or []) if str(item).strip()}
    by_oid: dict[str, dict[str, bool]] = {}
    for row in rows:
        oid = str(row.get("内部订单号") or "").strip()
        if not oid or oid in limited:
            continue
        flags = by_oid.setdefault(oid, {"name": True, "mobile": True, "address": True})
        flags["name"] = flags["name"] and _usable(row.get("收货人"), "name")
        flags["mobile"] = flags["mobile"] and _usable(row.get("手机"), "mobile")
        flags["address"] = flags["address"] and _usable(row.get("地址(包含省市区)"), "text")
    return bool(by_oid) and all(all(flags.values()) for flags in by_oid.values())


def fill_stats(rows: list[dict]) -> dict:
    def count(key, kind="text"):
        if kind == "number":
            return sum(1 for row in rows if _as_number(row.get(key)) is not None)
        return sum(1 for row in rows if _usable(row.get(key), kind))

    return {
        "lines": len(rows),
        "orders": len({str(row.get("内部订单号") or "") for row in rows if row.get("内部订单号")}),
        "收货人": count("收货人"),
        "手机": count("手机", "mobile"),
        "地址": count("地址(包含省市区)"),
        "供应商": count("供应商"),
        "供应商款号": count("供应商款号"),
        "快递单号": count("快递单号"),
        "商品裸价": count("商品裸价", "number"),
        "成本价": count("成本价", "number"),
    }


def public_export_result(payload: dict) -> dict:
    """给 Agent / 日志用的导出摘要，不含收货明文。"""
    stats = payload.get("stats") or {}
    return {
        "ok": True,
        "filename": payload.get("filename"),
        "path": payload.get("path"),
        "dataCount": payload.get("dataCount"),
        "orders": stats.get("orders"),
        "lines": stats.get("lines"),
        "收货人": stats.get("收货人"),
        "手机": stats.get("手机"),
        "地址": stats.get("地址"),
        "供应商": stats.get("供应商"),
        "供应商款号": stats.get("供应商款号"),
        "商品裸价": stats.get("商品裸价"),
        "成本价": stats.get("成本价"),
        "快递单号": stats.get("快递单号"),
        "rateLimited": list(payload.get("rateLimited") or []),
        "restComplete": bool(payload.get("restComplete")),
        "dataCutoff": payload.get("dataCutoff") or "",
    }


def export_today_dropship(runtime: Any, *, path=None, root=None, env_path=None) -> dict:
    """打开 epaas 代发池、揭开收货、写入当日工作簿。"""
    if runtime is None:
        raise ErpError("ERP Digital Worker 未装配。请先 scripts/run_erp_worker.py login")
    run_browser = getattr(runtime, "run_browser", None)
    if not callable(run_browser):
        raise ErpError("DigitalRuntime 没有 run_browser")
    payload = run_browser(_export_on_page)
    orders = list(payload.get("orders") or [])
    sku_ids = []
    style_ids = []
    for order in orders:
        for item in order.get("items") or []:
            if item.get("sku"):
                sku_ids.append(item.get("sku"))
            if item.get("style"):
                style_ids.append(item.get("style"))
    facts = fetch_sku_facts(sku_ids, style_ids, env_path=env_path or default_env_path(root))
    apply_sku_facts(orders, facts)
    sku_lookups = 0
    sku_limited = []
    need_sku = missing_sku_ids(orders)
    if need_sku:
        extra = run_browser(_fetch_skus_on_page, need_sku)
        sku_lookups = int(extra.get("lookedUp") or 0)
        sku_limited = list(extra.get("rateLimited") or [])
        apply_sku_facts(orders, extra.get("facts") or {})
    leftover = [order for order in orders if _needs_reload(order)]
    reloaded = 0
    reload_limited = []
    if leftover:
        extra = run_browser(_reload_missing, leftover)
        reloaded = int(extra.get("reloaded") or 0)
        reload_limited = list(extra.get("rateLimited") or [])
        by_oid = {str(item.get("o_id") or ""): item for item in extra.get("orders") or []}
        for order in orders:
            _merge_reloaded(order, by_oid.get(str(order.get("o_id") or "")) or {})
        apply_sku_facts(orders, facts)
    rows = rows_from_orders(orders, payload.get("unmasked") or {})
    target = Path(path) if path else dropship_output_path(root=root)
    try:
        write_dropship_workbook(rows, target)
    except PermissionError:
        target = target.with_name(target.stem + "-订单" + target.suffix)
        write_dropship_workbook(rows, target)
    stats = fill_stats(rows)
    limited = list(payload.get("rateLimited") or [])
    rest_ok = receiver_rest_complete(rows, limited)
    cutoff = write_dropship_cutoff(
        target, rate_limited=limited, stats=stats, rest_complete=rest_ok,
    )
    return {
        "ok": True,
        "path": str(target),
        "filename": target.name if hasattr(target, "name") else str(target),
        "dataCutoff": cutoff,
        "shopSites": payload.get("shopSites") or {},
        "itemKeys": payload.get("itemKeys") or [],
        "orderKeys": payload.get("orderKeys") or [],
        "occupancy": payload.get("occupancy") or {},
        "skuFacts": len(facts),
        "skuLookups": sku_lookups,
        "skuLimited": sku_limited,
        "reloaded": reloaded,
        "reloadLimited": reload_limited,
        "rateLimited": payload.get("rateLimited") or [],
        "restComplete": rest_ok,
        "failed": payload.get("failed") or [],
        "dataCount": payload.get("dataCount"),
        "stats": stats,
    }


def _merged_receiver(order: dict, opened: dict | None) -> dict:
    opened = opened or {}
    name = opened.get("name") if _usable(opened.get("name"), "name") else order.get("receiver_name")
    mobile = opened.get("mobile") if _usable(opened.get("mobile"), "mobile") else order.get("receiver_mobile")
    street = opened.get("street") if _usable(opened.get("street"), "street") else order.get("receiver_address")
    return {"name": name, "mobile": mobile, "street": street}


def _still_starred(order: dict, opened: dict | None) -> dict:
    current = _merged_receiver(order, opened)
    return {
        "wantName": not _usable(current["name"], "name"),
        "wantMobile": not _usable(current["mobile"], "mobile"),
        "wantStreet": not _usable(current["street"], "street"),
    }


def _is_taobao(order: dict) -> bool:
    site = str(order.get("shop_site") or "")
    return bool(order.get("isSecEn")) and site != "头条放心购" and site != "拼多多"


def _unmask_one(frame, order: dict, opened: dict | None, *, delay_ms: int) -> dict:
    wants = _still_starred(order, opened)
    if not any(wants.values()):
        return {"opened": opened or {}, "rateLimited": False, "skipped": True}
    if delay_ms:
        frame.wait_for_timeout(delay_ms)
    spec = {"o_id": order.get("o_id"), **wants}
    result = frame.evaluate(UNMASK_JS, spec) or {}
    if opened:
        for key in ("name", "mobile", "street", "state", "city", "district", "town", "method"):
            if not result.get(key) and opened.get(key):
                result[key] = opened[key]
    return {"opened": result, "rateLimited": bool(result.get("rateLimited")), "skipped": False}


def _export_on_page(page) -> dict:
    ready = ensure_epaas_order_page(page)
    frame = _list_frame(page)
    if frame is None:
        raise ErpError("epaas 外壳里没有订单列表 iframe")
    filtered = filter_unscheduled_dropship(frame)
    extracted = frame.evaluate(EXTRACT_JS) or {}
    if not (extracted.get("orders") or []):
        frame.wait_for_timeout(5000)
        extracted = frame.evaluate(EXTRACT_JS) or {}
    orders = list(extracted.get("orders") or [])
    unmasked = {}
    rate_limited = []
    failed = []

    def run_pass(targets: list[dict], delay_ms: int) -> None:
        for order in targets:
            oid = order.get("o_id") or ""
            try:
                outcome = _unmask_one(frame, order, unmasked.get(oid), delay_ms=delay_ms)
            except Exception as exc:
                failed.append({"o_id": oid, "error": type(exc).__name__})
                continue
            if outcome.get("skipped"):
                continue
            opened = outcome.get("opened") or {}
            if outcome.get("rateLimited"):
                if oid not in rate_limited:
                    rate_limited.append(oid)
                continue
            unmasked[oid] = opened
            if oid in rate_limited:
                rate_limited.remove(oid)
            if opened.get("error"):
                failed.append({"o_id": oid, "error": "unmask"})

    easy = [order for order in orders if not _is_taobao(order)]
    taobao = [order for order in orders if _is_taobao(order)]
    run_pass(easy, 800)
    run_pass(taobao, 5000)
    leftover = [
        order for order in orders
        if any(_still_starred(order, unmasked.get(order.get("o_id") or "")).values())
    ]
    if leftover:
        frame.wait_for_timeout(20000)
        run_pass(leftover, 6000)
        leftover = [
            order for order in leftover
            if any(_still_starred(order, unmasked.get(order.get("o_id") or "")).values())
        ]
        rate_limited = [order.get("o_id") for order in leftover if order.get("o_id")]
    listing = filtered.get("list") or {}
    return {
        "orders": orders,
        "unmasked": unmasked,
        "itemKeys": extracted.get("itemKeys") or [],
        "orderKeys": extracted.get("orderKeys") or [],
        "occupancy": extracted.get("occupancy") or {},
        "shopSites": listing.get("shopSites") or {},
        "hasGetTop": ready.get("hasGetTop"),
        "rateLimited": rate_limited,
        "failed": failed,
        "dataCount": listing.get("dataCount"),
        "dropshipCount": listing.get("dropshipCount") or len(orders),
    }


def _fetch_skus_on_page(page, sku_ids: list[str]) -> dict:
    frame = _list_frame(page)
    if frame is None:
        raise ErpError("epaas 外壳里没有订单列表 iframe")
    facts = {}
    limited = []
    for sku in sku_ids:
        if not sku:
            continue
        frame.wait_for_timeout(200)
        extra = frame.evaluate(GET_SKU_JS, sku) or {}
        if extra.get("rateLimited"):
            limited.append(sku)
            break
        if extra.get("error"):
            continue
        facts[str(extra.get("sku") or sku)] = {
            "name": extra.get("name") or "",
            "supplier": extra.get("supplier") or "",
            "supplier_sku": extra.get("supplier_sku") or "",
            "supplier_style": extra.get("supplier_style") or "",
            "cost": extra.get("cost"),
        }
    return {"facts": facts, "lookedUp": len(facts), "rateLimited": limited}


def _reload_missing(page, orders: list[dict]) -> dict:
    frame = _list_frame(page)
    if frame is None:
        raise ErpError("epaas 外壳里没有订单列表 iframe")
    filled = []
    limited = []
    item_keys = []
    for order in orders:
        oid = str(order.get("o_id") or "")
        if not oid:
            continue
        frame.wait_for_timeout(250)
        extra = frame.evaluate(RELOAD_JS, oid) or {}
        if extra.get("rateLimited"):
            limited.append(oid)
            continue
        if extra.get("itemKeys") and not item_keys:
            item_keys = extra.get("itemKeys")
        filled.append(extra)
    return {
        "orders": filled,
        "reloaded": len(filled),
        "rateLimited": limited,
        "itemKeys": item_keys,
    }
