# -*- coding: utf-8 -*-
"""把采购 CSV 幂等同步到 MySQL 采购明细事实表。"""
import argparse
import csv
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import CREATE_TABLE_SQL, TABLE_NAME, connect  # noqa: E402


COLUMNS = [
    ("purchase_order_no", "采购单号", "text"), ("line_no", "序号", "text"),
    ("sku_code", "商品编码", "text"), ("style_code", "款式编码", "text"),
    ("product_name", "商品名称", "text"), ("color_spec", "颜色及规格", "text"),
    ("quantity", "数量", "number"), ("purchase_date", "采购日期", "date"),
    ("status", "状态", "text"), ("buyer", "采购员", "text"),
    ("audit_date", "审核日期", "datetime"),
    ("earliest_arrival_date", "最早预计到货日期", "date"),
    ("expected_arrival_quantity", "预计到货数量", "number"),
    ("unit_price", "基本售价", "number"), ("amount", "基本金额", "number"),
    ("item_delivery_date", "item_delivery_date", "date"),
    ("item_poi_id", "item_poi_id", "text"), ("spu", "item_sku_other_1", "text"),
    ("season", "item_sku_other_2", "text"), ("category", "item_sku_other_3", "text"),
    ("channel", "item_sku_other_10", "text"), ("in_quantity", "item_in_qty", "number"),
    ("brand", "item_brand", "text"), ("supplier_id", "item_supplier_id", "text"),
    ("warehouse", "仓储方", "text"), ("receive_address", "收货地址", "text"),
    ("payment_method", "付款方式", "text"), ("external_order_no", "外部单号", "text"),
]


def clean(value, kind):
    value = (value or "").strip()
    if kind == "text":
        return value
    if kind == "date":
        return value[:10] if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-" else None
    if kind == "datetime":
        return value[:19] if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-" else None
    try:
        return str(Decimal(value or "0"))
    except InvalidOperation:
        return "0"


def source_key(row):
    identity = "\x1f".join([
        (row.get("采购单号") or "").strip(), (row.get("序号") or "").strip(),
        (row.get("item_poi_id") or "").strip(), (row.get("商品编码") or "").strip(),
    ])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def batches(items, size=200):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def main():
    parser = argparse.ArgumentParser(description="同步采购 CSV 到 MySQL")
    from backend.paths import DATA_DIR
    parser.add_argument("--csv", default=str(DATA_DIR / "snapshots" / "采购单完整数据.csv"))
    parser.add_argument("--env", required=True, help="目标 MySQL 的 env 文件路径")
    args = parser.parse_args()

    path = Path(args.csv)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("CSV 没有数据，未写入数据库。")

    names = [name for name, _, _ in COLUMNS]
    insert_names = ["source_key", *names, "source_payload"]
    placeholders = ", ".join(["%s"] * len(insert_names))
    updates = ", ".join(f"{name}=VALUES({name})" for name in [*names, "source_payload"])
    sql = (
        f"INSERT INTO {TABLE_NAME} ({', '.join(insert_names)}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    values = []
    for row in rows:
        record = [source_key(row)]
        record.extend(clean(row.get(csv_name), kind) for _, csv_name, kind in COLUMNS)
        record.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        values.append(tuple(record))

    with connect(args.env) as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(CREATE_TABLE_SQL)
                for batch in batches(values):
                    cursor.executemany(sql, batch)
                cursor.execute(f"SELECT COUNT(*) AS total FROM {TABLE_NAME}")
                total = cursor.fetchone()["total"]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    print(f"数据库同步完成：本次 {len(rows)} 行，表内 {total} 行。")


if __name__ == "__main__":
    main()
