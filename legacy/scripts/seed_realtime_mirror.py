# -*- coding: utf-8 -*-
"""把现有只读采购镜像一次性灌入 hanli.env 的规范化实时镜像表。"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import connect  # noqa: E402
from backend.business_time import BUSINESS_TIMEZONE  # noqa: E402
from backend.realtime_mirror import ensure_schema, upsert_purchase_records  # noqa: E402


SOURCE_MAIN = "10004_jst_purchase-main"
SOURCE_ITEMS = "10004_jst_purchase-main_items"


def main():
    parser = argparse.ArgumentParser(description="初始化 API 实时采购镜像")
    parser.add_argument("--source-env", default=str(ROOT / "hanli02.env"))
    parser.add_argument("--target-env", default=str(ROOT / "hanli.env"))
    parser.add_argument("--batch-size", type=int, default=300)
    args = parser.parse_args()
    batch_size = max(1, min(args.batch_size, 1000))
    ensure_schema(args.target_env)

    last_po_id = -1
    totals = {"orders": 0, "items": 0}
    while True:
        with connect(args.source_env, autocommit=True) as source:
            with source.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT po_date, po_id, so_id, remark, status, supplier_id, seller,
                           purchaser_name, send_address, payment_method, wms_co_name,
                           confirm_date, finish_time, modified, __dm_extract_at
                    FROM `{SOURCE_MAIN}`
                    WHERE po_id > %s
                    ORDER BY po_id
                    LIMIT %s
                    """,
                    (last_po_id, batch_size),
                )
                orders = cursor.fetchall()
                if not orders:
                    break
                po_ids = [row["po_id"] for row in orders]
                marks = ",".join(["%s"] * len(po_ids))
                cursor.execute(
                    f"""
                    SELECT sku_id, name, qty, price, i_id, po_id, poi_id,
                           delivery_date, remark, plan_arrive_qty, field_3,
                           `inQty` AS in_qty, properties_value, amount, custom_info
                    FROM `{SOURCE_ITEMS}`
                    WHERE po_id IN ({marks})
                    ORDER BY po_id, poi_id
                    """,
                    po_ids,
                )
                item_rows = cursor.fetchall()

        grouped = {str(po_id): [] for po_id in po_ids}
        for item in item_rows:
            item["sku_other_1"] = item.get("name") or item.get("sku_id") or ""
            item["sku_other_3"] = "未分类"
            grouped.setdefault(str(item["po_id"]), []).append(item)
        records = []
        for order in orders:
            order["items"] = grouped.get(str(order["po_id"]), [])
            records.append(order)
        extract_ms = max((float(row.get("__dm_extract_at") or 0) for row in orders), default=0)
        synced_at = (
            datetime.fromtimestamp(extract_ms / 1000, timezone.utc).astimezone(BUSINESS_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            if extract_ms else "1970-01-01 00:00:00"
        )
        counts = upsert_purchase_records(args.target_env, records, synced_at=synced_at)
        totals["orders"] += counts["orders"]
        totals["items"] += counts["items"]
        last_po_id = max(po_ids)
        print(f"已初始化采购单 {totals['orders']} 张、明细 {totals['items']} 行")

    print(f"初始化完成：采购单 {totals['orders']} 张、明细 {totals['items']} 行")


if __name__ == "__main__":
    main()
