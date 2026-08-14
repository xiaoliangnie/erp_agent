# -*- coding: utf-8 -*-
"""采购合同模型与票种规则测试。

离线用例走 tests/fixtures/contract_order_604264.json，不连库。
对真实采购单 604264 的 live 断言保留，设置 CONTRACT_LIVE_TESTS=1 才跑。
"""
import os
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from backend.contracts import build_contract_model, invoice_term, parse_quantity
from backend.database import fetch_contract_order, load_contract_order_fixture


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "contract_order_604264.json"
LIVE = os.environ.get("CONTRACT_LIVE_TESTS", "").strip().lower() in ("1", "true", "yes", "on")


def _offline_model(po_id, invoice_type, **kwargs):
    fetched = load_contract_order_fixture(FIXTURE, po_id)
    return build_contract_model(
        po_id, invoice_type, fetched=fetched, saved_gb=({}, {}),
        gb_lookup=lambda _no: None, **kwargs,
    )


class InvoiceTermTest(unittest.TestCase):
    def test_no_invoice_clause_uses_zero_rate(self):
        clause = invoice_term("no_invoice", 0)
        self.assertIn("不开票价格", clause)
        self.assertIn("税率为0%", clause)

    def test_special_invoice_clause_uses_selected_rate(self):
        clause = invoice_term("special_invoice", 13)
        self.assertIn("增值税专用发票", clause)
        self.assertIn("税率为13%", clause)

    def test_normal_invoice_allows_zero_rate(self):
        model = _offline_model(
            "604264", "normal_invoice", tax_rate=0,
            price_overrides={"BH25701004-02202": 20.8},
        )
        self.assertEqual(model["invoice"]["taxRate"], 0)
        self.assertIn("增值税普通发票", model["terms"][3])
        self.assertIn("税率为0%", model["terms"][3])


class ContractModelOfflineTest(unittest.TestCase):
    def test_fixture_order_uses_mapping_dates_and_special_price(self):
        model = _offline_model("604264", "special_invoice")
        self.assertEqual(model["purchaseOrderNo"], "604264")
        self.assertEqual(model["orderDate"], "2026-06-05")
        self.assertEqual(model["deliveryDate"], "2026-07-09")
        self.assertEqual(model["supplier"]["shortName"], "佰特")
        self.assertEqual(model["items"][0]["unitPrice"], 21.7)
        self.assertEqual("", model["items"][0]["gbStandard"])
        self.assertEqual("6978340007079", model["items"][0]["nationalCode"])
        self.assertIn("采购单号604264", model["paymentTerms"])
        self.assertIn("税率为13%", model["terms"][3])

    def test_normal_invoice_accepts_employee_overrides(self):
        model = _offline_model(
            "604264", "normal_invoice", tax_rate=3,
            price_overrides={"BH25701004-02202": 20.8},
        )
        self.assertEqual(model["invoice"]["taxRate"], 3)
        self.assertEqual(model["items"][0]["unitPrice"], 20.8)
        self.assertIn("增值税普通发票", model["terms"][3])
        self.assertIn("税率为3%", model["terms"][3])

    def test_fetch_contract_order_reads_fixture_path(self):
        order, items = fetch_contract_order("604264", fixture_path=str(FIXTURE))
        self.assertEqual(604264, order["po_id"])
        self.assertEqual("BH25701004-02202", items[0]["sku_id"])

    def test_fetch_contract_order_rejects_mismatched_po(self):
        with self.assertRaises(ValueError):
            fetch_contract_order("1", fixture_path=str(FIXTURE))

    def test_decimal_quantity_is_kept_not_truncated(self):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        items = deepcopy(items)
        items[0]["qty"] = 100.6
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
        )
        self.assertEqual(Decimal("100.6"), model["items"][0]["quantity"])

    def test_negative_quantity_is_rejected(self):
        self.assertEqual(Decimal("0"), parse_quantity(None))
        self.assertEqual(Decimal("100.6"), parse_quantity(100.6))
        with self.assertRaisesRegex(ValueError, "不能为负数"):
            parse_quantity(-1)
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        items = deepcopy(items)
        items[0]["qty"] = -0.5
        with self.assertRaisesRegex(ValueError, "不能为负数"):
            build_contract_model(
                "604264", "special_invoice", fetched=(order, items),
                saved_gb=({}, {}), gb_lookup=lambda _no: None,
            )


@unittest.skipUnless(LIVE, "设置 CONTRACT_LIVE_TESTS=1 才连 hanli.env 跑 604264")
class ContractModelLiveTest(unittest.TestCase):
    def test_sample_order_uses_erp_dates_and_mapping(self):
        model = build_contract_model("604264", "special_invoice")
        self.assertEqual(model["purchaseOrderNo"], "604264")
        self.assertEqual(model["orderDate"], "2026-06-05")
        self.assertEqual(model["deliveryDate"], "2026-07-09")
        self.assertEqual(model["supplier"]["shortName"], "佰特")
        self.assertEqual(model["items"][0]["unitPrice"], 21.7)
        self.assertIn("gbStandard", model["items"][0])
        self.assertIn("nationalCode", model["items"][0])
        self.assertIn("采购单号604264", model["paymentTerms"])
        self.assertIn("税率为13%", model["terms"][3])

    def test_normal_invoice_accepts_employee_overrides(self):
        model = build_contract_model(
            "604264", "normal_invoice", tax_rate=3,
            price_overrides={"BH25701004-02202": 20.8},
        )
        self.assertEqual(model["invoice"]["taxRate"], 3)
        self.assertEqual(model["items"][0]["unitPrice"], 20.8)
        self.assertIn("增值税普通发票", model["terms"][3])
        self.assertIn("税率为3%", model["terms"][3])


if __name__ == "__main__":
    unittest.main()
