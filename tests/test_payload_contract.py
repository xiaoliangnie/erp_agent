# -*- coding: utf-8 -*-
"""位置数组 payload 宽度契约：procurement_data 编码 ↔ tests/fixtures/payload_contract.json。

前端 scripts/check_payload_contract.mjs 用同一份夹具核对 payload.ts 的 WIDTH 常量。
"""
import json
import unittest
from pathlib import Path

from backend.procurement_data import (
    DASHBOARD_LINE_COLUMNS,
    DASHBOARD_LINE_WIDTH,
    DASHBOARD_ORDER_COLUMNS,
    DASHBOARD_ORDER_WIDTH,
    DELIVERY_LINE_COLUMNS,
    DELIVERY_LINE_WIDTH,
    DELIVERY_ORDER_COLUMNS,
    DELIVERY_ORDER_WIDTH,
    build_dashboard_payload,
    build_delivery_payload,
)


FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "payload_contract.json").read_text(encoding="utf-8")
)

SAMPLE_ROW = {
    "采购单号": "PO-1",
    "采购日期": "2026-08-01",
    "状态": "已确认",
    "采购员": "张三",
    "item_supplier_id": "供应商甲",
    "仓储方": "鄂州仓",
    "收货地址": "湖北",
    "付款方式": "现结",
    "外部单号": "EXT-1",
    "审核日期": "2026-08-02",
    "采购单建立时间": "2026-08-01 10:00:00",
    "颜色及规格": "红色;100cm",
    "款式编码": "ST-1",
    "item_sku_other_1": "SPU-1",
    "item_sku_other_2": "春",
    "item_sku_other_3": "毛绒",
    "item_sku_other_10": "线上",
    "item_brand": "品牌",
    "数量": 10,
    "item_in_qty": 2,
    "基本金额": 100,
    "基本售价": 10,
    "最早预计到货日期": "2026-08-20",
    "item_delivery_date": "2026-08-18",
    "商品编码": "SKU-1",
}


class PayloadContractTests(unittest.TestCase):
    def test_python_constants_match_fixture(self):
        self.assertEqual(FIXTURE["dashboard"]["orderWidth"], DASHBOARD_ORDER_WIDTH)
        self.assertEqual(FIXTURE["dashboard"]["lineWidth"], DASHBOARD_LINE_WIDTH)
        self.assertEqual(FIXTURE["delivery"]["orderWidth"], DELIVERY_ORDER_WIDTH)
        self.assertEqual(FIXTURE["delivery"]["lineWidth"], DELIVERY_LINE_WIDTH)
        self.assertEqual(FIXTURE["dashboard"]["orderColumns"], list(DASHBOARD_ORDER_COLUMNS))
        self.assertEqual(FIXTURE["dashboard"]["lineColumns"], list(DASHBOARD_LINE_COLUMNS))
        self.assertEqual(FIXTURE["delivery"]["orderColumns"], list(DELIVERY_ORDER_COLUMNS))
        self.assertEqual(FIXTURE["delivery"]["lineColumns"], list(DELIVERY_LINE_COLUMNS))
        self.assertEqual(len(DASHBOARD_ORDER_COLUMNS), DASHBOARD_ORDER_WIDTH)
        self.assertEqual(len(DASHBOARD_LINE_COLUMNS), DASHBOARD_LINE_WIDTH)

    def test_dashboard_encoder_width(self):
        payload = build_dashboard_payload([SAMPLE_ROW])
        self.assertEqual(1, len(payload["orders"]))
        self.assertEqual(1, len(payload["lines"]))
        self.assertEqual(DASHBOARD_ORDER_WIDTH, len(payload["orders"][0]))
        self.assertEqual(DASHBOARD_LINE_WIDTH, len(payload["lines"][0]))
        self.assertEqual("PO-1", payload["orders"][0][0])
        self.assertEqual("SKU-1", payload["lines"][0][-1])
        self.assertEqual(list(DASHBOARD_ORDER_COLUMNS), payload["columns"]["orders"])
        self.assertEqual(list(DASHBOARD_LINE_COLUMNS), payload["columns"]["lines"])

    def test_delivery_encoder_width(self):
        payload = build_delivery_payload([SAMPLE_ROW])
        self.assertEqual(DELIVERY_ORDER_WIDTH, len(payload["orders"][0]))
        self.assertEqual(DELIVERY_LINE_WIDTH, len(payload["lines"][0]))
        self.assertEqual("2026-08-18", payload["lines"][0][8])
        self.assertEqual("2026-08-20", payload["lines"][0][9])
        self.assertEqual(list(DELIVERY_ORDER_COLUMNS), payload["columns"]["orders"])
        self.assertEqual(list(DELIVERY_LINE_COLUMNS), payload["columns"]["lines"])


if __name__ == "__main__":
    unittest.main()
