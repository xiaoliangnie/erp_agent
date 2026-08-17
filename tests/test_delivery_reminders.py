# -*- coding: utf-8 -*-
"""交期四波催办口径。不连数据库，行数据直接构造。"""
import json
import unittest
from pathlib import Path

from backend.delivery_reminders import build_reminders, classify, filter_orders, reminder_markdown

WAVE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "delivery_waves.json"


def line(order_no, *, qty=10, in_qty=0, delivery="", eta="", buyer="张三",
         supplier="佰特", sku="SKU-1", price=20):
    return {
        "采购单号": order_no, "采购日期": "2026-07-01", "状态": "已确认",
        "采购员": buyer, "item_supplier_id": supplier, "仓储方": "主仓",
        "商品编码": sku, "数量": qty, "item_in_qty": in_qty,
        "item_delivery_date": delivery, "最早预计到货日期": eta,
        "基本售价": price, "基本金额": qty * price,
    }


class ClassifyTests(unittest.TestCase):
    def test_buckets_are_mutually_exclusive(self):
        self.assertEqual("unscheduled", classify(None))
        self.assertEqual("overdue", classify(-1))
        self.assertEqual("t1", classify(0))
        self.assertEqual("t1", classify(1))
        self.assertEqual("t10", classify(2))
        self.assertEqual("t10", classify(10))
        self.assertEqual("t20", classify(11))
        self.assertEqual("t20", classify(20))
        self.assertEqual("later", classify(21))


class BuildRemindersTests(unittest.TestCase):
    def test_only_pending_lines_count(self):
        rows = [
            line("A", qty=10, in_qty=10, delivery="2026-08-01"),
            line("A", qty=5, in_qty=1, delivery="2026-08-20"),
        ]
        result = build_reminders(rows, "2026-08-11")
        self.assertEqual(1, len(result["orders"]))
        order = result["orders"][0]
        self.assertEqual(4, order["pendingQty"])
        self.assertEqual("2026-08-20", order["deliveryDate"])
        self.assertEqual("t10", order["bucket"])

    def test_delivery_date_falls_back_to_expected_arrival(self):
        rows = [line("B", delivery="", eta="2026-08-15")]
        order = build_reminders(rows, "2026-08-11")["orders"][0]
        self.assertEqual("2026-08-15", order["deliveryDate"])
        self.assertEqual("预计到货", order["dateSource"])
        rows = [line("B", delivery="2026-08-30", eta="2026-08-15")]
        order = build_reminders(rows, "2026-08-11")["orders"][0]
        self.assertEqual("2026-08-30", order["deliveryDate"])
        self.assertEqual("交期", order["dateSource"])

    def test_order_takes_earliest_pending_line_and_most_urgent_bucket(self):
        rows = [
            line("C", qty=4, delivery="2026-09-30"),
            line("C", qty=6, delivery="2026-08-05"),
        ]
        order = build_reminders(rows, "2026-08-11")["orders"][0]
        self.assertEqual("2026-08-05", order["deliveryDate"])
        self.assertEqual("overdue", order["bucket"])
        self.assertEqual(-6, order["remainingDays"])
        self.assertEqual(10, order["pendingQty"])

    def test_fully_received_orders_disappear(self):
        result = build_reminders([line("D", qty=8, in_qty=8, delivery="2026-01-01")], "2026-08-11")
        self.assertEqual([], result["orders"])
        self.assertEqual(0, result["totals"]["urgentOrderCount"])

    def test_totals_and_grouping(self):
        rows = [
            line("E", qty=10, delivery="2026-08-01", buyer="张三"),
            line("F", qty=20, delivery="2026-08-12", buyer="李四"),
            line("G", qty=30, delivery="2026-12-01", buyer="李四"),
            line("H", qty=40, delivery="", eta="", buyer="王五"),
        ]
        result = build_reminders(rows, "2026-08-11")
        self.assertEqual(4, result["totals"]["orderCount"])
        self.assertEqual(2, result["totals"]["urgentOrderCount"])
        self.assertEqual(30, result["totals"]["urgentPendingQty"])
        self.assertEqual(1, result["buckets"]["later"]["orderCount"])
        self.assertEqual(1, result["buckets"]["unscheduled"]["orderCount"])
        self.assertEqual({"张三", "李四"}, {item["buyer"] for item in result["byBuyer"]})

    def test_orders_sorted_by_urgency(self):
        rows = [
            line("later-one", delivery="2026-12-01"),
            line("t1-one", delivery="2026-08-11"),
            line("overdue-one", delivery="2026-07-01"),
            line("t20-one", delivery="2026-08-27"),
        ]
        result = build_reminders(rows, "2026-08-11")
        self.assertEqual(
            ["overdue-one", "t1-one", "t20-one", "later-one"],
            [order["purchaseOrderNo"] for order in result["orders"]],
        )

    def test_filter_defaults_to_urgent_waves(self):
        rows = [
            line("I", delivery="2026-08-01", buyer="张三"),
            line("J", delivery="2026-12-01", buyer="张三"),
        ]
        result = build_reminders(rows, "2026-08-11")
        orders, matched = filter_orders(result)
        self.assertEqual(1, matched)
        self.assertEqual("I", orders[0]["purchaseOrderNo"])
        orders, _ = filter_orders(result, buckets=["later"])
        self.assertEqual("J", orders[0]["purchaseOrderNo"])
        orders, _ = filter_orders(result, buyer="李四")
        self.assertEqual([], orders)
        orders, _ = filter_orders(result, buyer=["张三", "不存在"])
        self.assertEqual(["I"], [item["purchaseOrderNo"] for item in orders])
        with self.assertRaisesRegex(ValueError, "催办档位"):
            filter_orders(result, buckets=["不存在"])

    def test_markdown_groups_by_buyer(self):
        rows = [line("K", delivery="2026-08-01", buyer="张三"),
                line("L", delivery="2026-08-12", buyer="李四")]
        result = build_reminders(rows, "2026-08-11")
        orders, _ = filter_orders(result)
        text = reminder_markdown(result, orders)
        self.assertIn("**张三**", text)
        self.assertIn("**李四**", text)
        self.assertIn("逾期 10 天", text)
        self.assertIn("剩 1 天", text)


class SharedWaveFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(WAVE_FIXTURE.read_text(encoding="utf-8"))

    def test_thresholds_match_classify(self):
        for row in self.fixture["waveThresholds"]:
            self.assertEqual(row["bucket"], classify(row["days"]), row)

    def test_mixed_order_uses_per_line_fallback(self):
        mixed = self.fixture["mixedOrder"]
        result = build_reminders(mixed["rows"], self.fixture["today"])
        self.assertEqual(1, len(result["orders"]))
        order = result["orders"][0]
        expected = mixed["expected"]
        self.assertEqual(expected["deliveryDate"], order["deliveryDate"])
        self.assertEqual(expected["dateSource"], order["dateSource"])
        self.assertEqual(expected["bucket"], order["bucket"])


if __name__ == "__main__":
    unittest.main()
