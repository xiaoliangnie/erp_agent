# -*- coding: utf-8 -*-
"""采购合同模型与票种规则测试。"""
import unittest

from backend.contracts import build_contract_model, invoice_term


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
        model = build_contract_model(
            "604264", "normal_invoice", tax_rate=0,
            price_overrides={"BH25701004-02202": 20.8},
        )
        self.assertEqual(model["invoice"]["taxRate"], 0)
        self.assertIn("增值税普通发票", model["terms"][3])
        self.assertIn("税率为0%", model["terms"][3])


class ContractModelIntegrationTest(unittest.TestCase):
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
