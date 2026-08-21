# -*- coding: utf-8 -*-
"""从镜像装商品 / 库存 / 出库，按款式汇总预警。"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

from ..business_time import business_now, business_today
from ..database import REALTIME_PRODUCT_TABLE, connect, fetch_last_suppliers
from ..forecast.dataset import load_in_transit
from .channel import is_offline_shop, shop_id_from_raw_so, so_id_from_raw_so
from .shops import ShopGroups, load_shop_groups
from .formula import (
    BAIHUO_REPLENISH_COVER_DAYS, daily_avg_baihuo, monthly_sales_baihuo,
    obsolete_label, order_qty, replenish_qty, sku_code_status, style_warning,
    style_year,
)
from .roster import (
    BAIHUO_LABEL, CATEGORY_LINES, parse_product_attrs, product_in_baihuo_scope,
    product_in_scope, skip_stockout_warning,
)

INVENTORY_TABLE = "realtime_inventory"
SALES_OUT_TABLE = "realtime_sales_outbounds"
SNAPSHOT_TABLE = "spu_style_snapshot"
BAIHUO_SNAPSHOT_TABLE = "baihuo_style_snapshot"
BOARD_APPAREL = "apparel"
BOARD_BAIHUO = "baihuo"
BOARDS = (BOARD_APPAREL, BOARD_BAIHUO)
SPU_PLAN_TABLES = (INVENTORY_TABLE, SALES_OUT_TABLE)
SNAPSHOT_COLUMNS = (
    "style_id", "category_line", "name", "production_mode", "sku_count",
    "year", "season", "category", "sale_price", "cost_price",
    "sales_1", "sales_3", "sales_7", "sales_14", "sales_15", "sales_30", "sales_45", "sales_60",
    "sales_90",
    "sales_60_online", "sales_60_offline", "sales_90_online", "sales_90_offline",
    "sales_7_online", "sales_7_offline", "sales_15_online", "sales_15_offline",
    "sales_30_online", "sales_30_offline", "sale_shops",
    "sales_prev7", "wow_ratio", "sales_daily",
    "broken_skus", "short_skus", "turnover_days", "stockout_label", "replenish_qty",
    "order_qty", "moq", "daily_avg", "on_hand", "qty", "occupy", "inbound",
    "in_qty",
    "remark", "labels", "missing_inventory", "computed_at",
)
# 已上线的结果表补列用；新建表直接走镜像 DDL
SNAPSHOT_COLUMN_DDL = (
    ("sales_prev7", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("wow_ratio", "DECIMAL(18, 4) NULL"),
    ("sales_daily", "TEXT NULL"),
    ("order_qty", "INT NULL"),
    ("moq", "INT NULL"),
    ("sales_90", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("in_qty", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_60_online", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_60_offline", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_90_online", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_90_offline", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_30_online", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_30_offline", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_14", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_7_online", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_7_offline", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_15_online", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sales_15_offline", "DECIMAL(18, 4) NOT NULL DEFAULT 0"),
    ("sale_shops", "TEXT NULL"),
)
SALES_STATUSES_EXCLUDED = ("Cancelled", "Delete", "Merged", "Cancel")
SALES_WINDOW_DAYS = (1, 3, 7, 15, 30, 45, 60)
SALES_LOOKBACK_DAYS = 60
# 看板趋势线：近 30 天逐日出库，旧→新
SPARK_DAYS = 30
# 自营百货：回看 30 天。日均用 7/15/30 折月；看板另出 14 天。
BAIHUO_WINDOW_DAYS = (1, 3, 7, 14, 15, 30)
BAIHUO_SALES_LOOKBACK_DAYS = 30
BAIHUO_SPARK_DAYS = 30
# 只出自营款（2026-08-20 拍板：一件代发/外供/线下定制不进总表）
OUTPUT_PRODUCTION_MODE = "自营"


def normalize_board(value) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("baihuo", "general", "百货", "自营百货"):
        return BOARD_BAIHUO
    return BOARD_APPAREL


def snapshot_table(board: str = BOARD_APPAREL) -> str:
    return BAIHUO_SNAPSHOT_TABLE if normalize_board(board) == BOARD_BAIHUO else SNAPSHOT_TABLE


def _sales_config(board: str) -> tuple[int, tuple[int, ...], int]:
    if normalize_board(board) == BOARD_BAIHUO:
        return BAIHUO_SALES_LOOKBACK_DAYS, BAIHUO_WINDOW_DAYS, BAIHUO_SPARK_DAYS
    return SALES_LOOKBACK_DAYS, SALES_WINDOW_DAYS, SPARK_DAYS


def _empty_windows(
    window_days: tuple[int, ...] = SALES_WINDOW_DAYS,
    spark_days: int = SPARK_DAYS,
) -> dict:
    windows: dict = {str(day): 0.0 for day in window_days}
    windows["prev7"] = 0.0  # 前 7 天（昨天往回 8–14 天），周环比用
    windows["days"] = [0.0] * spark_days
    return windows


class DataMissing(RuntimeError):
    """库存或出库表还没数据，不能用 0 假装现势。"""


def _as_date(value) -> date | None:
    raw = str(value or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _table_exists(cursor, table: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    row = cursor.fetchone() or {}
    return int(row.get("n") or 0) > 0


def scoped_style_ids(env_path: str, *, board: str = BOARD_APPAREL) -> list[str]:
    """圈定款式编码，去重且保持出现顺序。"""
    seen: list[str] = []
    found: set[str] = set()
    for item in load_products(env_path, board=board):
        style = item["styleId"]
        if style and style not in found:
            found.add(style)
            seen.append(style)
    return seen


def load_products(env_path: str, *, board: str = BOARD_APPAREL) -> list[dict]:
    board = normalize_board(board)
    in_scope = product_in_baihuo_scope if board == BOARD_BAIHUO else product_in_scope
    sql = (
        f"SELECT sku_id, i_id, name, labels, enabled, category, "
        f"sale_price, cost_price, source_payload "
        f"FROM `{REALTIME_PRODUCT_TABLE}` WHERE COALESCE(i_id, '') <> ''"
    )
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    products = []
    for row in rows:
        attrs = parse_product_attrs(row)
        if not attrs["enabled"] or not in_scope(attrs):
            continue
        products.append(attrs)
    return products


def _inventory_in_qty(row: dict) -> float:
    """进货仓库存。接口 in_qty，不进总库存，只给看板看采购是否已到仓。"""
    raw = row.get("in_qty")
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    payload = row.get("source_payload")
    if isinstance(payload, str) and payload.strip():
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if isinstance(payload, dict):
        try:
            return float(payload.get("in_qty") or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _load_inventory(env_path: str) -> dict[str, dict]:
    sql = (
        f"SELECT sku_id, qty, order_lock, purchase_qty, source_payload "
        f"FROM `{INVENTORY_TABLE}` WHERE COALESCE(sku_id, '') <> ''"
    )
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            if not _table_exists(cursor, INVENTORY_TABLE):
                raise DataMissing(f"缺少 {INVENTORY_TABLE}，请先同步库存")
            cursor.execute(f"SELECT COUNT(*) AS n FROM `{INVENTORY_TABLE}`")
            if int((cursor.fetchone() or {}).get("n") or 0) <= 0:
                raise DataMissing(f"{INVENTORY_TABLE} 是空的，请先跑库存同步")
            cursor.execute(sql)
            rows = cursor.fetchall()
    by_sku: dict[str, dict] = {}
    for row in rows:
        sku = str(row.get("sku_id") or "").strip()
        if not sku:
            continue
        current = by_sku.setdefault(
            sku, {"qty": 0.0, "occupy": 0.0, "inbound": 0.0, "inQty": 0.0},
        )
        current["qty"] += float(row.get("qty") or 0)
        current["occupy"] += float(row.get("order_lock") or 0)
        current["inbound"] += float(row.get("purchase_qty") or 0)
        current["inQty"] += _inventory_in_qty(row)
    return by_sku


def _add_sale(
    windows: dict,
    today: date,
    day: date,
    qty: float,
    *,
    lookback_days: int,
    window_days: tuple[int, ...],
    spark_days: int,
) -> None:
    delta = (today - day).days
    if delta < 1 or delta > lookback_days:
        return
    if delta == 1:
        windows["1"] += qty
    if 8 <= delta <= 14:
        windows["prev7"] += qty
    if delta <= spark_days:
        windows["days"][spark_days - delta] += qty
    for width in window_days:
        if width > 1 and delta <= width:
            windows[str(width)] += qty


def _parse_sale_shops(raw) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    return []


def _payload_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _load_shop_names(env_path: str) -> dict[str, str]:
    """订单镜像：shop_id / so_id → 店铺名，给没有店铺字段的出库明细用。"""
    from ..realtime_mirror import ORDER_TABLE

    table = ORDER_TABLE
    sql = (
        f"SELECT so_id, shop_name, source_payload FROM `{table}` "
        f"WHERE COALESCE(shop_name, '') <> ''"
    )
    lookup: dict[str, str] = {}
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            if not _table_exists(cursor, table):
                return lookup
            cursor.execute(sql)
            for row in cursor.fetchall():
                name = str(row.get("shop_name") or "").strip()
                if not name:
                    continue
                payload = _payload_dict(row.get("source_payload"))
                shop_id = str(payload.get("shop_id") or "").strip()
                if shop_id:
                    lookup[shop_id] = name
                so_id = str(row.get("so_id") or payload.get("so_id") or "").strip()
                if so_id:
                    lookup[f"so:{so_id}"] = name
    return lookup


def _sale_shop_id(payload: dict) -> str:
    shop_id = str(payload.get("shop_id") or payload.get("shopId") or "").strip()
    if shop_id and shop_id != "0":
        return shop_id
    return shop_id_from_raw_so(payload.get("raw_so_id") or "")


def _sale_shop_name(payload: dict, shops: dict[str, str]) -> str:
    name = str(payload.get("shop_name") or payload.get("shop") or "").strip()
    if name:
        return name
    shop_id = _sale_shop_id(payload)
    if shop_id and shop_id in shops:
        return shops[shop_id]
    so_id = so_id_from_raw_so(payload.get("raw_so_id") or "")
    if so_id:
        return shops.get(f"so:{so_id}", "")
    return ""


def _sale_channel(payload: dict, shops: dict[str, str], groups: ShopGroups) -> str:
    shop_id = _sale_shop_id(payload)
    shop_name = _sale_shop_name(payload, shops) or groups.shop_name(shop_id)
    group = groups.group_name(shop_id, shop_name)
    return "offline" if is_offline_shop(shop_name, group) else "online"


def _sale_shop_record(payload: dict, shops: dict[str, str], groups: ShopGroups) -> dict:
    """出库行对上的真实店铺：shop_id / 店名 / 店铺设置分组。对不上标出来，不编店。"""
    shop_id = _sale_shop_id(payload)
    shop_name = _sale_shop_name(payload, shops) or groups.shop_name(shop_id)
    group = groups.group_name(shop_id, shop_name)
    channel = "offline" if is_offline_shop(shop_name, group) else "online"
    return {
        "shopId": shop_id,
        "shopName": shop_name or ("(未对上店铺)" if not shop_id else shop_id),
        "groupName": group,
        "channel": channel,
        "qty7": 0.0,
        "qty15": 0.0,
        "qty30": 0.0,
    }


def _window_qty(windows: dict, key: str) -> float:
    try:
        return float(windows.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _top_shops(shops: dict, *, limit: int = 20) -> list[dict]:
    rows = []
    for rec in shops.values():
        rows.append({
            "shopId": rec.get("shopId") or "",
            "shopName": rec.get("shopName") or "",
            "groupName": rec.get("groupName") or "",
            "channel": rec.get("channel") or "online",
            "qty7": round(float(rec.get("qty7") or 0), 2),
            "qty15": round(float(rec.get("qty15") or 0), 2),
            "qty30": round(float(rec.get("qty30") or 0), 2),
        })
    rows.sort(key=lambda item: (-item["qty30"], -item["qty15"], -item["qty7"], item["shopName"]))
    return rows[:limit]


def _load_sales_windows(
    env_path: str,
    today: date,
    *,
    lookback_days: int = SALES_LOOKBACK_DAYS,
    window_days: tuple[int, ...] = SALES_WINDOW_DAYS,
    spark_days: int = SPARK_DAYS,
    split_channel: bool = False,
) -> dict[str, dict]:
    start = today - timedelta(days=lookback_days)
    excluded = ",".join(["%s"] * len(SALES_STATUSES_EXCLUDED))
    if split_channel:
        sql = (
            f"SELECT sku_id, LEFT(io_date, 10) AS io_day, qty, source_payload "
            f"FROM `{SALES_OUT_TABLE}` "
            f"WHERE COALESCE(sku_id, '') <> '' AND LEFT(io_date, 10) >= %s "
            f"AND COALESCE(status, '') NOT IN ({excluded})"
        )
    else:
        sql = (
            f"SELECT sku_id, LEFT(io_date, 10) AS io_day, SUM(qty) AS qty "
            f"FROM `{SALES_OUT_TABLE}` "
            f"WHERE COALESCE(sku_id, '') <> '' AND LEFT(io_date, 10) >= %s "
            f"AND COALESCE(status, '') NOT IN ({excluded}) "
            f"GROUP BY sku_id, io_day"
        )
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            if not _table_exists(cursor, SALES_OUT_TABLE):
                raise DataMissing(f"缺少 {SALES_OUT_TABLE}，请先同步销售出库")
            cursor.execute(sql, (start.isoformat(), *SALES_STATUSES_EXCLUDED))
            rows = cursor.fetchall()
    shops = _load_shop_names(env_path) if split_channel else {}
    groups = load_shop_groups() if split_channel else ShopGroups()
    by_sku: dict[str, dict] = defaultdict(
        lambda: _empty_windows(window_days, spark_days)
    )
    for row in rows:
        sku = str(row.get("sku_id") or "").strip()
        day = _as_date(row.get("io_day"))
        qty = float(row.get("qty") or 0)
        if not sku or day is None:
            continue
        bucket = by_sku[sku]
        if "online" not in bucket and split_channel:
            bucket["online"] = _empty_windows(window_days, spark_days)
            bucket["offline"] = _empty_windows(window_days, spark_days)
        _add_sale(
            bucket, today, day, qty,
            lookback_days=lookback_days, window_days=window_days, spark_days=spark_days,
        )
        if split_channel:
            payload = _payload_dict(row.get("source_payload"))
            channel = _sale_channel(payload, shops, groups)
            _add_sale(
                bucket[channel], today, day, qty,
                lookback_days=lookback_days, window_days=window_days, spark_days=spark_days,
            )
            record = _sale_shop_record(payload, shops, groups)
            key = record["shopId"] or record["shopName"]
            found = bucket.setdefault("shops", {}).setdefault(key, record)
            delta = (today - day).days
            if 1 <= delta <= 7:
                found["qty7"] += qty
            if 1 <= delta <= 15:
                found["qty15"] += qty
            if 1 <= delta <= 30:
                found["qty30"] += qty
    return by_sku


def build_style_alerts(env_path: str, *, today=None, board: str = BOARD_APPAREL) -> dict:
    """按款式汇总。库存表空则停；出库没有窗口当 0。"""
    board = normalize_board(board)
    today = today or business_today()
    if isinstance(today, str):
        today = date.fromisoformat(today[:10])
    lookback_days, window_days, spark_days = _sales_config(board)
    products = load_products(env_path, board=board)
    inventory = _load_inventory(env_path)
    sales = _load_sales_windows(
        env_path, today,
        lookback_days=lookback_days,
        window_days=window_days,
        spark_days=spark_days,
        split_channel=True,
    )
    inbound_fallback = load_in_transit(env_path, keys=[item["skuId"] for item in products])
    blank_windows = lambda: _empty_windows(window_days, spark_days)

    styles: dict[str, dict] = {}
    for item in products:
        row_key = item["skuId"] if board == BOARD_BAIHUO else item["styleId"]
        if not row_key:
            continue
        row_name = item["name"] if board == BOARD_BAIHUO else (item.get("productName") or item["name"])
        style = styles.setdefault(row_key, {
            "styleId": row_key,
            "name": row_name,
            "categoryLine": item["categoryLine"],
            "productionMode": item["productionMode"],
            "season": item.get("season") or "",
            "category": item.get("category") or "",
            "salePrice": item.get("salePrice") or 0.0,
            "costPrice": item.get("costPrice") or 0.0,
            "labels": set(),
            "qty": 0.0,
            "occupy": 0.0,
            "inbound": 0.0,
            "inQty": 0.0,
            "sales1": 0.0,
            "sales3": 0.0,
            "sales7": 0.0,
            "sales14": 0.0,
            "sales15": 0.0,
            "sales30": 0.0,
            "sales45": 0.0,
            "sales60": 0.0,
            "sales90": 0.0,
            "sales7Online": 0.0,
            "sales7Offline": 0.0,
            "sales15Online": 0.0,
            "sales15Offline": 0.0,
            "sales30Online": 0.0,
            "sales30Offline": 0.0,
            "sales60Online": 0.0,
            "sales60Offline": 0.0,
            "sales90Online": 0.0,
            "sales90Offline": 0.0,
            "saleShops": [],
            "salesPrev7": 0.0,
            "salesDaily": [0.0] * spark_days,
            "moq": 0,
            "broken": 0,
            "short": 0,
            "skuCount": 0,
            "missingInventory": 0,
            "styleCode": item["styleId"],
        })
        if row_name and not style["name"]:
            style["name"] = row_name
        if item["productionMode"] and not style["productionMode"]:
            style["productionMode"] = item["productionMode"]
        if item.get("season") and not style["season"]:
            style["season"] = item["season"]
        if item.get("category") and not style["category"]:
            style["category"] = item["category"]
        if item.get("salePrice") and not style["salePrice"]:
            style["salePrice"] = item["salePrice"]
        if item.get("costPrice") and not style["costPrice"]:
            style["costPrice"] = item["costPrice"]
        style["labels"].update(item["labels"])
        # 同款不同 SKU 写了不同起订量时取最大，宁多勿少
        style["moq"] = max(style["moq"], int(item.get("moq") or 0))
        style["skuCount"] += 1
        stock = inventory.get(item["skuId"])
        windows = {**blank_windows(), **(sales.get(item["skuId"]) or {})}
        style["sales1"] += _window_qty(windows, "1")
        style["sales3"] += _window_qty(windows, "3")
        style["sales7"] += _window_qty(windows, "7")
        style["sales14"] += _window_qty(windows, "14")
        style["sales15"] += _window_qty(windows, "15")
        style["sales30"] += _window_qty(windows, "30")
        style["sales45"] += _window_qty(windows, "45")
        style["sales60"] += _window_qty(windows, "60")
        style["sales90"] += _window_qty(windows, "90")
        online = windows.get("online") or {}
        offline = windows.get("offline") or {}
        style["sales7Online"] += _window_qty(online, "7")
        style["sales7Offline"] += _window_qty(offline, "7")
        style["sales15Online"] += _window_qty(online, "15")
        style["sales15Offline"] += _window_qty(offline, "15")
        style["sales30Online"] += _window_qty(online, "30")
        style["sales30Offline"] += _window_qty(offline, "30")
        style["sales60Online"] += _window_qty(online, "60")
        style["sales60Offline"] += _window_qty(offline, "60")
        style["sales90Online"] += _window_qty(online, "90")
        style["sales90Offline"] += _window_qty(offline, "90")
        if not style["saleShops"]:
            style["saleShops"] = _top_shops(windows.get("shops") or {})
        style["salesPrev7"] += windows["prev7"]
        for index, value in enumerate(windows.get("days") or []):
            if index < len(style["salesDaily"]):
                style["salesDaily"][index] += value
        if stock is None:
            # 接口对从未入过库的 SKU 不返回行。库存历史窗口已回填到 2018 年，
            # 员工数据源里这些 SKU 实际库存也全是 0（2026-08-20 逐个核过），
            # 所以按 0 计入断码，与示例表同口径；仍记 missingInventory 供备注。
            style["missingInventory"] += 1
            stock = {"qty": 0.0, "occupy": 0.0, "inbound": 0.0, "inQty": 0.0}
        inbound = stock["inbound"]
        if inbound <= 0:
            inbound = float(inbound_fallback.get(item["skuId"]) or 0)
        style["qty"] += stock["qty"]
        style["occupy"] += stock["occupy"]
        style["inbound"] += inbound
        style["inQty"] += float(stock.get("inQty") or 0)
        if board == BOARD_BAIHUO:
            expected_7 = daily_avg_baihuo(
                windows.get("7") or 0, windows.get("15") or 0, windows.get("30") or 0,
            ) * 7.0
            status = sku_code_status(stock["qty"], expected_7)
        else:
            status = sku_code_status(stock["qty"], windows["7"])
        if status == "断码":
            style["broken"] += 1
        elif status == "缺码":
            style["short"] += 1

    rows = []
    for style in styles.values():
        # 鞋服：只出自营。百货：必须带「自营百货」标签，不要求 other_10。
        if board == BOARD_APPAREL and style["productionMode"] != OUTPUT_PRODUCTION_MODE:
            continue
        if board == BOARD_BAIHUO and BAIHUO_LABEL not in style["labels"]:
            continue
        skip = skip_stockout_warning(style["labels"])
        if skip:
            continue
        avg_override = (
            daily_avg_baihuo(style["sales7"], style["sales15"], style["sales30"])
            if board == BOARD_BAIHUO else None
        )
        warning = style_warning(
            qty=style["qty"], occupy=style["occupy"], inbound=style["inbound"],
            sales_1=style["sales1"], sales_3=style["sales3"],
            sales_7=style["sales7"], sales_15=style["sales15"],
            skip_warning=skip,
            daily_avg_value=avg_override,
        )
        suggest = replenish_qty(
            warning["dailyAvg"], warning["onHand"],
            cover_days=BAIHUO_REPLENISH_COVER_DAYS if board == BOARD_BAIHUO else None,
        )
        if style["missingInventory"]:
            remark = (
                f"{style['missingInventory']}条无库存记录（按0件计）"
                if board == BOARD_BAIHUO
                else f"{style['missingInventory']}个SKU无库存记录（按0计）"
            )
        else:
            remark = ""
        rows.append({
            "styleId": style["styleId"],
            "name": style["name"],
            "categoryLine": style["categoryLine"],
            "productionMode": style["productionMode"],
            "season": style["season"],
            "category": style["category"],
            "salePrice": style["salePrice"],
            "costPrice": style["costPrice"],
            "year": style_year(style.get("styleCode") or style["styleId"]),
            "obsoleteLabel": obsolete_label(style["labels"]),
            "labels": sorted(style["labels"]),
            "skuCount": style["skuCount"],
            "missingInventory": style["missingInventory"],
            "qty": style["qty"],
            "occupy": style["occupy"],
            "inbound": style["inbound"],
            "inQty": style["inQty"],
            "sales1": style["sales1"],
            "sales3": style["sales3"],
            "sales7": style["sales7"],
            "sales14": style["sales14"],
            "sales15": style["sales15"],
            "sales30": style["sales30"],
            "sales45": style["sales45"],
            "sales60": style["sales60"],
            "sales90": style["sales90"],
            "sales7Online": style["sales7Online"],
            "sales7Offline": style["sales7Offline"],
            "sales15Online": style["sales15Online"],
            "sales15Offline": style["sales15Offline"],
            "sales30Online": style["sales30Online"],
            "sales30Offline": style["sales30Offline"],
            "sales60Online": style["sales60Online"],
            "sales60Offline": style["sales60Offline"],
            "sales90Online": style["sales90Online"],
            "sales90Offline": style["sales90Offline"],
            "monthlySales": (
                monthly_sales_baihuo(style["sales7"], style["sales15"], style["sales30"])
                if board == BOARD_BAIHUO else None
            ),
            "saleShops": style["saleShops"],
            "salesPrev7": style["salesPrev7"],
            "wowRatio": (
                round((style["sales7"] - style["salesPrev7"]) / style["salesPrev7"], 4)
                if style["salesPrev7"] > 0 else None
            ),
            "salesDaily": [round(value, 2) for value in style["salesDaily"]],
            "moq": style["moq"] or None,
            "orderQty": order_qty(suggest, moq=style["moq"] or None),
            "dailyAvg": warning["dailyAvg"],
            "onHand": warning["onHand"],
            "turnoverDays": warning["turnoverDays"],
            "turnoverDisplay": "-" if warning["turnoverDays"] is None else warning["turnoverDays"],
            "stockout": warning["stockout"],
            "stockoutLabel": "缺货" if warning["stockout"] else "",
            "replenishQty": suggest,
            "skipWarning": warning["skipWarning"],
            "brokenSkus": style["broken"],
            "shortSkus": style["short"],
            "remark": remark,
        })
    if board == BOARD_APPAREL:
        line_order = {name: index for index, name in enumerate(CATEGORY_LINES)}
        rows.sort(key=lambda item: (line_order.get(item["categoryLine"], 99), item["styleId"]))
    else:
        rows.sort(key=lambda item: (
            0 if item["categoryLine"] == "文创百货" else 1,
            item["categoryLine"] or "未分类",
            item["styleId"],
        ))
    return {
        "today": today.isoformat(),
        "board": board,
        "styleCount": len(rows),
        "stockoutCount": sum(1 for item in rows if item["stockout"]),
        "brokenStyleCount": sum(1 for item in rows if item["brokenSkus"] > 0),
        "shortStyleCount": sum(1 for item in rows if item["shortSkus"] > 0),
        "styles": rows,
    }


def _snapshot_upsert_sql(table: str = SNAPSHOT_TABLE) -> str:
    fields = ",".join(f"`{column}`" for column in SNAPSHOT_COLUMNS)
    marks = ",".join(["%s"] * len(SNAPSHOT_COLUMNS))
    updates = ",".join(
        f"`{column}`=VALUES(`{column}`)" for column in SNAPSHOT_COLUMNS[1:]
    )
    return (
        f"INSERT INTO `{table}` ({fields}) VALUES ({marks}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )


def _snapshot_tuple(item: dict, computed_at: str) -> tuple:
    labels = item.get("labels") or []
    if isinstance(labels, str):
        label_text = labels
    else:
        label_text = "，".join(str(part) for part in labels if part)
    return (
        str(item.get("styleId") or "").strip(),
        item.get("categoryLine") or "",
        item.get("name") or "",
        item.get("productionMode") or "",
        int(item.get("skuCount") or 0),
        item.get("year") or "",
        item.get("season") or "",
        item.get("category") or "",
        item.get("salePrice") or 0,
        item.get("costPrice") or 0,
        item.get("sales1") or 0,
        item.get("sales3") or 0,
        item.get("sales7") or 0,
        item.get("sales14") or 0,
        item.get("sales15") or 0,
        item.get("sales30") or 0,
        item.get("sales45") or 0,
        item.get("sales60") or 0,
        item.get("sales90") or 0,
        item.get("sales60Online") or 0,
        item.get("sales60Offline") or 0,
        item.get("sales90Online") or 0,
        item.get("sales90Offline") or 0,
        item.get("sales7Online") or 0,
        item.get("sales7Offline") or 0,
        item.get("sales15Online") or 0,
        item.get("sales15Offline") or 0,
        item.get("sales30Online") or 0,
        item.get("sales30Offline") or 0,
        json.dumps(item.get("saleShops") or [], ensure_ascii=False, separators=(",", ":")),
        item.get("salesPrev7") or 0,
        item.get("wowRatio"),
        json.dumps(item.get("salesDaily") or [], separators=(",", ":")),
        int(item.get("brokenSkus") or 0),
        int(item.get("shortSkus") or 0),
        item.get("turnoverDays"),
        item.get("stockoutLabel") or "",
        item.get("replenishQty"),
        item.get("orderQty"),
        item.get("moq"),
        item.get("dailyAvg") or 0,
        item.get("onHand") or 0,
        item.get("qty") or 0,
        item.get("occupy") or 0,
        item.get("inbound") or 0,
        item.get("inQty") or 0,
        item.get("remark") or "",
        label_text,
        int(item.get("missingInventory") or 0),
        computed_at,
    )


def save_style_snapshot(env_path: str, result: dict, *, board: str | None = None) -> int:
    """全量覆盖当前圈定款；不在这次结果里的旧行删掉。鞋服/百货分表，互不删除。"""
    from ..realtime_mirror import ensure_schema

    board = normalize_board(board or result.get("board"))
    table = snapshot_table(board)
    ensure_schema(env_path)
    computed_at = business_now().strftime("%Y-%m-%d %H:%M:%S")
    styles = [
        item for item in (result.get("styles") or [])
        if str(item.get("styleId") or "").strip()
    ]
    rows = [_snapshot_tuple(item, computed_at) for item in styles]
    style_ids = [row[0] for row in rows]
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                for column, ddl in SNAPSHOT_COLUMN_DDL:
                    try:
                        cursor.execute(
                            f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}"
                        )
                    except Exception as exc:
                        if "1060" not in str(exc) and "Duplicate column" not in str(exc):
                            raise
                if style_ids:
                    marks = ",".join(["%s"] * len(style_ids))
                    cursor.execute(
                        f"DELETE FROM `{table}` WHERE style_id NOT IN ({marks})",
                        style_ids,
                    )
                else:
                    cursor.execute(f"DELETE FROM `{table}`")
                if rows:
                    cursor.executemany(_snapshot_upsert_sql(table), rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return len(rows)


def load_style_snapshot(env_path: str, *, board: str = BOARD_APPAREL) -> dict:
    """看板读结果表，不重算。表还没建或为空时返回空清单。"""
    board = normalize_board(board)
    table = snapshot_table(board)
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            if not _table_exists(cursor, table):
                return {"ok": True, "board": board, "computedAt": "", "styleCount": 0,
                        "stockoutCount": 0, "brokenStyleCount": 0,
                        "shortStyleCount": 0, "styles": []}
            cursor.execute(f"SELECT * FROM `{table}`")
            rows = cursor.fetchall()

    def number(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _split_labels(value) -> list:
        if isinstance(value, (list, tuple)):
            return [str(part).strip() for part in value if str(part).strip()]
        text = str(value or "").replace("，", ",")
        return [part.strip() for part in text.split(",") if part.strip()]

    styles = []
    computed_at = ""
    for row in rows:
        stockout = str(row.get("stockout_label") or "") == "缺货"
        turnover = row.get("turnover_days")
        wow = row.get("wow_ratio")
        replenish = row.get("replenish_qty")
        order = row.get("order_qty")
        moq = row.get("moq")
        raw_daily = row.get("sales_daily")
        try:
            daily = json.loads(raw_daily) if isinstance(raw_daily, str) and raw_daily else []
        except json.JSONDecodeError:
            daily = []
        styles.append({
            "styleId": str(row.get("style_id") or ""),
            "name": str(row.get("name") or ""),
            "categoryLine": str(row.get("category_line") or ""),
            "skuCount": int(row.get("sku_count") or 0),
            "sales1": number(row.get("sales_1")),
            "sales3": number(row.get("sales_3")),
            "sales7": number(row.get("sales_7")),
            "sales14": number(row.get("sales_14")),
            "sales15": number(row.get("sales_15")),
            "sales30": number(row.get("sales_30")),
            "sales45": number(row.get("sales_45")),
            "salesPrev7": number(row.get("sales_prev7")),
            "wowRatio": None if wow is None else number(wow),
            "salesDaily": [number(value) for value in daily] if isinstance(daily, list) else [],
            "sales60": number(row.get("sales_60")),
            "sales90": number(row.get("sales_90")),
            "sales7Online": number(row.get("sales_7_online")),
            "sales7Offline": number(row.get("sales_7_offline")),
            "sales15Online": number(row.get("sales_15_online")),
            "sales15Offline": number(row.get("sales_15_offline")),
            "sales30Online": number(row.get("sales_30_online")),
            "sales30Offline": number(row.get("sales_30_offline")),
            "sales60Online": number(row.get("sales_60_online")),
            "sales60Offline": number(row.get("sales_60_offline")),
            "sales90Online": number(row.get("sales_90_online")),
            "sales90Offline": number(row.get("sales_90_offline")),
            "monthlySales": monthly_sales_baihuo(
                row.get("sales_7"), row.get("sales_15"), row.get("sales_30"),
            ) if board == BOARD_BAIHUO else None,
            "saleShops": _parse_sale_shops(row.get("sale_shops")),
            "dailyAvg": number(row.get("daily_avg")),
            "turnoverDays": None if turnover is None else number(turnover),
            "stockout": stockout,
            "brokenSkus": int(row.get("broken_skus") or 0),
            "shortSkus": int(row.get("short_skus") or 0),
            "onHand": number(row.get("on_hand")),
            "qty": number(row.get("qty")),
            "occupy": number(row.get("occupy")),
            "inbound": number(row.get("inbound")),
            "inQty": number(row.get("in_qty")),
            "replenishQty": None if replenish is None else int(replenish),
            "orderQty": None if order is None else int(order),
            "moq": None if moq is None else int(moq),
            "remark": str(row.get("remark") or ""),
            "year": str(row.get("year") or ""),
            "season": str(row.get("season") or ""),
            "category": str(row.get("category") or ""),
            "salePrice": number(row.get("sale_price")),
            "costPrice": number(row.get("cost_price")),
            "labels": _split_labels(row.get("labels")),
            "missingInventory": int(row.get("missing_inventory") or 0),
            "productionMode": str(row.get("production_mode") or ""),
        })
        stamp = str(row.get("computed_at") or "")
        if stamp > computed_at:
            computed_at = stamp
    _attach_last_suppliers(styles, env_path)
    if board == BOARD_APPAREL:
        line_order = {name: index for index, name in enumerate(CATEGORY_LINES)}
        styles.sort(key=lambda item: (line_order.get(item["categoryLine"], 99), item["styleId"]))
    else:
        styles.sort(key=lambda item: (
            0 if item["categoryLine"] == "文创百货" else 1,
            item["categoryLine"] or "未分类",
            item["styleId"],
        ))
    return {
        "ok": True,
        "board": board,
        "computedAt": computed_at,
        "styleCount": len(styles),
        "stockoutCount": sum(1 for item in styles if item["stockout"]),
        "brokenStyleCount": sum(1 for item in styles if item["brokenSkus"] > 0),
        "shortStyleCount": sum(1 for item in styles if item["shortSkus"] > 0),
        "styles": styles,
    }


def _attach_last_suppliers(styles: list[dict], env_path: str) -> None:
    """看板品名后展示最近采购供应商，不写进结果表、不进日均公式。"""
    keys = [str(item.get("styleId") or "").strip() for item in styles if item.get("styleId")]
    suppliers = fetch_last_suppliers(env_path, keys) if env_path and keys else {}
    for item in styles:
        item["lastSupplier"] = suppliers.get(str(item.get("styleId") or "").strip(), "")


def _positive_qty(value) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def format_alert_text(result: dict, *, limit: int = 40) -> str:
    title = "自营百货" if result.get("board") == BOARD_BAIHUO else "鞋服 SPU"
    styles = result.get("styles") or []
    baihuo = result.get("board") == BOARD_BAIHUO
    if baihuo:
        replenish_count = sum(1 for item in styles if _positive_qty(item.get("replenishQty")))
        inbound_count = sum(1 for item in styles if _positive_qty(item.get("inQty")))
        lines = [
            f"{title} {result['today']}：{result['styleCount']} 款，"
            f"缺货 {result['stockoutCount']}，"
            f"需补货 {replenish_count}，"
            f"进货仓待上架 {inbound_count}",
        ]
        hits = [
            item for item in styles
            if item.get("stockout")
            or _positive_qty(item.get("replenishQty"))
            or _positive_qty(item.get("inQty"))
        ]
        hits.sort(key=lambda item: (
            not item.get("stockout"),
            -_positive_qty(item.get("replenishQty")),
            -_positive_qty(item.get("inQty")),
            item.get("styleId") or "",
        ))
    else:
        lines = [
            f"{title} {result['today']}：{result['styleCount']} 款，"
            f"缺货 {result['stockoutCount']}，"
            f"有断码 {result['brokenStyleCount']}，"
            f"有缺码 {result['shortStyleCount']}",
        ]
        hits = [
            item for item in styles
            if item.get("stockout") or item.get("brokenSkus") or item.get("shortSkus")
        ]
        hits.sort(key=lambda item: (
            not item.get("stockout"),
            -int(item.get("brokenSkus") or 0),
            -int(item.get("shortSkus") or 0),
            item.get("styleId") or "",
        ))
    shown = 0
    for item in hits:
        parts = [item.get("styleId") or "", item.get("name") or ""]
        if item.get("stockout"):
            parts.append(f"缺货 周转{item.get('turnoverDays')}")
        if baihuo:
            if _positive_qty(item.get("replenishQty")):
                parts.append(f"建议{item.get('replenishQty'):g}")
        else:
            if item.get("brokenSkus"):
                parts.append(f"断码{item['brokenSkus']}")
            if item.get("shortSkus"):
                parts.append(f"缺码{item['shortSkus']}")
        if baihuo and item.get("sales30") is not None:
            parts.append(f"30天{item['sales30']:g}")
        elif item.get("sales60") is not None:
            parts.append(f"60天{item['sales60']:g}")
        if _positive_qty(item.get("inQty")):
            parts.append(f"进货仓{item['inQty']:g}")
        lines.append(" ".join(str(part) for part in parts if part).strip())
        shown += 1
        if shown >= limit:
            lines.append("…")
            break
    if shown == 0:
        lines.append("当前没有缺货或待补货。" if baihuo else "当前没有缺货、断码或缺码。")
    return "\n".join(lines)
