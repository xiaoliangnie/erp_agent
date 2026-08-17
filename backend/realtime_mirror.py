# -*- coding: utf-8 -*-
"""通过供应链代理 API 维护本地 MySQL 实时镜像。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import mimetypes
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

from .business_time import BUSINESS_TIMEZONE, business_now
from .database import connect, is_transient_mysql_error

try:  # urllib 在部分 macOS Python 安装中找不到系统根证书。
    import certifi
except ImportError:  # pragma: no cover - requirements 已显式安装
    certifi = None

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where() if certifi else None)
logger = logging.getLogger(__name__)

_PRIVATE_IMAGE_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


PURCHASE_ORDER_TABLE = "realtime_purchase_orders"
PURCHASE_ITEM_TABLE = "realtime_purchase_order_items"
ORDER_TABLE = "realtime_orders"
ORDER_ITEM_TABLE = "realtime_order_items"
PRODUCT_TABLE = "realtime_products"
SUPPLIER_TABLE = "realtime_suppliers"
SYNC_STATE_TABLE = "realtime_sync_state"

PURCHASE_ROUTE = "/api/proxy/v1/jushuitan/purchase/orders/query"
# 订单查询接口支持修改时间分页，并在每张订单内返回商品明细及 pic 图片地址。
ORDER_ROUTE = "/api/proxy/v1/jushuitan/orders/search"
PRODUCT_ROUTE = "/api/proxy/v1/jushuitan/items/query"
SUPPLIER_ROUTE = "/api/proxy/v1/jushuitan/suppliers/query"

SCHEMA_SQL = [
    f"""
    CREATE TABLE IF NOT EXISTS `{PURCHASE_ORDER_TABLE}` (
        po_id VARCHAR(64) NOT NULL PRIMARY KEY,
        po_date DATETIME NULL,
        so_id VARCHAR(128) NOT NULL DEFAULT '',
        status VARCHAR(64) NOT NULL DEFAULT '',
        supplier_id VARCHAR(128) NOT NULL DEFAULT '',
        seller VARCHAR(255) NOT NULL DEFAULT '',
        purchaser_name VARCHAR(128) NOT NULL DEFAULT '',
        send_address VARCHAR(512) NOT NULL DEFAULT '',
        payment_method VARCHAR(128) NOT NULL DEFAULT '',
        wms_co_name VARCHAR(255) NOT NULL DEFAULT '',
        confirm_date DATETIME NULL,
        finish_time DATETIME NULL,
        remark TEXT NULL,
        modified DATETIME NULL,
        source_payload JSON NULL,
        api_synced_at DATETIME NOT NULL,
        KEY idx_purchase_po_date (po_date),
        KEY idx_purchase_modified (modified),
        KEY idx_purchase_purchaser (purchaser_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{PURCHASE_ITEM_TABLE}` (
        source_key CHAR(64) NOT NULL PRIMARY KEY,
        po_id VARCHAR(64) NOT NULL,
        poi_id VARCHAR(64) NOT NULL DEFAULT '',
        sku_id VARCHAR(128) NOT NULL DEFAULT '',
        i_id VARCHAR(128) NOT NULL DEFAULT '',
        name VARCHAR(255) NOT NULL DEFAULT '',
        properties_value VARCHAR(255) NOT NULL DEFAULT '',
        qty DECIMAL(18, 4) NOT NULL DEFAULT 0,
        price DECIMAL(18, 4) NOT NULL DEFAULT 0,
        amount DECIMAL(18, 4) NOT NULL DEFAULT 0,
        delivery_date DATETIME NULL,
        plan_arrive_qty DECIMAL(18, 4) NOT NULL DEFAULT 0,
        in_qty DECIMAL(18, 4) NOT NULL DEFAULT 0,
        spu VARCHAR(255) NOT NULL DEFAULT '',
        season VARCHAR(128) NOT NULL DEFAULT '',
        category VARCHAR(128) NOT NULL DEFAULT '',
        channel VARCHAR(128) NOT NULL DEFAULT '',
        brand VARCHAR(128) NOT NULL DEFAULT '',
        supplier_id VARCHAR(128) NOT NULL DEFAULT '',
        image_url VARCHAR(2048) NOT NULL DEFAULT '',
        remark TEXT NULL,
        source_payload JSON NULL,
        api_synced_at DATETIME NOT NULL,
        KEY idx_purchase_item_po (po_id),
        KEY idx_purchase_item_sku (sku_id),
        KEY idx_purchase_item_delivery (delivery_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{ORDER_TABLE}` (
        o_id VARCHAR(64) NOT NULL PRIMARY KEY,
        so_id VARCHAR(128) NOT NULL DEFAULT '',
        outer_so_id VARCHAR(128) NOT NULL DEFAULT '',
        order_date DATETIME NULL,
        pay_date DATETIME NULL,
        status VARCHAR(64) NOT NULL DEFAULT '',
        shop_name VARCHAR(255) NOT NULL DEFAULT '',
        buyer_name VARCHAR(255) NOT NULL DEFAULT '',
        modified DATETIME NULL,
        source_payload JSON NULL,
        api_synced_at DATETIME NOT NULL,
        KEY idx_order_date (order_date),
        KEY idx_order_modified (modified),
        KEY idx_order_so_id (so_id),
        KEY idx_order_shop (shop_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{ORDER_ITEM_TABLE}` (
        source_key CHAR(64) NOT NULL PRIMARY KEY,
        o_id VARCHAR(64) NOT NULL,
        sku_id VARCHAR(128) NOT NULL DEFAULT '',
        i_id VARCHAR(128) NOT NULL DEFAULT '',
        name VARCHAR(255) NOT NULL DEFAULT '',
        properties_value VARCHAR(255) NOT NULL DEFAULT '',
        qty DECIMAL(18, 4) NOT NULL DEFAULT 0,
        image_url VARCHAR(2048) NOT NULL DEFAULT '',
        source_payload JSON NULL,
        api_synced_at DATETIME NOT NULL,
        KEY idx_order_item_order (o_id),
        KEY idx_order_item_sku (sku_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{PRODUCT_TABLE}` (
        sku_id VARCHAR(128) NOT NULL PRIMARY KEY,
        i_id VARCHAR(128) NOT NULL DEFAULT '',
        name VARCHAR(255) NOT NULL DEFAULT '',
        short_name VARCHAR(255) NOT NULL DEFAULT '',
        properties_value VARCHAR(255) NOT NULL DEFAULT '',
        category_id VARCHAR(128) NOT NULL DEFAULT '',
        category VARCHAR(128) NOT NULL DEFAULT '',
        brand VARCHAR(128) NOT NULL DEFAULT '',
        unit VARCHAR(32) NOT NULL DEFAULT '',
        supplier_id VARCHAR(128) NOT NULL DEFAULT '',
        supplier_name VARCHAR(255) NOT NULL DEFAULT '',
        supplier_sku_id VARCHAR(128) NOT NULL DEFAULT '',
        supplier_i_id VARCHAR(128) NOT NULL DEFAULT '',
        sale_price DECIMAL(18, 4) NOT NULL DEFAULT 0,
        cost_price DECIMAL(18, 4) NOT NULL DEFAULT 0,
        market_price DECIMAL(18, 4) NOT NULL DEFAULT 0,
        weight DECIMAL(18, 4) NOT NULL DEFAULT 0,
        enabled TINYINT(1) NOT NULL DEFAULT 0,
        stock_disabled TINYINT(1) NOT NULL DEFAULT 0,
        sku_type VARCHAR(64) NOT NULL DEFAULT '',
        item_type VARCHAR(64) NOT NULL DEFAULT '',
        labels VARCHAR(512) NOT NULL DEFAULT '',
        color VARCHAR(128) NOT NULL DEFAULT '',
        image_url VARCHAR(2048) NOT NULL DEFAULT '',
        thumbnail_url VARCHAR(2048) NOT NULL DEFAULT '',
        created DATETIME NULL,
        modified DATETIME NULL,
        source_payload JSON NULL,
        api_synced_at DATETIME NOT NULL,
        KEY idx_product_style (i_id),
        KEY idx_product_modified (modified),
        KEY idx_product_supplier (supplier_id),
        KEY idx_product_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{SUPPLIER_TABLE}` (
        supplier_id VARCHAR(128) NOT NULL PRIMARY KEY,
        name VARCHAR(255) NOT NULL DEFAULT '',
        supplier_code VARCHAR(128) NOT NULL DEFAULT '',
        enabled TINYINT(1) NOT NULL DEFAULT 0,
        supplier_group VARCHAR(128) NOT NULL DEFAULT '',
        contacts VARCHAR(255) NOT NULL DEFAULT '',
        mobile VARCHAR(128) NOT NULL DEFAULT '',
        phone VARCHAR(128) NOT NULL DEFAULT '',
        address VARCHAR(1000) NOT NULL DEFAULT '',
        deposit_bank VARCHAR(255) NOT NULL DEFAULT '',
        bank_account_name VARCHAR(255) NOT NULL DEFAULT '',
        bank_account_number VARCHAR(255) NOT NULL DEFAULT '',
        tax_rate DECIMAL(18, 4) NOT NULL DEFAULT 0,
        payment_method VARCHAR(128) NOT NULL DEFAULT '',
        accounting_period_days INT NOT NULL DEFAULT 0,
        business_registration_num VARCHAR(255) NOT NULL DEFAULT '',
        taxpayer_identification_num VARCHAR(255) NOT NULL DEFAULT '',
        unified_social_credit_code VARCHAR(255) NOT NULL DEFAULT '',
        establish_date DATETIME NULL,
        registered_capital VARCHAR(255) NOT NULL DEFAULT '',
        business_scope TEXT NULL,
        remark TEXT NULL,
        modified DATETIME NULL,
        source_payload JSON NULL,
        api_synced_at DATETIME NOT NULL,
        KEY idx_supplier_modified (modified),
        KEY idx_supplier_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{SYNC_STATE_TABLE}` (
        source_name VARCHAR(64) NOT NULL PRIMARY KEY,
        status VARCHAR(32) NOT NULL DEFAULT 'never',
        watermark_modified DATETIME NULL,
        last_started_at DATETIME NULL,
        last_success_at DATETIME NULL,
        last_request_id VARCHAR(128) NOT NULL DEFAULT '',
        rows_synced BIGINT NOT NULL DEFAULT 0,
        error_message VARCHAR(1000) NOT NULL DEFAULT '',
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


class MirrorError(RuntimeError):
    """不包含凭据的稳定同步错误。"""


class ProxyAPIError(MirrorError):
    def __init__(self, message: str, *, status: int | None = None, request_id: str = ""):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def _first(record: dict, *names: str, default: Any = "") -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return default


def _number(value: Any) -> str:
    try:
        return str(Decimal(str(value or "0")))
    except (InvalidOperation, TypeError, ValueError):
        return "0"


def _datetime(value: Any) -> str | None:
    value = _text(value)
    if not value:
        return None
    value = value.replace("T", " ").replace("Z", "")
    return value[:19] if len(value) >= 10 else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _list(value: Any) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
    if isinstance(value, dict):
        for key in ("datas", "data", "items", "orders"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _source_key(*parts: Any) -> str:
    value = "\x1f".join(_text(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _image_url(record: dict) -> str:
    """优先使用大图；API 不返回图片字段时稳定返回空字符串。"""
    value = _first(
        record, "pic300", "pic_300", "pic_big", "image_url", "pic_url",
        "img_url", "image", "pic160", "pic100", "pic", default="",
    )
    if isinstance(value, dict):
        value = _first(value, "url", "src", "image_url", default="")
    value = _text(value)
    if not value.startswith(("http://", "https://")):
        return ""
    host = urllib.parse.urlparse(value).hostname or ""
    if _blocked_literal_host(host):
        return ""
    return value[:2048]


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in network for network in _PRIVATE_IMAGE_NETWORKS)


def _blocked_literal_host(host: str) -> bool:
    hostname = str(host or "").strip().strip("[]").rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return _ip_is_private(ipaddress.ip_address(hostname))
    except ValueError:
        return False


def blocked_image_url(url: str, *, resolve: bool = False) -> bool:
    """内网 / localhost 图片地址不可下载。resolve=True 时再解析 DNS，挡住解析到内网的域名。"""
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        return True
    host = parsed.hostname or ""
    if _blocked_literal_host(host):
        return True
    if not resolve:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        addr = info[4][0] if info[4] else ""
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_private(ip):
            return True
    return False


def _items_field(record: dict) -> tuple[list[dict], bool]:
    for key in ("items", "item_list", "order_items", "purchase_items", "products", "skus"):
        if key in record:
            value = record.get(key)
            parsed = _list(value)
            if parsed or isinstance(value, (list, dict)):
                return parsed, True
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(decoded, (list, dict)):
                    return _list(decoded), True
    return [], False


def normalize_purchase(record: dict, synced_at: str) -> tuple[tuple, list[tuple], bool]:
    po_id = _text(_first(record, "po_id", "purchase_order_no", "purchase_id"))
    if not po_id:
        raise MirrorError("采购接口返回记录缺少 po_id")
    order = (
        po_id, _datetime(_first(record, "po_date", "purchase_date", "created")),
        _text(_first(record, "so_id", "outer_po_id", "external_order_no")),
        _text(_first(record, "status", "status_v")),
        _text(_first(record, "supplier_id", "seller_id")),
        _text(_first(record, "seller", "supplier_name", "seller_name")),
        _text(_first(record, "purchaser_name", "buyer_name", "purchaser")),
        _text(_first(record, "send_address", "address", "receive_address")),
        _text(_first(record, "payment_method", "term")),
        _text(_first(record, "wms_co_name", "warehouse_name")),
        _datetime(_first(record, "confirm_date", "audit_date")),
        _datetime(_first(record, "finish_time", "finished")),
        _text(_first(record, "remark", "memo")),
        _datetime(_first(record, "modified", "modified_at", "updated_at")),
        _json(record), synced_at,
    )
    raw_items, has_items = _items_field(record)
    items = []
    for index, item in enumerate(raw_items):
        poi_id = _text(_first(item, "poi_id", "item_id", "line_no", default=index + 1))
        sku_id = _text(_first(item, "sku_id", "sku_code"))
        style = _text(_first(item, "i_id", "style_code", "item_id"))
        name = _text(_first(item, "name", "product_name", "item_name"))
        qty = _number(_first(item, "qty", "orderQty", "quantity"))
        price = _number(_first(item, "price", "sale_price", "unit_price"))
        raw_amount = _first(item, "amount", "sale_amount", default=None)
        amount = _number(raw_amount) if raw_amount not in (None, "") else str(Decimal(qty) * Decimal(price))
        items.append((
            _source_key(po_id, poi_id, sku_id, index), po_id, poi_id, sku_id, style,
            name,
            _text(_first(item, "properties_value", "field_3", "properties", "specification")),
            qty, price, amount,
            _datetime(_first(item, "delivery_date", "min_plan_arrive_date", "plan_arrive_date")),
            _number(_first(item, "plan_arrive_qty", "expect_arrive_qty")),
            _number(_first(item, "in_qty", "inQty", "ioQty", "item_in_qty")),
            _text(_first(item, "sku_other_1", "item_sku_other_1", "spu", default="")) or name or sku_id,
            _text(_first(item, "sku_other_2", "item_sku_other_2", "season")),
            _text(_first(item, "sku_other_3", "item_sku_other_3", "category")) or "未分类",
            _text(_first(item, "sku_other_10", "item_sku_other_10", "channel")),
            _text(_first(item, "brand", "item_brand")),
            _text(_first(item, "supplier_id", "item_supplier_id")),
            _image_url(item), _text(_first(item, "remark", "memo")), _json(item), synced_at,
        ))
    return order, items, has_items


def normalize_order(record: dict, synced_at: str) -> tuple[tuple, list[tuple], bool]:
    o_id = _text(_first(record, "o_id", "order_id"))
    if not o_id:
        raise MirrorError("订单接口返回记录缺少 o_id")
    order = (
        o_id, _text(_first(record, "so_id", "platform_order_no")),
        _text(_first(record, "outer_so_id", "outer_order_no")),
        _datetime(_first(record, "order_date", "created")),
        _datetime(_first(record, "pay_date", "paid_at")),
        _text(_first(record, "status", "status_v")),
        _text(_first(record, "shop_name", "shop")),
        _text(_first(record, "buyer_name", "shop_buyer_id", "buyer_id", "buyer")),
        _datetime(_first(record, "modified", "modified_at", "updated_at")),
        _json(record), synced_at,
    )
    raw_items, has_items = _items_field(record)
    items = []
    seen: dict[tuple[str, str], int] = {}
    for item in raw_items:
        sku_id = _text(_first(item, "sku_id", "sku_code"))
        item_id = _text(_first(item, "oi_id", "item_id", "id"))
        identity = (item_id, sku_id)
        seen[identity] = seen.get(identity, 0) + 1
        items.append((
            _source_key(o_id, item_id, sku_id, seen[identity]), o_id, sku_id,
            _text(_first(item, "i_id", "style_code")),
            _text(_first(item, "name", "product_name", "item_name")),
            _text(_first(item, "properties_value", "properties", "specification")),
            _number(_first(item, "qty", "quantity", "order_qty")),
            _image_url(item), _json(item), synced_at,
        ))
    return order, items, has_items


def normalize_product(record: dict, synced_at: str) -> tuple:
    """把 sku.query 的常用字段列式保存，其余完整响应保留在 source_payload。"""
    sku_id = _text(_first(record, "sku_id", "sku_code"))
    if not sku_id:
        raise MirrorError("商品接口返回记录缺少 sku_id")
    return (
        sku_id,
        _text(_first(record, "i_id", "item_id", "style_code")),
        _text(_first(record, "name", "item_name", "product_name")),
        _text(_first(record, "short_name")),
        _text(_first(record, "properties_value", "properties", "specification")),
        _text(_first(record, "c_id", "category_id")),
        _text(_first(record, "category", "category_name")),
        _text(_first(record, "brand", "brand_name")),
        _text(_first(record, "unit")),
        _text(_first(record, "supplier_id")),
        _text(_first(record, "supplier_name")),
        _text(_first(record, "supplier_sku_id")),
        _text(_first(record, "supplier_i_id")),
        _number(_first(record, "sale_price")),
        _number(_first(record, "cost_price")),
        _number(_first(record, "market_price")),
        _number(_first(record, "weight")),
        1 if _first(record, "enabled", default=False) in (True, 1, "1", "true", "True") else 0,
        1 if _first(record, "stock_disabled", default=False) in (True, 1, "1", "true", "True") else 0,
        _text(_first(record, "sku_type")),
        _text(_first(record, "item_type")),
        _text(_first(record, "labels")),
        _text(_first(record, "color")),
        _image_url(record),
        _text(_first(record, "pic", "pic100", "pic160"))[:2048],
        _datetime(_first(record, "created", "created_at")),
        _datetime(_first(record, "modified", "modified_at", "updated_at")),
        _json(record), synced_at,
    )


def normalize_supplier(record: dict, synced_at: str) -> tuple:
    """供应商财税、联系信息用于合同，完整原始字段同时保留用于后续规则。"""
    supplier_id = _text(_first(record, "supplier_id", "seller_id", "id"))
    if not supplier_id:
        raise MirrorError("供应商接口返回记录缺少 supplier_id")
    return (
        supplier_id,
        _text(_first(record, "name", "supplier_name", "seller")),
        _text(_first(record, "supplier_code", "code")),
        1 if _first(record, "enabled", default=False) in (True, 1, "1", "true", "True") else 0,
        _text(_first(record, "group", "supplier_group")),
        _text(_first(record, "contacts", "contact")),
        _text(_first(record, "mobile")),
        _text(_first(record, "phone")),
        _text(_first(record, "address")),
        _text(_first(record, "depositbank", "deposit_bank")),
        _text(_first(record, "bankacount", "bank_account_name")),
        _text(_first(record, "acountnumber", "bank_account_number")),
        _number(_first(record, "tax_rate")),
        _text(_first(record, "payment_method")),
        int(Decimal(_number(_first(record, "accounting_period_days")))),
        _text(_first(record, "business_registration_num")),
        _text(_first(record, "taxpayer_identification_num")),
        _text(_first(record, "unified_social_credit_code")),
        _datetime(_first(record, "establish_date")),
        _text(_first(record, "registered_capital")),
        _text(_first(record, "business_scope")),
        _text(_first(record, "remark", "memo")),
        _datetime(_first(record, "modified", "modified_at", "updated_at")),
        _json(record), synced_at,
    )


def _walk_dicts(value: Any, max_depth: int = 5) -> Iterable[dict]:
    queue = [(value, 0)]
    while queue:
        current, depth = queue.pop(0)
        if not isinstance(current, dict):
            continue
        yield current
        if depth < max_depth:
            queue.extend((child, depth + 1) for child in current.values() if isinstance(child, dict))


def _request_id(value: Any) -> str:
    for record in _walk_dicts(value, 2):
        found = _first(record, "request_id", "requestId", "trace_id", "traceId")
        if found:
            return _text(found)[:128]
    return ""


def _api_error(value: Any) -> str:
    """只检查响应包裹层，避免把业务记录里的 code/status 当成错误。"""
    records = list(_walk_dicts(value, 2))
    for record in records:
        error = record.get("error")
        if error:
            if isinstance(error, dict):
                return _text(_first(error, "message", "msg", "code", default=error))
            return _text(error)
        success = record.get("success")
        if success is False:
            return _text(_first(record, "message", "msg", default="API 返回 success=false"))
        if "code" in record:
            code = _text(record.get("code")).lower()
            if code not in ("", "0", "200", "ok", "success"):
                return _text(_first(record, "message", "msg", default=f"API 错误码 {code}"))
    return ""


def extract_page(value: Any, page_index: int, page_size: int) -> tuple[list[dict], bool, str]:
    """兼容代理包裹层及聚水潭常见 datas/data/orders 返回结构。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MirrorError("API 返回的不是 JSON 对象") from exc
    error = _api_error(value)
    request_id = _request_id(value)
    if error:
        raise ProxyAPIError(error, request_id=request_id)

    records: list[dict] | None = None
    container: dict = value if isinstance(value, dict) else {}
    if isinstance(value, list):
        records = _list(value)
    else:
        for record in _walk_dicts(value):
            for key in ("datas", "orders", "purchase_orders", "order_list", "data_list", "list"):
                if isinstance(record.get(key), list):
                    records = _list(record[key])
                    container = record
                    break
            if records is not None:
                break
        if records is None:
            for record in _walk_dicts(value):
                if isinstance(record.get("data"), list):
                    records = _list(record["data"])
                    container = record
                    break
        if records is None:
            # 聚水潭采购查询在空结果时返回 datas=null，并用 data_count=0 表示成功空页。
            for record in _walk_dicts(value):
                count = _first(record, "data_count", "dataCount", "DataCount", "total", default=None)
                if count not in (None, ""):
                    try:
                        if int(count) == 0:
                            records = []
                            container = record
                            break
                    except (TypeError, ValueError):
                        pass
    if records is None:
        raise MirrorError("API 响应中未找到 datas/orders 数据列表")

    def integer(*names: str) -> int | None:
        for record in (container, *list(_walk_dicts(value, 3))):
            raw = _first(record, *names, default=None)
            if raw not in (None, ""):
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    pass
        return None

    response_page = integer("page_index", "pageIndex", "PageIndex") or page_index
    page_count = integer("page_count", "pageCount", "PageCount")
    data_count = integer("data_count", "dataCount", "DataCount", "total")
    has_next = None
    for record in (container, *list(_walk_dicts(value, 3))):
        for key in ("has_next", "hasNext"):
            if key in record:
                has_next = bool(record[key])
                break
    if has_next is not None:
        more = has_next
    elif page_count is not None:
        more = response_page < page_count
    elif data_count is not None and data_count >= 0:
        more = response_page * page_size < data_count
    else:
        more = len(records) >= page_size
    return records, more, request_id


class SupplyProxyClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, *, timeout: int = 45):
        self.base_url = str(base_url or "").rstrip("/")
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.timeout = max(5, int(timeout))
        if not self.base_url or not self.client_id or not self.client_secret:
            raise MirrorError("实时同步缺少 SUPPLY_API_BASE / CLIENT_ID / CLIENT_SECRET 配置")
        self.ssl_context = SSL_CONTEXT

    def post(self, route: str, body: dict) -> dict:
        request = urllib.request.Request(
            self.base_url + route,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "X-Client-Id": self.client_id,
                "Authorization": f"Bearer {self.client_secret}",
                "Content-Type": "application/json",
                "User-Agent": "AgentDemoRealtimeMirror/1.0",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context,
            ) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = {}
            request_id = _request_id(value)
            detail = _api_error(value) or f"HTTP {exc.code}"
            if exc.code == 404 and "not authorized" in detail.lower():
                detail = "接口路由不存在或当前 Client 未授权"
            raise ProxyAPIError(detail, status=exc.code, request_id=request_id) from exc
        except urllib.error.URLError as exc:
            raise ProxyAPIError(f"供应链代理连接失败：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ProxyAPIError("供应链代理返回了无效 JSON") from exc


def ensure_schema(env_path: str) -> None:
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                for sql in SCHEMA_SQL:
                    cursor.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


PURCHASE_ORDER_COLUMNS = [
    "po_id", "po_date", "so_id", "status", "supplier_id", "seller",
    "purchaser_name", "send_address", "payment_method", "wms_co_name",
    "confirm_date", "finish_time", "remark", "modified", "source_payload", "api_synced_at",
]
PURCHASE_ITEM_COLUMNS = [
    "source_key", "po_id", "poi_id", "sku_id", "i_id", "name", "properties_value",
    "qty", "price", "amount", "delivery_date", "plan_arrive_qty", "in_qty", "spu",
    "season", "category", "channel", "brand", "supplier_id", "image_url", "remark",
    "source_payload", "api_synced_at",
]
ORDER_COLUMNS = [
    "o_id", "so_id", "outer_so_id", "order_date", "pay_date", "status", "shop_name",
    "buyer_name", "modified", "source_payload", "api_synced_at",
]
ORDER_ITEM_COLUMNS = [
    "source_key", "o_id", "sku_id", "i_id", "name", "properties_value", "qty",
    "image_url", "source_payload", "api_synced_at",
]
PRODUCT_COLUMNS = [
    "sku_id", "i_id", "name", "short_name", "properties_value", "category_id",
    "category", "brand", "unit", "supplier_id", "supplier_name", "supplier_sku_id",
    "supplier_i_id", "sale_price", "cost_price", "market_price", "weight", "enabled",
    "stock_disabled", "sku_type", "item_type", "labels", "color", "image_url",
    "thumbnail_url", "created", "modified", "source_payload", "api_synced_at",
]
SUPPLIER_COLUMNS = [
    "supplier_id", "name", "supplier_code", "enabled", "supplier_group", "contacts",
    "mobile", "phone", "address", "deposit_bank", "bank_account_name",
    "bank_account_number", "tax_rate", "payment_method", "accounting_period_days",
    "business_registration_num", "taxpayer_identification_num", "unified_social_credit_code",
    "establish_date", "registered_capital", "business_scope", "remark", "modified",
    "source_payload", "api_synced_at",
]


def _upsert_sql(table: str, columns: list[str]) -> str:
    fields = ",".join(f"`{column}`" for column in columns)
    marks = ",".join(["%s"] * len(columns))
    updates = ",".join(f"`{column}`=VALUES(`{column}`)" for column in columns[1:])
    return f"INSERT INTO `{table}` ({fields}) VALUES ({marks}) ON DUPLICATE KEY UPDATE {updates}"


def upsert_purchase_records(env_path: str, records: list[dict], synced_at: str) -> dict:
    normalized = [normalize_purchase(record, synced_at) for record in records]
    orders = [value[0] for value in normalized]
    items = [item for value in normalized for item in value[1]]
    replaced = [value[0][0] for value in normalized if value[2]]
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                if orders:
                    cursor.executemany(_upsert_sql(PURCHASE_ORDER_TABLE, PURCHASE_ORDER_COLUMNS), orders)
                if replaced:
                    marks = ",".join(["%s"] * len(replaced))
                    cursor.execute(f"DELETE FROM `{PURCHASE_ITEM_TABLE}` WHERE po_id IN ({marks})", replaced)
                if items:
                    cursor.executemany(_upsert_sql(PURCHASE_ITEM_TABLE, PURCHASE_ITEM_COLUMNS), items)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"orders": len(orders), "items": len(items), "images": sum(bool(item[19]) for item in items)}


def upsert_order_records(env_path: str, records: list[dict], synced_at: str) -> dict:
    normalized = [normalize_order(record, synced_at) for record in records]
    orders = [value[0] for value in normalized]
    items = [item for value in normalized for item in value[1]]
    replaced = [value[0][0] for value in normalized if value[2]]
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                if orders:
                    cursor.executemany(_upsert_sql(ORDER_TABLE, ORDER_COLUMNS), orders)
                if replaced:
                    marks = ",".join(["%s"] * len(replaced))
                    cursor.execute(f"DELETE FROM `{ORDER_ITEM_TABLE}` WHERE o_id IN ({marks})", replaced)
                if items:
                    cursor.executemany(_upsert_sql(ORDER_ITEM_TABLE, ORDER_ITEM_COLUMNS), items)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"orders": len(orders), "items": len(items), "images": sum(bool(item[7]) for item in items)}


def replace_order_item_sku(env_path: str, o_id: str, source_sku: str, target_sku: str) -> bool:
    """把一单镜像明细里的源 SKU 换成目标。增量同步滞后时先本地跟上。"""
    o_id = str(o_id or "").strip()
    source_sku = str(source_sku or "").strip()
    target_sku = str(target_sku or "").strip()
    if not o_id or not source_sku or not target_sku or source_sku == target_sku:
        return False
    synced_at = _format_api_time(business_now())
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT source_key, o_id, sku_id, i_id, name, properties_value, qty, "
                    f"image_url, source_payload FROM `{ORDER_ITEM_TABLE}` "
                    f"WHERE CAST(o_id AS CHAR)=%s",
                    (o_id,),
                )
                rows = list(cursor.fetchall() or [])
                sources = [row for row in rows if str(row.get("sku_id") or "") == source_sku]
                if not sources:
                    return False
                has_target = any(str(row.get("sku_id") or "") == target_sku for row in rows)
                for row in sources:
                    cursor.execute(
                        f"DELETE FROM `{ORDER_ITEM_TABLE}` WHERE source_key=%s",
                        (row["source_key"],),
                    )
                if not has_target:
                    src = sources[0]
                    cursor.execute(
                        _upsert_sql(ORDER_ITEM_TABLE, ORDER_ITEM_COLUMNS),
                        (
                            _source_key(o_id, "", target_sku, 1),
                            o_id, target_sku,
                            str(src.get("i_id") or ""),
                            str(src.get("name") or ""),
                            str(src.get("properties_value") or ""),
                            str(src.get("qty") or "1"),
                            str(src.get("image_url") or ""),
                            src.get("source_payload") or "{}",
                            synced_at,
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return True


def upsert_product_records(env_path: str, records: list[dict], synced_at: str) -> dict:
    products = [normalize_product(record, synced_at) for record in records]
    with connect(env_path) as conn:
        try:
            if products:
                with conn.cursor() as cursor:
                    cursor.executemany(_upsert_sql(PRODUCT_TABLE, PRODUCT_COLUMNS), products)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"records": len(products), "images": sum(bool(row[23]) for row in products)}


def upsert_supplier_records(env_path: str, records: list[dict], synced_at: str) -> dict:
    suppliers = [normalize_supplier(record, synced_at) for record in records]
    with connect(env_path) as conn:
        try:
            if suppliers:
                with conn.cursor() as cursor:
                    cursor.executemany(_upsert_sql(SUPPLIER_TABLE, SUPPLIER_COLUMNS), suppliers)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"records": len(suppliers)}


def sync_state(
    env_path: str, source_name: str | None = None, *, ensure: bool = True,
) -> dict | list[dict]:
    if ensure:
        ensure_schema(env_path)
    sql = f"SELECT * FROM `{SYNC_STATE_TABLE}`"
    params: list[Any] = []
    if source_name:
        sql += " WHERE source_name=%s"
        params.append(source_name)
    sql += " ORDER BY source_name"
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    return (rows[0] if rows else {}) if source_name else rows


def _state_update(env_path: str, source_name: str, **values: Any) -> None:
    allowed = {
        "status", "watermark_modified", "last_started_at", "last_success_at",
        "last_request_id", "rows_synced", "error_message",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    columns = ["source_name", *values]
    row = [source_name, *values.values()]
    update = ",".join(f"`{key}`=VALUES(`{key}`)" for key in values)
    sql = (
        f"INSERT INTO `{SYNC_STATE_TABLE}` ({','.join(f'`{key}`' for key in columns)}) "
        f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE {update}"
    )
    with connect(env_path) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, row)
        conn.commit()


def _format_api_time(value: datetime) -> str:
    if value.tzinfo:
        value = value.astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=BUSINESS_TIMEZONE) if value.tzinfo is None else value
    raw = _text(value).replace("T", " ")[:19]
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BUSINESS_TIMEZONE)
    except ValueError:
        return None


class RealtimeMirror:
    """分页同步订单和采购单；每个数据源单独维护成功水位。"""

    def __init__(
        self,
        env_path: str,
        client: SupplyProxyClient,
        *,
        page_size: int = 50,
        initial_days: int = 30,
        overlap_minutes: int = 5,
        chunk_days: int = 7,
        request_interval: float = 1.05,
        image_dir: str | Path | None = None,
    ):
        self.env_path = env_path
        self.client = client
        # orders.search 官方建议不超过 50；采购接口同用该值以保证统一限流。
        self.page_size = max(1, min(int(page_size), 50))
        self.initial_days = max(1, int(initial_days))
        self.overlap = timedelta(minutes=max(0, int(overlap_minutes)))
        self.chunk = timedelta(days=max(1, int(chunk_days)))
        self.request_interval = max(0, float(request_interval))
        self.image_dir = Path(image_dir) if image_dir else None
        self._lock = threading.Lock()
        self._image_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=5000)
        self._image_pending: set[str] = set()
        self._image_lock = threading.Lock()
        self._image_workers_started = False
        self._schema_ready = False

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        for attempt in range(3):
            try:
                ensure_schema(self.env_path)
                self._schema_ready = True
                return
            except Exception as exc:
                if attempt == 2 or not is_transient_mysql_error(exc):
                    raise
                time.sleep(0.5 * (attempt + 1))

    def sync_all(self, *, since: datetime | None = None, until: datetime | None = None) -> dict:
        if not self._lock.acquire(blocking=False):
            raise MirrorError("已有实时镜像同步任务正在运行")
        try:
            self._ensure_schema()
            result = {}
            errors = []
            for source in ("purchase", "orders", "products", "suppliers"):
                for attempt in range(2):
                    try:
                        result[source] = self.sync_source(source, since=since, until=until)
                        break
                    except Exception as exc:  # 两个数据源互不阻塞，并在最后统一报告。
                        if attempt == 0 and is_transient_mysql_error(exc):
                            time.sleep(0.5)
                            continue
                        result[source] = {"ok": False, "error": str(exc)}
                        errors.append(f"{source}: {exc}")
                        break
            if errors:
                raise MirrorError("；".join(errors))
            return result
        finally:
            self._lock.release()

    def sync_source(
        self, source: str, *, since: datetime | None = None, until: datetime | None = None,
    ) -> dict:
        if source not in ("purchase", "orders", "products", "suppliers"):
            raise ValueError("source 只能是 purchase、orders、products 或 suppliers")
        self._ensure_schema()
        now = business_now()
        end = until or now
        state = sync_state(self.env_path, source, ensure=False)
        watermark = _parse_time(state.get("watermark_modified")) if state else None
        begin = since or ((watermark - self.overlap) if watermark else (end - timedelta(days=self.initial_days)))
        if begin >= end:
            begin = end - max(self.overlap, timedelta(minutes=1))
        started = _format_api_time(now)
        _state_update(
            self.env_path, source, status="syncing", last_started_at=started, error_message="",
        )
        totals = {"records": 0, "orders": 0, "items": 0, "images": 0, "pages": 0, "requestId": ""}
        try:
            # 商品接口对时间窗口限制为 7 天。首次接入先按业务镜像里已经出现过的 SKU
            # 批量补齐，避免扫描多年空窗口；随后再跑最近修改时间增量，持续收新商品。
            if source == "products" and not state.get("last_success_at") and since is None:
                bootstrap = self._sync_referenced_products()
                for key in ("records", "images", "pages"):
                    totals[key] += bootstrap.get(key, 0)
                totals["requestId"] = bootstrap.get("requestId") or totals["requestId"]

            # suppliers.query 支持不带时间条件的全量分页。第一次直接全量抓取，之后才按
            # modified 水位增量，既完整又不会把多年历史切成数百个空窗口。
            if source == "suppliers" and not state.get("last_success_at") and since is None:
                page = 1
                while True:
                    value = self.client.post(SUPPLIER_ROUTE, {
                        "page_index": page, "page_size": self.page_size, "supplier_id": "",
                    })
                    records, more, request_id = extract_page(value, page, self.page_size)
                    counts = upsert_supplier_records(
                        self.env_path, records, _format_api_time(business_now()),
                    )
                    totals["records"] += counts["records"]
                    totals["pages"] += 1
                    totals["requestId"] = request_id or totals["requestId"]
                    if not more:
                        break
                    page += 1
                    if page > 10000:
                        raise MirrorError("供应商 API 分页超过 10000 页，已停止")
                    if self.request_interval:
                        time.sleep(self.request_interval)
                finished = _format_api_time(business_now())
                _state_update(
                    self.env_path, source, status="success",
                    watermark_modified=_format_api_time(end), last_success_at=finished,
                    last_request_id=totals["requestId"], rows_synced=totals["records"],
                    error_message="",
                )
                return {"ok": True, "mode": "full", "end": _format_api_time(end), **totals}

            window_start = begin
            while window_start < end:
                window_end = min(window_start + self.chunk, end)
                page = 1
                while True:
                    body = {
                        "page_index": page,
                        "page_size": self.page_size,
                        "modified_begin": _format_api_time(window_start),
                        "modified_end": _format_api_time(window_end),
                    }
                    route = {
                        "purchase": PURCHASE_ROUTE,
                        "orders": ORDER_ROUTE,
                        "products": PRODUCT_ROUTE,
                        "suppliers": SUPPLIER_ROUTE,
                    }[source]
                    value = self.client.post(route, body)
                    records, more, request_id = extract_page(value, page, self.page_size)
                    synced_at = _format_api_time(business_now())
                    if source == "purchase":
                        counts = upsert_purchase_records(self.env_path, records, synced_at)
                        counts["records"] = counts["orders"]
                    elif source == "orders":
                        counts = upsert_order_records(self.env_path, records, synced_at)
                        counts["records"] = counts["orders"]
                    elif source == "products":
                        counts = upsert_product_records(self.env_path, records, synced_at)
                    else:
                        counts = upsert_supplier_records(self.env_path, records, synced_at)
                    for key in ("records", "orders", "items", "images"):
                        totals[key] += counts.get(key, 0)
                    totals["pages"] += 1
                    totals["requestId"] = request_id or totals["requestId"]
                    if self.image_dir and records and source in ("orders", "products"):
                        self._queue_images(records)
                    if not more:
                        break
                    page += 1
                    if page > 10000:
                        raise MirrorError("API 分页超过 10000 页，已停止以避免无限循环")
                    if self.request_interval:
                        time.sleep(self.request_interval)
                window_start = window_end
                if window_start < end and self.request_interval:
                    time.sleep(self.request_interval)
            finished = _format_api_time(business_now())
            _state_update(
                self.env_path, source, status="success", watermark_modified=_format_api_time(end),
                last_success_at=finished, last_request_id=totals["requestId"],
                rows_synced=totals["records"], error_message="",
            )
            return {"ok": True, "begin": _format_api_time(begin), "end": _format_api_time(end), **totals}
        except Exception as exc:
            request_id = exc.request_id if isinstance(exc, ProxyAPIError) else ""
            try:
                _state_update(
                    self.env_path, source, status="failed", last_request_id=request_id,
                    error_message=str(exc)[:1000],
                )
            except Exception:
                # 原始断连错误用于上层判断是否安全重试，状态记录失败不能覆盖它。
                pass
            raise

    def refresh_orders(self, o_ids: list[str]) -> dict:
        """按内部单号拉代理并覆盖镜像，不推进增量水位。"""
        clean: list[str] = []
        for value in o_ids or []:
            oid = str(value or "").strip()
            if oid and oid not in clean:
                clean.append(oid)
        if not clean:
            return {"ok": True, "orders": 0, "items": 0, "pages": 0}
        totals = {"orders": 0, "items": 0, "pages": 0, "requestId": ""}
        synced_at = _format_api_time(business_now())
        chunk_size = min(self.page_size, 20)
        for offset in range(0, len(clean), chunk_size):
            chunk = clean[offset:offset + chunk_size]
            page = 1
            while True:
                value = self.client.post(ORDER_ROUTE, {
                    "page_index": page,
                    "page_size": self.page_size,
                    "o_ids": ",".join(chunk),
                })
                records, more, request_id = extract_page(value, page, self.page_size)
                counts = upsert_order_records(self.env_path, records, synced_at)
                totals["orders"] += counts.get("orders", 0)
                totals["items"] += counts.get("items", 0)
                totals["pages"] += 1
                totals["requestId"] = request_id or totals["requestId"]
                if not more:
                    break
                page += 1
                if page > 100:
                    raise MirrorError("按单刷新订单超过 100 页，已停止")
                if self.request_interval:
                    time.sleep(self.request_interval)
            if offset + chunk_size < len(clean) and self.request_interval:
                time.sleep(self.request_interval)
        return {"ok": True, **totals}

    def _sync_referenced_products(self) -> dict:
        """首次接入商品主数据时，优先补齐订单/采购镜像实际引用的 SKU。"""
        sql = f"""
            SELECT refs.sku_id
            FROM (
                SELECT DISTINCT sku_id FROM `{PURCHASE_ITEM_TABLE}` WHERE sku_id <> ''
                UNION
                SELECT DISTINCT sku_id FROM `{ORDER_ITEM_TABLE}` WHERE sku_id <> ''
            ) refs
            LEFT JOIN `{PRODUCT_TABLE}` products ON products.sku_id = refs.sku_id
            WHERE products.sku_id IS NULL
            ORDER BY refs.sku_id
        """
        with connect(self.env_path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                skus = [str(row.get("sku_id") or "") for row in cursor.fetchall()]
        totals = {"records": 0, "images": 0, "pages": 0, "requestId": ""}
        # sku.query 的 sku_ids 是逗号分隔字符串；接口实测单批 100 个可用。
        for offset in range(0, len(skus), 100):
            batch = skus[offset:offset + 100]
            value = self.client.post(PRODUCT_ROUTE, {
                "page_index": 1, "page_size": 100,
                "modified_begin": "", "modified_end": "",
                "sku_id": "", "sku_ids": ",".join(batch),
            })
            records, _more, request_id = extract_page(value, 1, 100)
            counts = upsert_product_records(
                self.env_path, records, _format_api_time(business_now()),
            )
            totals["records"] += counts["records"]
            totals["images"] += counts["images"]
            totals["pages"] += 1
            totals["requestId"] = request_id or totals["requestId"]
            if self.image_dir and records:
                self._queue_images(records)
            if offset + 100 < len(skus) and self.request_interval:
                time.sleep(self.request_interval)
        return totals

    def _queue_images(self, records: list[dict]) -> None:
        """图片走守护队列，不能延迟订单/采购单水位提交。"""
        if not self.image_dir:
            return
        self.image_dir.mkdir(parents=True, exist_ok=True)
        targets: dict[str, str] = {}
        for record in records:
            sku = _text(_first(record, "sku_id", "sku_code"))
            url = _image_url(record)
            if sku and url:
                targets.setdefault(sku, url)
            raw_items, _ = _items_field(record)
            for item in raw_items:
                sku = _text(_first(item, "sku_id", "sku_code"))
                url = _image_url(item)
                if sku and url:
                    targets.setdefault(sku, url)
        if not targets:
            return
        with self._image_lock:
            if not self._image_workers_started:
                for index in range(4):
                    threading.Thread(
                        target=self._image_worker,
                        name=f"realtime-image-{index + 1}",
                        daemon=True,
                    ).start()
                self._image_workers_started = True
            for sku, url in targets.items():
                if sku in self._image_pending:
                    continue
                try:
                    self._image_queue.put_nowait((sku, url))
                except queue.Full:
                    break
                self._image_pending.add(sku)

    def _image_worker(self) -> None:
        while True:
            sku, url = self._image_queue.get()
            try:
                cache_product_image(self.image_dir, sku, url)
            except (OSError, urllib.error.URLError, ValueError):
                # 图片不能阻塞订单/采购单主数据实时同步。
                pass
            finally:
                with self._image_lock:
                    self._image_pending.discard(sku)
                self._image_queue.task_done()


def cache_product_image(directory: Path, sku: str, url: str, *, timeout: int = 10) -> Path | None:
    safe = sku if Path(sku).name == sku and all(char.isalnum() or char in "_.-" for char in sku) else ""
    if not safe:
        return None
    existing = next((path for path in directory.glob(f"{safe}.*") if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")), None)
    if existing:
        return existing
    if blocked_image_url(url, resolve=True):
        raise ValueError("拒绝下载内网或本机商品图片地址")
    request = urllib.request.Request(url, headers={"User-Agent": "AgentDemoRealtimeMirror/1.0"})

    class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if blocked_image_url(newurl, resolve=True):
                raise ValueError("拒绝跟随到内网图片地址")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
        urllib.request.HTTPHandler(),
        _GuardedRedirect(),
    )
    with opener.open(request, timeout=max(1, int(timeout))) as response:
        content_type = response.headers.get_content_type().lower()
        data = response.read(10 * 1024 * 1024 + 1)
    if not data or len(data) > 10 * 1024 * 1024:
        raise ValueError("商品图片为空或超过 10MB")
    signatures = {
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(content_type)
    suffix = suffix or mimetypes.guess_extension(content_type or "")
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in signatures or not signatures[suffix]:
        raise ValueError("商品图片格式或文件签名不正确")
    destination = directory / f"{safe}{suffix}"
    temporary = directory / f".{safe}.{threading.get_ident()}.tmp"
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination


class MirrorScheduler:
    """进程内增量同步调度；失败时退避，页面服务保持可用。"""

    def __init__(
        self, mirror: RealtimeMirror | None, *, enabled: bool, interval_seconds: int = 60,
        initial_delay_seconds: int = 30,
    ):
        self.mirror = mirror
        self.enabled = bool(enabled and mirror)
        self.interval = max(30, int(interval_seconds))
        self.initial_delay = max(0, int(initial_delay_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = {
            "enabled": self.enabled, "running": False, "lastError": "",
            "initialDelaySeconds": self.initial_delay,
        }

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="realtime-mirror", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def status(self) -> dict:
        return dict(self._status)

    def _run(self) -> None:
        if self.initial_delay and self._stop.wait(self.initial_delay):
            return
        failures = 0
        while not self._stop.is_set():
            self._status.update(running=True)
            try:
                self.mirror.sync_all()
                failures = 0
                self._status.update(lastError="")
            except Exception as exc:  # 主服务日志不包含请求凭据。
                failures += 1
                self._status.update(lastError=str(exc)[:1000])
                logger.error("实时镜像同步失败：%s", exc)
            finally:
                self._status.update(running=False)
            wait_seconds = min(self.interval * (2 ** min(failures, 4)), 900) if failures else self.interval
            self._stop.wait(wait_seconds)


def build_mirror_from_settings(
    setting: Callable[[str, str], str], *, root: Path, env_path: str,
) -> tuple[RealtimeMirror | None, MirrorScheduler]:
    enabled = str(setting("REALTIME_SYNC_ENABLED", "false")).strip().lower() in ("1", "true", "yes", "on")
    client_secret = setting("SUPPLY_API_CLIENT_SECRET", "")
    secret_file = str(setting("SUPPLY_API_CLIENT_SECRET_FILE", "") or "").strip()
    if secret_file:
        secret_path = Path(secret_file)
        if not secret_path.is_absolute():
            secret_path = root / secret_path
        try:
            client_secret = secret_path.read_text(encoding="utf-8").strip() or client_secret
        except OSError:
            pass
    required = [
        setting("SUPPLY_API_BASE", "https://api.wjyfek.com"),
        setting("SUPPLY_API_CLIENT_ID", ""),
        client_secret,
    ]
    if not enabled or not all(required):
        return None, MirrorScheduler(None, enabled=False)
    client = SupplyProxyClient(
        required[0], required[1], required[2],
        timeout=int(setting("REALTIME_SYNC_TIMEOUT_SECONDS", "45") or 45),
    )
    from .paths import resolve_repo_path
    image_dir = resolve_repo_path(setting("PRODUCT_IMAGE_CACHE_DIR", "files/data/product-images"), root=root)
    mirror = RealtimeMirror(
        env_path, client,
        page_size=int(setting("REALTIME_SYNC_PAGE_SIZE", "50") or 50),
        initial_days=int(setting("REALTIME_SYNC_INITIAL_DAYS", "30") or 30),
        overlap_minutes=int(setting("REALTIME_SYNC_OVERLAP_MINUTES", "5") or 5),
        chunk_days=int(setting("REALTIME_SYNC_CHUNK_DAYS", "7") or 7),
        request_interval=float(setting("REALTIME_SYNC_REQUEST_INTERVAL", "1.05") or 1.05),
        image_dir=image_dir,
    )
    scheduler = MirrorScheduler(
        mirror, enabled=True,
        interval_seconds=int(setting("REALTIME_SYNC_INTERVAL_SECONDS", "60") or 60),
        initial_delay_seconds=int(setting("REALTIME_SYNC_INITIAL_DELAY_SECONDS", "30") or 30),
    )
    return mirror, scheduler
