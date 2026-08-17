# -*- coding: utf-8 -*-
"""换货页订单候选数据源，默认读取 API 维护的本地订单镜像。"""
from __future__ import annotations

import re
from typing import Callable

from .database import connect


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_ORDER_STATUS = "待发货"


class OrderSourceError(ValueError):
    pass


def _identifier(value: str, label: str, *, required: bool = True) -> str:
    value = str(value or "").strip()
    if not value and not required:
        return ""
    if not value or not IDENTIFIER_RE.fullmatch(value):
        raise OrderSourceError(f"订单数据源字段配置不正确：{label}")
    return value


def source_status(setting: Callable[[str, str], str]) -> dict:
    table = str(setting("EXCHANGE_ORDER_TABLE", "") or "").strip()
    if not table:
        return {
            "configured": False,
            "source": "unconfigured",
            "message": "订单数据库尚未接入；当前仅保留接口和手工测试入口。",
        }
    _identifier(table, "EXCHANGE_ORDER_TABLE")
    _identifier(setting("EXCHANGE_ORDER_ID_COLUMN", "o_id"), "EXCHANGE_ORDER_ID_COLUMN")
    return {"configured": True, "source": "database", "message": ""}


def _mirror_state(env_path: str, table: str) -> dict:
    """镜像订单未成功同步时明确降级，避免把空表误报成实时数据。"""
    if table != "realtime_orders":
        return {}
    try:
        with connect(env_path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT status, last_success_at, error_message
                       FROM realtime_sync_state WHERE source_name='orders' LIMIT 1"""
                )
                row = cursor.fetchone() or {}
    except Exception:
        return {
            "configured": False,
            "source": "mirror_unavailable",
            "message": "订单镜像同步状态不可用，请检查数据库迁移和 API 同步任务。",
        }
    # 增量同步开始时状态会暂时切到 syncing；只要历史上成功过，现有镜像仍然可读。
    # 后续某次同步失败也不应让换货页失去已经落库的订单，只会保留稍旧的数据。
    if row.get("status") != "success" and not row.get("last_success_at"):
        message = "订单镜像尚未完成首次同步"
        if row.get("status") == "failed":
            detail = str(row.get("error_message") or "")
            message = "订单镜像同步失败"
            if "未授权" in detail or "not authorized" in detail.lower():
                message += "：请为当前 Client 开通订单查询接口权限"
        return {"configured": False, "source": "mirror_pending", "message": message}
    return {}


def _looks_like_order_id(query: str) -> bool:
    return bool(ORDER_ID_RE.fullmatch(str(query or "").strip()))


def _date_arg(value: str, label: str) -> str:
    value = str(value or "").strip()[:10]
    if not value:
        return ""
    if not DATE_RE.fullmatch(value):
        raise OrderSourceError(f"{label} 必须是 YYYY-MM-DD")
    return value


def resolve_status_filter(status, *, query: str = "", source_sku: str = "",
                          shop: str = "", date_from: str = "", date_to: str = "") -> list[str]:
    """未指定状态时：候选搜索默认待发货；查明确单号则不限状态。"""
    if status is None:
        specific = _looks_like_order_id(query) and not source_sku and not shop and not date_from and not date_to
        return [] if specific else [DEFAULT_ORDER_STATUS]
    if isinstance(status, (list, tuple)):
        parts = [str(item or "").strip() for item in status]
    else:
        parts = [part.strip() for part in re.split(r"[,，;；]+", str(status or ""))]
    parts = [part for part in parts if part]
    if not parts or any(part.lower() in ("all", "*", "全部") for part in parts):
        return []
    return parts


def fetch_exchange_orders(
    setting: Callable[[str, str], str],
    default_env_path: str,
    *,
    query: str = "",
    source_sku: str = "",
    shop: str = "",
    status=None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
) -> dict:
    status_filter = status
    availability = source_status(setting)
    if not availability["configured"]:
        return {**availability, "orders": []}

    limit = max(1, min(int(limit), 200))
    table = _identifier(setting("EXCHANGE_ORDER_TABLE", ""), "EXCHANGE_ORDER_TABLE")
    env_path = str(setting("EXCHANGE_ORDER_DATABASE_ENV_FILE", "") or "").strip() or default_env_path
    mirror_status = _mirror_state(env_path, table)
    if mirror_status:
        return {**availability, **mirror_status, "orders": []}
    oid_col = _identifier(setting("EXCHANGE_ORDER_ID_COLUMN", "o_id"), "EXCHANGE_ORDER_ID_COLUMN")
    optional = {
        "platform": _identifier(setting("EXCHANGE_ORDER_PLATFORM_ID_COLUMN", "so_id"), "EXCHANGE_ORDER_PLATFORM_ID_COLUMN", required=False),
        "date": _identifier(setting("EXCHANGE_ORDER_DATE_COLUMN", "order_date"), "EXCHANGE_ORDER_DATE_COLUMN", required=False),
        "status": _identifier(setting("EXCHANGE_ORDER_STATUS_COLUMN", "status"), "EXCHANGE_ORDER_STATUS_COLUMN", required=False),
        "shop": _identifier(setting("EXCHANGE_ORDER_SHOP_COLUMN", "shop_name"), "EXCHANGE_ORDER_SHOP_COLUMN", required=False),
        "buyer": _identifier(setting("EXCHANGE_ORDER_BUYER_COLUMN", "buyer_name"), "EXCHANGE_ORDER_BUYER_COLUMN", required=False),
    }
    item_table = _identifier(setting("EXCHANGE_ORDER_ITEM_TABLE", ""), "EXCHANGE_ORDER_ITEM_TABLE", required=False)
    item_oid = _identifier(setting("EXCHANGE_ORDER_ITEM_ORDER_ID_COLUMN", "o_id"), "EXCHANGE_ORDER_ITEM_ORDER_ID_COLUMN") if item_table else ""
    item_sku = _identifier(setting("EXCHANGE_ORDER_ITEM_SKU_COLUMN", "sku_id"), "EXCHANGE_ORDER_ITEM_SKU_COLUMN") if item_table else ""

    def selected(column: str, alias: str) -> str:
        return f"COALESCE(CAST(o.`{column}` AS CHAR), '') AS `{alias}`" if column else f"'' AS `{alias}`"

    where = [f"COALESCE(CAST(o.`{oid_col}` AS CHAR), '') <> ''"]
    params: list[object] = []
    query = str(query or "").strip()
    shop = str(shop or "").strip()
    date_from = _date_arg(date_from, "date_from")
    date_to = _date_arg(date_to, "date_to")
    statuses = resolve_status_filter(
        status_filter, query=query, source_sku=source_sku, shop=shop,
        date_from=date_from, date_to=date_to,
    )
    if query:
        searchable = [oid_col] + [optional[key] for key in ("platform", "shop", "buyer") if optional[key]]
        where.append("(" + " OR ".join(f"CAST(o.`{column}` AS CHAR) LIKE %s" for column in searchable) + ")")
        params.extend([f"%{query}%"] * len(searchable))
    if shop:
        if not optional["shop"]:
            return {
                **availability, "configured": False, "source": "partial",
                "message": "订单主表未配置店铺列，无法按店铺筛选。",
                "orders": [],
            }
        where.append(f"CAST(o.`{optional['shop']}` AS CHAR) LIKE %s")
        params.append(f"%{shop}%")
    if statuses:
        if not optional["status"]:
            return {
                **availability, "configured": False, "source": "partial",
                "message": "订单主表未配置状态列，无法按状态筛选。",
                "orders": [],
            }
        where.append(
            "(" + " OR ".join(f"CAST(o.`{optional['status']}` AS CHAR) = %s" for _ in statuses) + ")"
        )
        params.extend(statuses)
    if date_from or date_to:
        if not optional["date"]:
            return {
                **availability, "configured": False, "source": "partial",
                "message": "订单主表未配置日期列，无法按日期筛选。",
                "orders": [],
            }
        date_expr = f"LEFT(CAST(o.`{optional['date']}` AS CHAR), 10)"
        if date_from:
            where.append(f"{date_expr} >= %s")
            params.append(date_from)
        if date_to:
            where.append(f"{date_expr} <= %s")
            params.append(date_to)
    source_sku = str(source_sku or "").strip()
    if source_sku:
        if not item_table:
            return {
                **availability,
                "configured": False,
                "source": "partial",
                "message": "订单主表已配置，但订单明细表尚未配置，无法按源 SKU 筛选。",
                "orders": [],
            }
        where.append(
            f"EXISTS (SELECT 1 FROM `{item_table}` i "
            f"WHERE CAST(i.`{item_oid}` AS CHAR)=CAST(o.`{oid_col}` AS CHAR) AND i.`{item_sku}`=%s)"
        )
        params.append(source_sku)
    params.append(limit)
    sql = f"""
        SELECT {selected(oid_col, 'o_id')},
               {selected(optional['platform'], 'platform_order_no')},
               {selected(optional['date'], 'order_date')},
               {selected(optional['status'], 'status')},
               {selected(optional['shop'], 'shop_name')},
               {selected(optional['buyer'], 'buyer')}
        FROM `{table}` o
        WHERE {' AND '.join(where)}
        ORDER BY o.`{oid_col}` DESC
        LIMIT %s
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    return {
        **availability,
        "filters": {
            "query": query,
            "sourceSku": source_sku,
            "shop": shop,
            "status": statuses,
            "dateFrom": date_from,
            "dateTo": date_to,
        },
        "orders": [{
            "oId": str(row.get("o_id") or ""),
            "platformOrderNo": str(row.get("platform_order_no") or ""),
            "orderDate": str(row.get("order_date") or "")[:19],
            "status": str(row.get("status") or ""),
            "shopName": str(row.get("shop_name") or ""),
            "buyer": str(row.get("buyer") or ""),
        } for row in rows],
    }


def fetch_exchange_order_items(
    setting: Callable[[str, str], str],
    default_env_path: str,
    *,
    o_ids: list[str],
) -> dict:
    """汇总所选订单内的商品，返回 SKU 覆盖订单数与总数量。"""
    status = source_status(setting)
    if not status["configured"]:
        return {**status, "selectedOrderCount": 0, "items": []}
    clean_oids = []
    for value in o_ids:
        oid = str(value or "").strip()
        if oid and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", oid) and oid not in clean_oids:
            clean_oids.append(oid)
    if not clean_oids:
        return {**status, "selectedOrderCount": 0, "items": []}
    if len(clean_oids) > 200:
        raise OrderSourceError("一次最多读取 200 张订单的商品")

    item_table = _identifier(setting("EXCHANGE_ORDER_ITEM_TABLE", ""), "EXCHANGE_ORDER_ITEM_TABLE", required=False)
    table = _identifier(setting("EXCHANGE_ORDER_TABLE", ""), "EXCHANGE_ORDER_TABLE")
    env_path = str(setting("EXCHANGE_ORDER_DATABASE_ENV_FILE", "") or "").strip() or default_env_path
    mirror_status = _mirror_state(env_path, table)
    if mirror_status:
        return {**status, **mirror_status, "selectedOrderCount": len(clean_oids), "items": []}
    if not item_table:
        return {
            **status, "configured": False, "source": "partial",
            "message": "订单主表已配置，但订单明细表尚未配置，不能从订单中选择商品。",
            "selectedOrderCount": len(clean_oids), "items": [],
        }
    item_oid = _identifier(setting("EXCHANGE_ORDER_ITEM_ORDER_ID_COLUMN", "o_id"), "EXCHANGE_ORDER_ITEM_ORDER_ID_COLUMN")
    item_sku = _identifier(setting("EXCHANGE_ORDER_ITEM_SKU_COLUMN", "sku_id"), "EXCHANGE_ORDER_ITEM_SKU_COLUMN")
    item_style = _identifier(setting("EXCHANGE_ORDER_ITEM_STYLE_COLUMN", "i_id"), "EXCHANGE_ORDER_ITEM_STYLE_COLUMN", required=False)
    item_name = _identifier(setting("EXCHANGE_ORDER_ITEM_NAME_COLUMN", "name"), "EXCHANGE_ORDER_ITEM_NAME_COLUMN", required=False)
    item_props = _identifier(setting("EXCHANGE_ORDER_ITEM_PROPERTIES_COLUMN", "properties_value"), "EXCHANGE_ORDER_ITEM_PROPERTIES_COLUMN", required=False)
    item_qty = _identifier(setting("EXCHANGE_ORDER_ITEM_QTY_COLUMN", "qty"), "EXCHANGE_ORDER_ITEM_QTY_COLUMN", required=False)

    def selected(column: str, alias: str, aggregate="MAX") -> str:
        if not column:
            return f"'' AS `{alias}`"
        return f"{aggregate}(COALESCE(CAST(i.`{column}` AS CHAR), '')) AS `{alias}`"

    marks = ",".join(["%s"] * len(clean_oids))
    qty_sql = f"SUM(COALESCE(i.`{item_qty}`, 0))" if item_qty else "0"
    sql = f"""
        SELECT CAST(i.`{item_sku}` AS CHAR) AS sku_id,
               {selected(item_style, 'i_id')},
               {selected(item_name, 'name')},
               {selected(item_props, 'properties_value')},
               COUNT(DISTINCT CAST(i.`{item_oid}` AS CHAR)) AS order_count,
               {qty_sql} AS total_qty
        FROM `{item_table}` i
        WHERE CAST(i.`{item_oid}` AS CHAR) IN ({marks})
          AND COALESCE(CAST(i.`{item_sku}` AS CHAR), '') <> ''
        GROUP BY CAST(i.`{item_sku}` AS CHAR)
        ORDER BY order_count DESC, sku_id
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, clean_oids)
            rows = cursor.fetchall()
    return {
        **status,
        "selectedOrderCount": len(clean_oids),
        "items": [{
            "sku": str(row.get("sku_id") or ""),
            "styleCode": str(row.get("i_id") or ""),
            "name": str(row.get("name") or ""),
            "properties": str(row.get("properties_value") or ""),
            "orderCount": int(row.get("order_count") or 0),
            "totalQuantity": float(row.get("total_qty") or 0),
        } for row in rows],
    }
