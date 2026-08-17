# -*- coding: utf-8 -*-
"""采购合同模型与票种规则测试。

离线用例走 tests/fixtures/contract_order_604264.json，不连库。
对真实采购单 604264 的 live 断言保留，设置 CONTRACT_LIVE_TESTS=1 才跑。
"""
import json
import os
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from backend.contracts import (
    DEFAULT_RECEIVING_INFO,
    build_contract_model,
    compose_inspection_standards,
    format_payment_block,
    invoice_term,
    parse_quantity,
    parse_remark_unit_price,
    resolve_payment_terms,
    resolve_preview_binary,
)
from backend.database import clean_master_text, fetch_contract_order, load_contract_order_fixture
from backend.supplier_master import load_supplier_book


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "contract_order_604264.json"
JSON_SUPPLIERS = Path(__file__).resolve().parents[1] / "files" / "config" / "suppliers.json"
LIVE = os.environ.get("CONTRACT_LIVE_TESTS", "").strip().lower() in ("1", "true", "yes", "on")
SUPPLIER_BOOK = load_supplier_book(JSON_SUPPLIERS)


def _offline_model(po_id, invoice_type, **kwargs):
    fetched = load_contract_order_fixture(FIXTURE, po_id)
    kwargs.setdefault("product_master", {})
    return build_contract_model(
        po_id, invoice_type, fetched=fetched, saved_gb=({}, {}),
        gb_lookup=lambda _no: None, supplier_book=SUPPLIER_BOOK, **kwargs,
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
        self.assertEqual(DEFAULT_RECEIVING_INFO, model["receivingInfo"])
        self.assertEqual(DEFAULT_RECEIVING_INFO, model["deliveryAddress"])
        self.assertIn("1.到仓产品及包装无破损", model["inspectionStandards"])

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
            supplier_book=SUPPLIER_BOOK, product_master={},
        )
        self.assertEqual(Decimal("100.6"), model["items"][0]["quantity"])

    def test_payment_option_and_manual_text(self):
        supplier = {"settlement": "银行转账"}
        self.assertEqual(
            "按供方月度到仓合格货物数量，支付货款",
            resolve_payment_terms({"payment_method": ""}, supplier, payment_option="monthly"),
        )
        # ERP 月结预选到月度结算，现结预选到到仓后付款。
        self.assertIn("月度", resolve_payment_terms({"payment_method": "MonthlyStatement"}, supplier))
        self.assertIn("发送到仓", resolve_payment_terms({"payment_method": "CurrentSettlement"}, supplier))
        self.assertEqual(
            "验收后 15 天付清",
            resolve_payment_terms({"payment_method": ""}, supplier, payment_text=" 验收后 15 天付清 "),
        )
        with self.assertRaisesRegex(ValueError, "payment_options"):
            resolve_payment_terms({"payment_method": ""}, supplier, payment_option="unknown")
        with self.assertRaisesRegex(ValueError, "请先选择付款方式"):
            resolve_payment_terms({"payment_method": ""}, supplier)
        with self.assertRaisesRegex(ValueError, "不能超过"):
            resolve_payment_terms({"payment_method": ""}, supplier, payment_text="长" * 501)

    def test_payment_history_is_recorded_and_reused(self):
        from backend.contract_history import last_payment_choice, record_payment_choice
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "payment_history.json"
            self.assertEqual({}, last_payment_choice("佰特", path))
            record_payment_choice(
                "佰特", option="monthly", text="按供方月度到仓合格货物数量，支付货款",
                po_id="604264", path=path,
            )
            saved = last_payment_choice("佰特", path)
            self.assertEqual("monthly", saved["option"])
            self.assertEqual("604264", saved["poId"])
            record_payment_choice("佰特", option="prepay_30_70", text="改成三七", po_id="604300", path=path)
            self.assertEqual("prepay_30_70", last_payment_choice("佰特", path)["option"])
            # 空供应商或空条款不留痕，避免把占位值当成历史。
            record_payment_choice("", option="monthly", text="x", po_id="1", path=path)
            record_payment_choice("佳裕", option="monthly", text="", po_id="1", path=path)
            self.assertEqual({"佰特"}, set(json.loads(path.read_text(encoding="utf-8"))))

    def test_option_label_is_not_written_into_contract(self):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        order = deepcopy(order)
        order["payment_method"] = ""
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
            supplier_book=SUPPLIER_BOOK, payment_option="prepay_30_70",
            product_master={},
        )
        self.assertIn("合同签订后支付30%预付款", model["paymentTerms"])
        self.assertNotIn("3/7", model["paymentTerms"])
        self.assertIn("采购单号604264", model["paymentTerms"])

    def test_internal_supplier_skips_payment_fields(self):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        order = deepcopy(order)
        order["seller"] = "蜀黍家毛绒组装加工"
        order["payment_method"] = ""
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
            supplier_book=SUPPLIER_BOOK, product_master={},
        )
        self.assertTrue(model["supplier"]["internal"])
        self.assertEqual("蜀黍家毛绒组装加工", model["supplier"]["legalName"])
        self.assertEqual("内部往来", model["supplier"]["contact"])
        self.assertIn("不列收付款信息", model["paymentTerms"])
        self.assertEqual(21.7, model["items"][0]["unitPrice"])

    def test_frozen_supplier_is_rejected(self):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        frozen = dict(SUPPLIER_BOOK.lookup("佰特"))
        frozen["frozen"] = True
        with self.assertRaisesRegex(ValueError, "已冻结"):
            build_contract_model(
                "604264", "special_invoice", fetched=(order, items),
                saved_gb=({}, {}), gb_lookup=lambda _no: None,
                suppliers={"佰特": frozen}, product_master={},
            )

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
                supplier_book=SUPPLIER_BOOK, product_master={},
            )


class PreviewBinaryTest(unittest.TestCase):
    def test_missing_configured_path_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / "soffice.exe"
            real.write_text("", encoding="utf-8")
            with patch("backend.contracts.contract_setting", return_value="/Users/yyyy/missing/soffice"):
                with patch("backend.contracts.shutil.which", return_value=str(real)):
                    self.assertEqual(str(real), resolve_preview_binary("CONTRACT_SOFFICE", "soffice"))

    def test_windows_libreoffice_is_discovered(self):
        expected = Path(r"C:\Program Files\LibreOffice\program\soffice.com")
        if not expected.is_file():
            self.skipTest("本机未安装 LibreOffice")
        with patch("backend.contracts.contract_setting", return_value="/Users/yyyy/missing/soffice"):
            with patch("backend.contracts.shutil.which", return_value=None):
                self.assertEqual(str(expected), resolve_preview_binary("CONTRACT_SOFFICE"))


class ProductMasterFallbackTest(unittest.TestCase):
    def _foreign_sku(self, sku="BH26500206-00101"):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        items = deepcopy(items)
        items[0]["sku_id"] = sku
        items[0]["i_id"] = sku.rsplit("-", 1)[0]
        items[0]["name"] = "战术单肩斜挎包"
        return order, items

    def test_clean_master_text_drops_json_null_literals(self):
        self.assertEqual("", clean_master_text(None))
        self.assertEqual("", clean_master_text("null"))
        self.assertEqual("", clean_master_text(" NULL "))
        self.assertEqual("个", clean_master_text("个"))
        self.assertEqual("6978340007468", clean_master_text("6978340007468"))

    def test_unit_and_barcode_fall_back_to_product_master(self):
        order, items = self._foreign_sku()
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
            supplier_book=SUPPLIER_BOOK,
            product_master={
                "BH26500206-00101": {
                    "name": "战术单肩斜挎包",
                    "unit": "个",
                    "category": "箱包（02）",
                    "national_code": "6978340007468",
                    "virtual_category": "斜挎包（0206）",
                },
            },
        )
        item = model["items"][0]
        self.assertEqual("个", item["unit"])
        self.assertEqual("箱包（02）", item["category"])
        self.assertEqual("6978340007468", item["nationalCode"])
        self.assertEqual("斜挎包（0206）", item["virtualCategory"])
        self.assertEqual(21.7, item["unitPrice"])

    def test_erp_price_allowed_when_only_master_exists(self):
        order, items = self._foreign_sku()
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
            supplier_book=SUPPLIER_BOOK,
            product_master={"BH26500206-00101": {"unit": "个", "name": "斜挎包"}},
        )
        self.assertEqual(21.7, model["items"][0]["unitPrice"])

    def test_missing_unit_still_fails_when_master_has_no_unit(self):
        order, items = self._foreign_sku()
        with self.assertRaisesRegex(ValueError, "单位为空"):
            build_contract_model(
                "604264", "special_invoice", fetched=(order, items),
                saved_gb=({}, {}), gb_lookup=lambda _no: None,
                supplier_book=SUPPLIER_BOOK,
                product_master={"BH26500206-00101": {"name": "斜挎包", "unit": ""}},
            )

    def test_remark_first_number_becomes_unit_price(self):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        items = deepcopy(items)
        items[0]["remark"] = "包体32+2个魔术贴标3.45"
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
            supplier_book=SUPPLIER_BOOK, product_master={},
        )
        self.assertEqual(32.0, model["items"][0]["unitPrice"])
        self.assertEqual("包体32+2个魔术贴标3.45", model["items"][0]["remark"])

    def test_price_override_beats_remark_number(self):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        items = deepcopy(items)
        items[0]["remark"] = "包体32+2个魔术贴标3.45"
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
            supplier_book=SUPPLIER_BOOK, product_master={},
            price_overrides={"BH25701004-02202": 40},
        )
        self.assertEqual(40.0, model["items"][0]["unitPrice"])

    def test_receiving_and_inspection_overrides(self):
        model = _offline_model(
            "604264", "special_invoice",
            receiving_info="手工仓：测试路1号",
            inspection_extra="外箱完好\n3.已编号",
        )
        self.assertEqual("手工仓：测试路1号", model["receivingInfo"])
        self.assertEqual("手工仓：测试路1号", model["deliveryAddress"])
        self.assertIn("1.到仓产品及包装无破损", model["inspectionStandards"])
        self.assertIn("3.外箱完好", model["inspectionStandards"])
        self.assertIn("3.已编号", model["inspectionStandards"])

    def test_payment_block_appends_bank_fields(self):
        order, items = load_contract_order_fixture(FIXTURE, "604264")
        supplier = dict(SUPPLIER_BOOK.lookup("佰特"))
        supplier.update({
            "bank_account_name": "连云港佰特玩具有限公司",
            "bank_name": "中国银行扬州分行",
            "bank_account": "1234567890",
        })
        model = build_contract_model(
            "604264", "special_invoice", fetched=(order, items),
            saved_gb=({}, {}), gb_lookup=lambda _no: None,
            suppliers={"佰特": supplier}, product_master={},
        )
        self.assertIn("付款账户名：连云港佰特玩具有限公司", model["paymentTerms"])
        self.assertIn("开户行：中国银行扬州分行", model["paymentTerms"])
        self.assertIn("账户：1234567890", model["paymentTerms"])

    def test_internal_payment_omits_bank_fields(self):
        block = format_payment_block(
            "内部往来，不列收付款信息", "604264",
            {"internal": True, "bank_account_name": "不该出现", "bank_name": "x", "bank_account": "1"},
        )
        self.assertNotIn("付款账户名", block)
        self.assertIn("采购单号604264", block)

    def test_remark_and_inspection_helpers(self):
        self.assertEqual(32.0, parse_remark_unit_price("包体32+2个魔术贴标3.45"))
        self.assertEqual(12.5, parse_remark_unit_price("  12.5元"))
        self.assertIsNone(parse_remark_unit_price("无数字"))
        self.assertIsNone(parse_remark_unit_price("包体0+标"))
        default = "1.到仓产品及包装无破损\n2.入仓数量与下单数量一致"
        self.assertEqual(default, compose_inspection_standards(default, ""))
        self.assertEqual(
            default + "\n3.外箱完好",
            compose_inspection_standards(default, "外箱完好"),
        )

    def test_unknown_sku_without_master_fails_on_unit(self):
        order, items = self._foreign_sku()
        with self.assertRaisesRegex(ValueError, "realtime_products"):
            build_contract_model(
                "604264", "special_invoice", fetched=(order, items),
                saved_gb=({}, {}), gb_lookup=lambda _no: None,
                supplier_book=SUPPLIER_BOOK, product_master={},
                price_overrides={"BH26500206-00101": 10},
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
