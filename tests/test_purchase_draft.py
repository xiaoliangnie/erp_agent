# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from datetime import datetime

from backend.purchase_draft.service import (
    PurchaseDraftError,
    apply_draft_edits,
    create_purchase_draft,
    draft_xlsx_path,
    erp_payload,
    erp_po_datetime,
    load_purchase_draft,
    public_draft,
    tax_rate_choices,
    validate_for_erp,
)
from backend.erp.purchase_create import _po_id_from_result
from backend.purchase_draft.workbook import SHEET1_COLUMNS, write_blank_purchase_template


def _style(style_id, name, order_qty, replenish=None):
    return {
        "styleId": style_id,
        "name": name,
        "orderQty": order_qty,
        "replenishQty": replenish if replenish is not None else order_qty,
    }


class PurchaseDraftTests(unittest.TestCase):
    def test_create_groups_by_supplier_and_writes_xlsx(self):
        snapshot = {
            "styles": [
                _style("SKU-A", "胶带", 20),
                _style("SKU-B", "剪刀", 10),
                _style("SKU-C", "零", 0),
            ],
        }
        hints = {
            "SKU-A": {
                "sku": "SKU-A", "styleId": "SKU-A", "name": "胶带",
                "spec": "透明", "price": 1.2, "supplier": "甲厂", "poId": "600001",
                "remark": "加急到货", "itemRemark": "透明加厚",
            },
            "SKU-B": {
                "sku": "SKU-B", "styleId": "SKU-B", "name": "剪刀",
                "spec": "", "price": None, "supplier": "", "poId": "",
            },
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch("backend.purchase_draft.service.load_style_snapshot", return_value=snapshot), \
                patch("backend.purchase_draft.service._load_last_purchases", return_value=hints), \
                patch("backend.purchase_draft.service.fetch_product_master", return_value={}):
            draft = create_purchase_draft(
                board="baihuo",
                style_ids=["SKU-A", "SKU-B", "SKU-C"],
                env_path="hanli.env",
                root=tmp,
            )
            self.assertEqual(3, draft["stats"]["lines"])
            self.assertEqual(30, draft["stats"]["qty"])
            self.assertEqual(2, draft["stats"]["suppliers"])
            self.assertEqual(2, draft["stats"]["missingSupplier"])
            self.assertEqual(2, draft["stats"]["missingPrice"])
            self.assertFalse(draft["writesErp"])
            self.assertEqual("甲厂", draft["lines"][0]["supplier"])
            self.assertEqual("SKU-A", draft["lines"][0]["sku"])
            self.assertEqual(1.2, draft["lines"][0]["price"])
            self.assertEqual("甲厂", draft["header"]["seller"])
            self.assertEqual("加急到货", draft["header"]["remark"])
            self.assertEqual("透明加厚", draft["lines"][0]["remark"])
            path = draft_xlsx_path(draft["id"], root=tmp)
            self.assertTrue(path.is_file())
            book = load_workbook(path)
            self.assertEqual(SHEET1_COLUMNS, [cell.value for cell in book["Sheet1"][1]])
            self.assertEqual("甲厂", book["Sheet1"]["A2"].value)
            self.assertEqual(20, book["Sheet1"]["F2"].value)
            loaded = load_purchase_draft(draft["id"], root=tmp)
            self.assertEqual(draft["id"], public_draft(loaded)["id"])

    def test_quantity_override_and_reject_empty(self):
        snapshot = {"styles": [_style("XZ01", "鞋", 0)]}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("backend.purchase_draft.service.load_style_snapshot", return_value=snapshot), \
                patch("backend.purchase_draft.service._load_last_purchases", return_value={}), \
                patch("backend.purchase_draft.service.fetch_product_master", return_value={}):
            empty = create_purchase_draft(board="apparel", style_ids=["XZ01"], env_path=None, root=tmp)
            self.assertEqual(0, empty["lines"][0]["qty"])
            with self.assertRaises(PurchaseDraftError):
                validate_for_erp(empty)
            draft = create_purchase_draft(
                board="apparel",
                style_ids=["XZ01"],
                quantities={"XZ01": 40},
                env_path=None,
                root=tmp,
            )
            self.assertEqual(40, draft["lines"][0]["qty"])
            self.assertEqual("XZ01", draft["lines"][0]["sku"])
            self.assertEqual("0", draft["header"]["wmsCoId"])
            with self.assertRaises(PurchaseDraftError):
                validate_for_erp(draft)
            apply_draft_edits(draft, {
                "header": {
                    "seller": "甲厂",
                    "sellerId": "9",
                    "purchaserName": "张三",
                    "paymentMethod": "CurrentSettlement",
                    "wmsCoId": "0",
                },
                "lines": [{**draft["lines"][0], "qty": 12, "price": 8}],
            }, root=tmp)
            saved = load_purchase_draft(draft["id"], root=tmp)
            self.assertEqual("after_arrival", saved["header"]["paymentMethod"])
            items = validate_for_erp(saved)
            self.assertEqual(1, len(items))
            self.assertEqual(12, items[0]["qty"])
            self.assertEqual("CurrentSettlement", erp_payload(saved)["paymentMethod"])

    def test_apparel_lists_all_skus_qty_from_last_po(self):
        snapshot = {"styles": [_style("XZ01", "鞋", 99)]}
        catalog = {
            "XZ01": [
                {"sku": "XZ01-L", "name": "鞋", "spec": "L"},
                {"sku": "XZ01-M", "name": "鞋", "spec": "M"},
                {"sku": "XZ01-S", "name": "鞋", "spec": "S"},
            ],
        }
        hints = {
            "XZ01": {
                "sku": "XZ01-M", "styleId": "XZ01", "name": "鞋",
                "spec": "M", "price": 10, "qty": 8, "supplier": "甲厂",
                "supplierId": "9", "poId": "600010",
            },
            "XZ01-S": {
                "sku": "XZ01-S", "styleId": "XZ01", "name": "鞋",
                "spec": "S", "price": 10, "qty": 5, "supplier": "甲厂",
                "supplierId": "9", "poId": "600009",
            },
            "XZ01-M": {
                "sku": "XZ01-M", "styleId": "XZ01", "name": "鞋",
                "spec": "M", "price": 10, "qty": 8, "supplier": "甲厂",
                "supplierId": "9", "poId": "600010",
            },
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch("backend.purchase_draft.service.load_style_snapshot", return_value=snapshot), \
                patch("backend.purchase_draft.service._load_last_purchases", return_value=hints), \
                patch("backend.purchase_draft.service._load_style_skus", return_value=catalog), \
                patch("backend.purchase_draft.service.fetch_product_master", return_value={}):
            draft = create_purchase_draft(
                board="apparel",
                style_ids=["XZ01"],
                env_path="hanli.env",
                root=tmp,
            )
            by_sku = {line["sku"]: line for line in draft["lines"]}
            self.assertEqual(["XZ01-L", "XZ01-M", "XZ01-S"], sorted(by_sku))
            self.assertEqual(5, by_sku["XZ01-S"]["qty"])
            self.assertEqual(8, by_sku["XZ01-M"]["qty"])
            self.assertEqual(0, by_sku["XZ01-L"]["qty"])
            self.assertEqual(5, by_sku["XZ01-S"]["lastQty"])
            self.assertIsNone(by_sku["XZ01-L"]["lastQty"])
            self.assertNotEqual(99, by_sku["XZ01-L"]["qty"])
            apply_draft_edits(draft, {
                "header": {
                    "seller": "甲厂",
                    "sellerId": "9",
                    "purchaserName": "张三",
                    "paymentMethod": "after_arrival",
                    "wmsCoId": "0",
                },
            }, root=tmp)
            items = validate_for_erp(load_purchase_draft(draft["id"], root=tmp))
            self.assertEqual({"XZ01-S", "XZ01-M"}, {item["sku"] for item in items})
            self.assertEqual(13, sum(item["qty"] for item in items))

    def test_missing_style_and_blank_template(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("backend.purchase_draft.service.load_style_snapshot", return_value={"styles": []}):
            with self.assertRaises(PurchaseDraftError):
                create_purchase_draft(board="apparel", style_ids=["NOPE"], env_path=None, root=tmp)
            path = write_blank_purchase_template(root=tmp)
            self.assertTrue(path.is_file())
            self.assertEqual("采购单模板.xlsx", Path(path).name)

    def test_header_uses_supplier_master_like_contract(self):
        snapshot = {"styles": [_style("SKU-A", "胶带", 8)]}
        hints = {
            "SKU-A": {
                "sku": "SKU-A", "styleId": "SKU-A", "name": "胶带",
                "spec": "", "price": 2.5, "supplier": "佰特", "supplierId": "",
                "poId": "600002", "purchaserName": "李四",
                "paymentMethod": "",
            },
        }
        record = {
            "code": "MRWJ0004",
            "short_name": "佰特",
            "legal_name": "连云港佰特玩具有限公司",
            "frozen": False,
            "invoice_label": "专用发票(13%)",
            "settlement": "银行转账",
            "erp_price_mode": "special_invoice",
            "invoice_rates": {"special_invoice": 13.0},
        }

        class FakeBook:
            def lookup(self, seller):
                return record if seller == "佰特" else None

            def as_dict(self):
                return {"佰特": record}

        with tempfile.TemporaryDirectory() as tmp, \
                patch("backend.purchase_draft.service.load_style_snapshot", return_value=snapshot), \
                patch("backend.purchase_draft.service._load_last_purchases", return_value=hints), \
                patch("backend.purchase_draft.service.fetch_product_master", return_value={}), \
                patch("backend.purchase_draft.service._load_supplier_book", return_value=FakeBook()), \
                patch("backend.purchase_draft.service.last_payment_choice", return_value={"option": "monthly"}):
            draft = create_purchase_draft(
                board="baihuo",
                style_ids=["SKU-A"],
                env_path="hanli.env",
                root=tmp,
            )
            self.assertEqual("佰特", draft["header"]["seller"])
            self.assertEqual("MRWJ0004", draft["header"]["sellerId"])
            self.assertEqual("special_invoice", draft["header"]["invoiceType"])
            self.assertEqual(13.0, draft["header"]["taxRate"])
            self.assertEqual("monthly", draft["header"]["paymentMethod"])
            self.assertTrue(any(item["id"] == "prepay_30_70" and item["name"] == "3/7" for item in draft["options"]["payments"]))
            self.assertEqual("MonthlyStatement", erp_payload(draft)["paymentMethod"])
            self.assertTrue(any(item["seller"] == "佰特" for item in draft["options"]["suppliers"]))
            self.assertIn("连云港佰特玩具有限公司", draft["supplierNote"])

    def test_purchaser_list_tax_and_erp_datetime(self):
        snapshot = {"styles": [_style("SKU-A", "胶带", 8)]}
        hints = {
            "SKU-A": {
                "sku": "SKU-A", "styleId": "SKU-A", "name": "胶带",
                "spec": "", "price": 2.5, "supplier": "甲厂", "supplierId": "9",
                "poId": "600003", "purchaserName": "",
            },
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch("backend.purchase_draft.service.load_style_snapshot", return_value=snapshot), \
                patch("backend.purchase_draft.service._load_last_purchases", return_value=hints), \
                patch("backend.purchase_draft.service.fetch_product_master", return_value={}), \
                patch("backend.purchase_draft.service._load_purchasers", return_value=["李四"]):
            draft = create_purchase_draft(
                board="baihuo",
                style_ids=["SKU-A"],
                env_path=None,
                operator="韩立",
                root=tmp,
            )
            self.assertEqual("韩立", draft["header"]["purchaserName"])
            self.assertEqual(["韩立", "李四"], [item["id"] for item in draft["options"]["purchasers"]])
            self.assertEqual(13, draft["header"]["taxRate"])
            self.assertEqual(10, len(draft["header"]["poDate"]))
            self.assertIn(0, draft["options"]["taxRates"])
            self.assertEqual(0, draft["options"]["invoiceRates"]["no_invoice"])
            apply_draft_edits(draft, {
                "header": {
                    **draft["header"],
                    "invoiceType": "no_invoice",
                    "taxRate": 0,
                    "purchaserName": "李四",
                    "paymentMethod": "CurrentSettlement",
                },
            }, root=tmp)
            loaded = load_purchase_draft(draft["id"], root=tmp)
            self.assertEqual(0, loaded["header"]["taxRate"])
            payload = erp_payload(loaded)
            self.assertRegex(payload["poDate"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            self.assertTrue(payload["poDate"].startswith(loaded["header"]["poDate"]))
            self.assertEqual(0, payload["taxRate"])
            self.assertEqual("李四", payload["purchaserName"])

    def test_erp_po_datetime_keeps_picked_day(self):
        stamp = datetime(2026, 8, 21, 17, 9, 8)
        self.assertEqual("2026-08-20 17:09:08", erp_po_datetime("2026-08-20", now=stamp))
        self.assertEqual("2026-08-21 17:09:08", erp_po_datetime("", now=stamp))
        self.assertEqual([0, 1, 3, 6, 9, 13], tax_rate_choices(13, None, 13))

    def test_po_id_from_erp_result(self):
        self.assertEqual("631200", _po_id_from_result({"po_id": 631200}))
        self.assertEqual("631201", _po_id_from_result({"data": {"poId": "631201"}}))
        self.assertEqual("", _po_id_from_result({"msg": "ok"}))


if __name__ == "__main__":
    unittest.main()
