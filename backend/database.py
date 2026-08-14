# -*- coding: utf-8 -*-
"""数据库连接、表结构与采购明细查询。"""
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .business_time import BUSINESS_TIMEZONE, business_today

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError as exc:  # pragma: no cover - 给未安装依赖时更清楚的提示
    raise SystemExit("缺少 PyMySQL，请先运行：python3 -m pip install -r requirements.txt") from exc


TABLE_NAME = "purchase_order_lines"
REALTIME_MAIN_TABLE = "realtime_purchase_orders"
REALTIME_ITEM_TABLE = "realtime_purchase_order_items"
REALTIME_SYNC_TABLE = "realtime_sync_state"
REALTIME_PRODUCT_TABLE = "realtime_products"
REALTIME_SUPPLIER_TABLE = "realtime_suppliers"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    source_key CHAR(64) NOT NULL PRIMARY KEY,
    purchase_order_no VARCHAR(64) NOT NULL,
    line_no VARCHAR(32) NOT NULL DEFAULT '',
    sku_code VARCHAR(128) NOT NULL DEFAULT '',
    style_code VARCHAR(128) NOT NULL DEFAULT '',
    product_name VARCHAR(255) NOT NULL DEFAULT '',
    color_spec VARCHAR(255) NOT NULL DEFAULT '',
    quantity DECIMAL(18, 4) NOT NULL DEFAULT 0,
    purchase_date DATE NULL,
    status VARCHAR(64) NOT NULL DEFAULT '',
    buyer VARCHAR(128) NOT NULL DEFAULT '',
    audit_date DATETIME NULL,
    earliest_arrival_date DATE NULL,
    expected_arrival_quantity DECIMAL(18, 4) NOT NULL DEFAULT 0,
    unit_price DECIMAL(18, 4) NOT NULL DEFAULT 0,
    amount DECIMAL(18, 4) NOT NULL DEFAULT 0,
    item_delivery_date DATE NULL,
    item_poi_id VARCHAR(64) NOT NULL DEFAULT '',
    spu VARCHAR(255) NOT NULL DEFAULT '',
    season VARCHAR(128) NOT NULL DEFAULT '',
    category VARCHAR(128) NOT NULL DEFAULT '',
    channel VARCHAR(128) NOT NULL DEFAULT '',
    in_quantity DECIMAL(18, 4) NOT NULL DEFAULT 0,
    brand VARCHAR(128) NOT NULL DEFAULT '',
    supplier_id VARCHAR(128) NOT NULL DEFAULT '',
    warehouse VARCHAR(128) NOT NULL DEFAULT '',
    receive_address VARCHAR(512) NOT NULL DEFAULT '',
    payment_method VARCHAR(128) NOT NULL DEFAULT '',
    external_order_no VARCHAR(128) NOT NULL DEFAULT '',
    source_payload JSON NULL,
    synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_purchase_date (purchase_date),
    KEY idx_delivery_date (item_delivery_date),
    KEY idx_arrival_date (earliest_arrival_date),
    KEY idx_buyer (buyer),
    KEY idx_purchase_order (purchase_order_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def load_env(path):
    """读取简单 KEY=VALUE 文件，兼容 Windows 换行与引号。"""
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    required = ["MYSQL_HOST", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError(f"{path} 缺少配置：" + ", ".join(missing))
    return values


def load_all_env(path):
    """读取全部可选配置，不校验具体数据源。"""
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class DatabaseUnavailable(pymysql.err.OperationalError):
    """握手阶段就失败。沿用 PyMySQL 的错误码，只把「连不上谁」写进消息。

    连接失败和查询中途断流在 PyMySQL 里都是 2013，光看错误码分不出是地址写错还是网络抖动。
    """


def describe_target(env_path):
    """返回可安全打印的目标描述，不含账号与口令。"""
    env = load_env(env_path)
    return f"{env['MYSQL_HOST']}:{env.get('MYSQL_PORT', '3306')}/{env['MYSQL_DATABASE']}"


def connect(env_path, *, autocommit=False):
    """创建 MySQL 连接；凭证仅在服务端进程内使用。"""
    env = load_env(env_path)
    try:
        return pymysql.connect(
            host=env["MYSQL_HOST"],
            port=int(env.get("MYSQL_PORT", "3306")),
            database=env["MYSQL_DATABASE"],
            user=env["MYSQL_USER"],
            password=env["MYSQL_PASSWORD"],
            charset=env.get("MYSQL_CHARSET", "utf8mb4"),
            connect_timeout=max(1, int(env.get("MYSQL_CONNECT_TIMEOUT", "10"))),
            # 镜像库在远端；年度看板会读取数万行，30 秒容易被网络抖动误杀。
            read_timeout=max(5, int(env.get("MYSQL_READ_TIMEOUT", "90"))),
            write_timeout=max(5, int(env.get("MYSQL_WRITE_TIMEOUT", "90"))),
            autocommit=autocommit,
            cursorclass=DictCursor,
        )
    except pymysql.err.OperationalError as exc:
        reason = exc.args[1] if len(exc.args) > 1 else str(exc)
        raise DatabaseUnavailable(
            exc.args[0] if exc.args else 2003,
            f"连不上镜像库 {describe_target(env_path)}（配置来自 {Path(env_path).name}）：{reason}。"
            "MySQL 握手没有完成，请依次检查该地址是否写对、本机代理或防火墙是否拦了这个端口、"
            "数据库白名单里有没有这台机器的出口 IP。",
        ) from exc


TRANSIENT_MYSQL_CODES = {2006, 2013}


def is_transient_mysql_error(exc):
    """识别连接被服务端关闭/查询中断，供幂等读取和镜像 upsert 重试。"""
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, pymysql.err.OperationalError):
            try:
                if int(current.args[0]) in TRANSIENT_MYSQL_CODES:
                    return True
            except (IndexError, TypeError, ValueError):
                pass
        current = current.__cause__ or current.__context__
    return False


def read_query(env_path, sql, params=(), *, one=False, retries=1):
    """在新连接上执行只读 SQL；远程库瞬断时安全重试一次。"""
    for attempt in range(max(0, int(retries)) + 1):
        try:
            with connect(env_path, autocommit=True) as conn:
                with conn.cursor() as cursor:
                    if params:
                        cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                    return cursor.fetchone() if one else cursor.fetchall()
        except pymysql.err.OperationalError as exc:
            if attempt >= retries or not is_transient_mysql_error(exc):
                raise
            time.sleep(0.25 * (attempt + 1))


def fetch_purchase_rows(env_path):
    """查询看板需要的采购事实字段，并恢复为生成器使用的业务列名。"""
    sql = f"""
        SELECT
            purchase_order_no, line_no, sku_code, style_code, product_name,
            color_spec, quantity, purchase_date, status, buyer, audit_date,
            earliest_arrival_date, expected_arrival_quantity, unit_price, amount,
            item_delivery_date, item_poi_id, spu, season, category, channel,
            in_quantity, brand, supplier_id, warehouse, receive_address,
            payment_method, external_order_no
        FROM {TABLE_NAME}
        ORDER BY purchase_date, purchase_order_no, line_no, item_poi_id
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            records = cursor.fetchall()

    column_map = {
        "purchase_order_no": "采购单号", "line_no": "序号",
        "sku_code": "商品编码", "style_code": "款式编码",
        "product_name": "商品名称", "color_spec": "颜色及规格",
        "quantity": "数量", "purchase_date": "采购日期", "status": "状态",
        "buyer": "采购员", "audit_date": "审核日期",
        "earliest_arrival_date": "最早预计到货日期",
        "expected_arrival_quantity": "预计到货数量", "unit_price": "基本售价",
        "amount": "基本金额", "item_delivery_date": "item_delivery_date",
        "item_poi_id": "item_poi_id", "spu": "item_sku_other_1",
        "season": "item_sku_other_2", "category": "item_sku_other_3",
        "channel": "item_sku_other_10", "in_quantity": "item_in_qty",
        "brand": "item_brand", "supplier_id": "item_supplier_id",
        "warehouse": "仓储方", "receive_address": "收货地址",
        "payment_method": "付款方式", "external_order_no": "外部单号",
    }
    return [{column_map[key]: value for key, value in row.items()} for row in records]


def fetch_realtime_years(env_path="hanli.env"):
    """返回实时采购库中可用的统计年度。"""
    sql = f"""
        SELECT DISTINCT CAST(YEAR(po_date) AS CHAR) AS year
        FROM `{REALTIME_MAIN_TABLE}`
        WHERE po_date IS NOT NULL
          AND COALESCE(status, '') NOT IN ('Cancelled', 'Delete', 'Merged')
          AND po_date < %s
        ORDER BY year DESC
    """
    upper = business_today() + timedelta(days=1)
    years = [row["year"] for row in read_query(env_path, sql, (upper,))]
    return [year for year in years if str(year).isdigit() and int(year) <= business_today().year]


def purchase_window_warning(year) -> str | None:
    """当前年度明细窗口截到今天时给出提示，与 fetch_realtime_purchase_rows 同一上界。"""
    year = int(year)
    natural_end = date(year + 1, 1, 1)
    end = min(natural_end, business_today() + timedelta(days=1))
    if end < natural_end:
        return f"{year} 年明细只读到 {business_today().isoformat()}，之后下的采购单未纳入"
    return None


def fetch_realtime_purchase_rows(year, env_path="hanli.env"):
    """按月分批读取年度明细，避免远程 MySQL 大结果集传输中断。"""
    year = int(year)
    start = date(year, 1, 1)
    natural_end = date(year + 1, 1, 1)
    end = min(natural_end, business_today() + timedelta(days=1))
    sql = f"""
        SELECT
            m.po_id AS purchase_order_no,
            i.poi_id AS line_no,
            i.sku_id AS sku_code,
            i.i_id AS style_code,
            i.name AS product_name,
            COALESCE(i.properties_value, '') AS color_spec,
            COALESCE(i.qty, 0) AS quantity,
            LEFT(m.po_date, 10) AS purchase_date,
            m.po_date AS purchase_created_at,
            CASE
                WHEN m.status IN ('Confirmed', 'Finished') THEN '已确认'
                WHEN m.status = 'WaitConfirm' THEN '待审核'
                ELSE COALESCE(m.status, '')
            END AS status,
            COALESCE(m.purchaser_name, '') AS buyer,
            LEFT(COALESCE(m.confirm_date, ''), 19) AS audit_date,
            LEFT(COALESCE(i.delivery_date, ''), 10) AS earliest_arrival_date,
            COALESCE(i.plan_arrive_qty, 0) AS expected_arrival_quantity,
            COALESCE(i.price, 0) AS unit_price,
            COALESCE(i.amount, i.qty * i.price, 0) AS amount,
            LEFT(COALESCE(i.delivery_date, ''), 10) AS item_delivery_date,
            i.poi_id AS item_poi_id,
            COALESCE(NULLIF(i.name, ''), i.sku_id, '未命名') AS spu,
            '' AS season,
            '未分类' AS category,
            '' AS channel,
            COALESCE(i.in_qty, 0) AS in_quantity,
            COALESCE(i.brand, '') AS brand,
            COALESCE(NULLIF(m.seller, ''), CONCAT('供应商 ', m.supplier_id), '未知') AS supplier,
            COALESCE(m.wms_co_name, '') AS warehouse,
            COALESCE(m.send_address, '') AS receive_address,
            COALESCE(m.payment_method, '') AS payment_method,
            COALESCE(m.so_id, '') AS external_order_no
        FROM `{REALTIME_MAIN_TABLE}` AS m
        STRAIGHT_JOIN `{REALTIME_ITEM_TABLE}` AS i ON i.po_id = m.po_id
        WHERE m.po_date >= %s
          AND m.po_date < %s
          AND COALESCE(m.status, '') NOT IN ('Cancelled', 'Delete', 'Merged')
        ORDER BY m.po_date, m.po_id, i.poi_id
    """
    records = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(
            date(chunk_start.year + (chunk_start.month == 12), chunk_start.month % 12 + 1, 1),
            end,
        )
        # 一个月失败只重试该月，不必重新传输整个年度的数万行。
        records.extend(read_query(
            env_path, sql, (chunk_start.isoformat(), chunk_end.isoformat()), retries=2,
        ))
        chunk_start = chunk_end

    column_map = {
        "purchase_order_no": "采购单号", "line_no": "序号",
        "sku_code": "商品编码", "style_code": "款式编码",
        "product_name": "商品名称", "color_spec": "颜色及规格",
        "quantity": "数量", "purchase_date": "采购日期",
        "purchase_created_at": "采购单建立时间", "status": "状态",
        "buyer": "采购员", "audit_date": "审核日期",
        "earliest_arrival_date": "最早预计到货日期",
        "expected_arrival_quantity": "预计到货数量", "unit_price": "基本售价",
        "amount": "基本金额", "item_delivery_date": "item_delivery_date",
        "item_poi_id": "item_poi_id", "spu": "item_sku_other_1",
        "season": "item_sku_other_2", "category": "item_sku_other_3",
        "channel": "item_sku_other_10", "in_quantity": "item_in_qty",
        "brand": "item_brand", "supplier": "item_supplier_id",
        "warehouse": "仓储方", "receive_address": "收货地址",
        "payment_method": "付款方式", "external_order_no": "外部单号",
    }
    return [{column_map[key]: value for key, value in row.items()} for row in records]


def load_contract_order_fixture(path, po_id=None):
    """从 JSON 夹具读取一张采购单，形状与 `fetch_contract_order` 相同。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "order" not in data or "items" not in data:
        raise ValueError("合同夹具必须包含 order 和 items")
    order = data["order"]
    items = data["items"]
    if not isinstance(order, dict) or not isinstance(items, list) or not items:
        raise ValueError("合同夹具 order 必须是对象、items 必须是非空数组")
    if po_id is not None and str(order.get("po_id") or "") != str(po_id):
        raise ValueError(f"合同夹具采购单号是 {order.get('po_id')}，不是 {po_id}")
    return order, items


def fetch_contract_order(po_id, env_path="hanli.env", *, fixture_path=None):
    """读取一张采购单及其全部合同明细。"""
    fixture_path = fixture_path or os.environ.get("CONTRACT_ORDER_FIXTURE", "").strip() or None
    if fixture_path:
        return load_contract_order_fixture(fixture_path, po_id)
    main_sql = f"""
        SELECT po_id, po_date, so_id, status, supplier_id, seller,
               purchaser_name, send_address, payment_method, wms_co_name,
               confirm_date, remark
        FROM `{REALTIME_MAIN_TABLE}`
        WHERE po_id = %s
        LIMIT 1
    """
    item_sql = f"""
        SELECT poi_id, sku_id, i_id, name,
               COALESCE(properties_value, '') AS properties_value,
               COALESCE(qty, 0) AS qty, COALESCE(price, 0) AS price,
               COALESCE(amount, qty * price, 0) AS amount,
               delivery_date, COALESCE(plan_arrive_qty, 0) AS plan_arrive_qty,
               COALESCE(in_qty, 0) AS in_qty, remark
        FROM `{REALTIME_ITEM_TABLE}`
        WHERE po_id = %s
        ORDER BY poi_id
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(main_sql, (int(po_id),))
            order = cursor.fetchone()
            if not order:
                raise ValueError(f"采购单 {po_id} 不存在")
            cursor.execute(item_sql, (int(po_id),))
            items = cursor.fetchall()
    if not items:
        raise ValueError(f"采购单 {po_id} 没有商品明细")
    return order, items


def fetch_contract_order_choices(env_path="hanli.env", limit=100, query=""):
    """搜索本年度可用于生成合同的采购单，供页面选择。"""
    limit = max(1, min(int(limit), 1000))
    today = business_today()
    start_date = f"{today.year:04d}-01-01"
    end_date = (today + timedelta(days=1)).isoformat()
    query = str(query or "").strip()
    search_sql = ""
    params = [start_date, end_date]
    if query:
        search_sql = """
          AND (CAST(m.po_id AS CHAR) LIKE %s
               OR COALESCE(m.seller, '') LIKE %s
               OR COALESCE(m.purchaser_name, '') LIKE %s)
        """
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern])
    params.append(limit)
    sql = f"""
        SELECT m.po_id, LEFT(m.po_date, 10) AS po_date,
               COALESCE(m.seller, '') AS seller,
               COALESCE(m.purchaser_name, '') AS purchaser_name,
               COALESCE(m.status, '') AS status
        FROM `{REALTIME_MAIN_TABLE}` AS m
        WHERE LEFT(m.po_date, 10) >= %s
          AND LEFT(m.po_date, 10) < %s
          AND COALESCE(m.status, '') NOT IN ('Cancelled', 'Delete', 'Merged')
          AND EXISTS (
              SELECT 1 FROM `{REALTIME_ITEM_TABLE}` AS i WHERE i.po_id = m.po_id
          )
          {search_sql}
        ORDER BY m.po_date DESC, m.po_id DESC
        LIMIT %s
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    return [{
        "purchaseOrderNo": str(row.get("po_id") or ""),
        "orderDate": day_value(row.get("po_date")),
        "supplier": str(row.get("seller") or ""),
        "purchaser": str(row.get("purchaser_name") or ""),
        "status": str(row.get("status") or ""),
    } for row in rows]


def fetch_exchange_products(env_path="hanli.env", limit=100, query=""):
    """优先读取商品主数据，并兼容尚未被商品接口覆盖的采购明细 SKU。"""
    limit = max(1, min(int(limit), 500))
    query = str(query or "").strip()
    product_params = []
    product_where = "WHERE COALESCE(p.sku_id, '') <> ''"
    if query:
        product_where += """ AND (p.sku_id LIKE %s
                               OR COALESCE(p.i_id, '') LIKE %s
                               OR COALESCE(p.name, '') LIKE %s
                               OR COALESCE(p.properties_value, '') LIKE %s)"""
        pattern = f"%{query}%"
        product_params.extend([pattern, pattern, pattern, pattern])
    product_params.append(limit)
    product_sql = f"""
        SELECT p.sku_id, COALESCE(p.i_id, '') AS i_id,
               COALESCE(p.name, '') AS name,
               COALESCE(p.properties_value, '') AS properties_value,
               COALESCE(p.category, '') AS category,
               0 AS purchase_line_count
        FROM `{REALTIME_PRODUCT_TABLE}` AS p
        {product_where}
        ORDER BY p.modified DESC, p.sku_id
        LIMIT %s
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(product_sql, product_params)
            rows = list(cursor.fetchall())
            if len(rows) < limit:
                fallback_params = []
                fallback_where = "WHERE COALESCE(i.sku_id, '') <> ''"
                if query:
                    fallback_where += """ AND (i.sku_id LIKE %s
                                            OR COALESCE(i.i_id, '') LIKE %s
                                            OR COALESCE(i.name, '') LIKE %s
                                            OR COALESCE(i.properties_value, '') LIKE %s)"""
                    fallback_params.extend([pattern, pattern, pattern, pattern])
                fallback_params.append(limit - len(rows))
                cursor.execute(f"""
                    SELECT i.sku_id,
                           MAX(COALESCE(i.i_id, '')) AS i_id,
                           MAX(COALESCE(i.name, '')) AS name,
                           MAX(COALESCE(i.properties_value, '')) AS properties_value,
                           '' AS category,
                           COUNT(*) AS purchase_line_count
                    FROM `{REALTIME_ITEM_TABLE}` AS i
                    {fallback_where}
                      AND NOT EXISTS (
                          SELECT 1 FROM `{REALTIME_PRODUCT_TABLE}` p WHERE p.sku_id = i.sku_id
                      )
                    GROUP BY i.sku_id
                    ORDER BY purchase_line_count DESC, i.sku_id
                    LIMIT %s
                """, fallback_params)
                rows.extend(cursor.fetchall())
    return [{
        "sku": str(row.get("sku_id") or ""),
        "styleCode": str(row.get("i_id") or ""),
        "name": str(row.get("name") or ""),
        "properties": str(row.get("properties_value") or ""),
        "category": str(row.get("category") or ""),
        "purchaseLineCount": int(row.get("purchase_line_count") or 0),
    } for row in rows]


def day_value(value):
    """把数据库日期值稳定转换为 YYYY-MM-DD。"""
    return str(value or "")[:10]


def fetch_realtime_sync_state(env_path="hanli.env"):
    """返回本地镜像最近写入时间，供页面识别 API 同步延迟。"""
    sql = f"""
        SELECT DATE_FORMAT(NOW(), '%Y-%m-%d %H:%i:%s') AS database_now,
               DATE_FORMAT(GREATEST(
                   COALESCE((SELECT MAX(api_synced_at) FROM `{REALTIME_MAIN_TABLE}`), '1970-01-01'),
                   COALESCE((SELECT MAX(api_synced_at) FROM `{REALTIME_ITEM_TABLE}`), '1970-01-01'),
                   COALESCE((
                       SELECT last_success_at FROM `{REALTIME_SYNC_TABLE}`
                       WHERE source_name = 'purchase' AND status = 'success'
                   ), '1970-01-01')
               ), '%Y-%m-%d %H:%i:%s') AS last_synced_at,
               COALESCE((
                   SELECT GROUP_CONCAT(CONCAT(source_name, ':', status) ORDER BY source_name SEPARATOR ',')
                   FROM `{REALTIME_SYNC_TABLE}`
               ), '') AS source_status
    """
    row = read_query(env_path, sql, one=True) or {}
    synced_at = str(row.get("last_synced_at") or "")
    synced = None
    if synced_at and not synced_at.startswith("1970-"):
        try:
            synced = datetime.strptime(synced_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BUSINESS_TIMEZONE)
        except ValueError:
            synced_at = ""
    database_now = str(row.get("database_now") or "")
    lag_minutes = None
    if synced is not None and database_now:
        try:
            current = datetime.strptime(database_now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BUSINESS_TIMEZONE)
            lag_minutes = max(0, int((current - synced).total_seconds() // 60))
        except ValueError:
            pass
    return {
        "databaseNow": database_now,
        "syncedAt": synced_at,
        "syncLagMinutes": lag_minutes,
        "fresh": lag_minutes is not None and lag_minutes <= 15,
        "sourceStatus": str(row.get("source_status") or ""),
    }
