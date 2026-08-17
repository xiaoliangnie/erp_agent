# -*- coding: utf-8 -*-
"""本机供应商管理表：解析、匹配、冻结、缺字段。不上库。"""
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from backend.contract_mappings import load_mappings
from backend.supplier_master import (
    clear_supplier_cache,
    dedupe_supplier_records,
    load_supplier_book,
    missing_supplier_fields,
    parse_created_at,
    parse_invoice_type,
    supplier_issue,
)


HEADERS = [
    "编码", "全称", "简称", "主体公司", "分类", "擅长类目大类", "擅长类目", "等级",
    "结算方式", "是否指定", "采购员", "是否冻结", "付款账户名", "开户行", "账户",
    "税号", "发票类型", "地址电话", "联系人", "联系电话", "传真", "公司地址",
    "创建时间",
]
SETTLEMENT_INDEX = HEADERS.index("结算方式")


def _write_book(path: Path, rows: list[list]):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "供应商管理"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def _row(*, code, legal, short, frozen="正常", invoice="", contact="", phone="",
         address="", phone_addr="", settlement="银行转账",
         bank_account_name="", bank_name="", bank_account="", created=""):
    return [
        code, legal, short, None, None, None, None, None,
        settlement, None, None, frozen, bank_account_name, bank_name, bank_account,
        None, invoice, phone_addr, contact, phone, None, address, created,
    ]


class ParseInvoiceTests(unittest.TestCase):
    def test_known_labels(self):
        self.assertEqual(("special_invoice", 13.0), parse_invoice_type("专用发票(13%)"))
        self.assertEqual(("normal_invoice", 0.0), parse_invoice_type("普通发票(0%)"))
        self.assertEqual(("no_invoice", 0.0), parse_invoice_type("不开发票(0%)"))
        self.assertEqual(("special_invoice", 0.0), parse_invoice_type("专业发票(0%)"))
        self.assertEqual(("normal_invoice", 0.0), parse_invoice_type("普票发票(0%)"))

    def test_bare_rate_does_not_guess_kind(self):
        self.assertEqual((None, 0.0), parse_invoice_type("(0%)"))
        self.assertEqual((None, None), parse_invoice_type(""))

    def test_mapping_file_covers_real_labels(self):
        aliases = load_mappings()["invoice_types"]
        for label in ("专用发票", "专业发票", "普通发票", "普票发票", "不开发票"):
            self.assertIn(label, aliases)


class SupplierBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "供应商管理.xlsx"
        _write_book(self.path, [
            _row(
                code="MRWJ0004", legal="连云港佰特玩具有限公司", short="佰特",
                invoice="专用发票(13%)", contact="汪炎", phone="13912133467",
                address="江苏省扬州市邗江区司徒庙路299号",
            ),
            _row(
                code="CP-0184", legal="徐水区林行鞋垫厂", short="&林行",
                invoice="不开发票(0%)", contact="张泽林", phone="17734355606",
                address="河北省保定市徐水区",
            ),
            _row(
                code="SW0105", legal="远东服装A", short="远东",
                invoice="普通发票(0%)", contact="甲", phone="13400000001",
                address="广东佛山",
            ),
            _row(
                code="SW0104", legal="远东服装B", short="&远东",
                invoice="普通发票(0%)", contact="乙", phone="13400000002",
                address="山东青岛",
            ),
            _row(
                code="FZ0001", legal="已冻结厂", short="冻结厂",
                frozen="冻结", invoice="专用发票(13%)",
                contact="丙", phone="13500000003", address="浙江杭州",
            ),
            _row(
                code="QZ0001", legal="缺址厂", short="缺址",
                invoice="(0%)", contact="丁", phone="13600000004",
            ),
            _row(
                code="SW0103", legal="占位厂", short="占位",
                invoice="(0%)", contact="1", phone="1", address="1",
            ),
        ])
        clear_supplier_cache()
        self.book = load_supplier_book(self.path)

    def tearDown(self):
        clear_supplier_cache()
        self.tmp.cleanup()

    def test_exact_short_name(self):
        hit = self.book.lookup("佰特")
        self.assertEqual("连云港佰特玩具有限公司", hit["legal_name"])
        self.assertEqual("银行转账", hit["settlement"])
        self.assertEqual("special_invoice", hit["erp_price_mode"])
        self.assertEqual(13, hit["invoice_rates"]["special_invoice"])
        self.assertIsNone(hit["invoice_rates"]["normal_invoice"])
        self.assertEqual("", hit["bank_account_name"])
        self.assertEqual([], missing_supplier_fields(hit))

    def test_ampersand_alias_when_unique(self):
        self.assertEqual("徐水区林行鞋垫厂", self.book.lookup("林行")["legal_name"])
        self.assertEqual("徐水区林行鞋垫厂", self.book.lookup("&林行")["legal_name"])

    def test_ampersand_collision_stays_exact(self):
        self.assertEqual("远东服装A", self.book.lookup("远东")["legal_name"])
        self.assertEqual("远东服装B", self.book.lookup("&远东")["legal_name"])

    def test_lookup_by_legal_and_code(self):
        self.assertEqual("佰特", self.book.lookup("连云港佰特玩具有限公司")["short_name"])
        self.assertEqual("佰特", self.book.lookup("MRWJ0004")["short_name"])

    def test_frozen_and_missing_fields(self):
        frozen = self.book.lookup("冻结厂")
        self.assertTrue(frozen["frozen"])
        self.assertIn("已冻结", supplier_issue("冻结厂", frozen))
        missing = self.book.lookup("缺址")
        self.assertEqual(["address"], missing_supplier_fields(missing))
        self.assertIn("公司地址", supplier_issue("缺址", missing))
        placeholder = self.book.lookup("占位")
        self.assertEqual(
            ["address", "contact_name", "contact_phone"],
            missing_supplier_fields(placeholder),
        )

    def test_quality_names_include_stripped_alias(self):
        names = self.book.names()
        self.assertIn("佰特", names)
        self.assertIn("林行", names)
        self.assertIn("&林行", names)

    def test_mtime_cache_reloads(self):
        import os
        first = load_supplier_book(self.path)
        self.assertIs(first, load_supplier_book(self.path))
        _write_book(self.path, [
            _row(
                code="X1", legal="新厂", short="新厂",
                invoice="专用发票(13%)", contact="戊", phone="13700000005",
                address="上海",
            ),
        ])
        later = first.path.stat().st_mtime + 10
        os.utime(self.path, (later, later))
        reloaded = load_supplier_book(self.path)
        self.assertIsNone(reloaded.lookup("佰特"))
        self.assertEqual("新厂", reloaded.lookup("新厂")["short_name"])


class InternalSupplierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = self.root / "config"
        config.mkdir()
        _write_book(config / "供应商管理.xlsx", [
            _row(
                code="MRWJ0004", legal="连云港佰特玩具有限公司", short="佰特",
                invoice="专用发票(13%)", contact="汪炎", phone="13912133467",
                address="扬州",
            ),
        ])
        (config / "internal_suppliers.json").write_text(
            '{"蜀黍家毛绒组装加工": {"label": "蜀黍家毛绒组装加工"}, "蜀黍家": {}}',
            encoding="utf-8",
        )
        clear_supplier_cache()
        self.book = load_supplier_book(root=self.root)

    def tearDown(self):
        clear_supplier_cache()
        self.tmp.cleanup()

    def test_internal_is_mapped_without_bank_fields(self):
        hit = self.book.lookup("蜀黍家毛绒组装加工")
        self.assertTrue(hit["internal"])
        self.assertEqual("内部往来", hit["address"])
        self.assertEqual([], missing_supplier_fields(hit))
        self.assertEqual("", supplier_issue("蜀黍家毛绒组装加工", hit))
        self.assertTrue(self.book.lookup("佰特"))
        self.assertFalse(self.book.lookup("佰特").get("internal"))


class SupplierDedupeTests(unittest.TestCase):
    def test_parse_created_at_accepts_common_formats(self):
        from datetime import datetime
        self.assertEqual(datetime(2026, 8, 1, 12, 30, 0), parse_created_at("2026-08-01 12:30:00"))
        self.assertEqual(datetime(2026, 8, 1), parse_created_at("2026-08-01"))
        self.assertIsNone(parse_created_at(""))
        self.assertIsNone(parse_created_at(None))

    def test_newer_created_at_wins(self):
        older = {"short_name": "佰特", "legal_name": "旧厂", "created_at": parse_created_at("2024-01-01")}
        newer = {"short_name": "佰特", "legal_name": "新厂", "created_at": parse_created_at("2026-08-01")}
        kept = dedupe_supplier_records([older, newer])
        self.assertEqual(1, len(kept))
        self.assertEqual("新厂", kept[0]["legal_name"])
        kept_reverse = dedupe_supplier_records([newer, older])
        self.assertEqual("新厂", kept_reverse[0]["legal_name"])

    def test_dated_row_beats_undated_duplicate(self):
        undated = {"short_name": "佰特", "legal_name": "无日期", "created_at": None}
        dated = {"short_name": "佰特", "legal_name": "有日期", "created_at": parse_created_at("2026-01-01")}
        self.assertEqual("有日期", dedupe_supplier_records([undated, dated])[0]["legal_name"])
        self.assertEqual("有日期", dedupe_supplier_records([dated, undated])[0]["legal_name"])

    def test_excel_keeps_latest_duplicate_short_name(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "供应商管理.xlsx"
        _write_book(path, [
            _row(
                code="OLD", legal="旧佰特", short="佰特",
                invoice="专用发票(13%)", contact="甲", phone="13900000001",
                address="扬州旧址", bank_account_name="旧户名",
                bank_name="旧行", bank_account="111", created="2024-01-01 00:00:00",
            ),
            _row(
                code="NEW", legal="新佰特", short="佰特",
                invoice="专用发票(13%)", contact="乙", phone="13900000002",
                address="扬州新址", bank_account_name="新户名",
                bank_name="新行", bank_account="222", created="2026-08-01 10:00:00",
            ),
        ])
        clear_supplier_cache()
        book = load_supplier_book(path)
        hit = book.lookup("佰特")
        self.assertEqual("新佰特", hit["legal_name"])
        self.assertEqual("新户名", hit["bank_account_name"])
        self.assertEqual("新行", hit["bank_name"])
        self.assertEqual("222", hit["bank_account"])
        clear_supplier_cache()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
