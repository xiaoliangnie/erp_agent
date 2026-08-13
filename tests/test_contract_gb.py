# -*- coding: utf-8 -*-
"""合同执行标准：排序、覆盖解析、Excel 列布局。离线，不连库。"""
import unittest
from pathlib import Path

from backend.contracts import (
    normalize_gb_overrides,
    pick_saved_standard,
    resolve_line_gb,
)
from backend.gb_standards import category_tokens, rank_standards


ROOT = Path(__file__).resolve().parents[1]


class RankStandardsTests(unittest.TestCase):
    def test_strips_category_ops_codes(self):
        self.assertEqual(["毛绒"], category_tokens("毛绒（04）", ""))
        self.assertIn("衬衫", category_tokens("衬衫", "男装"))

    def test_current_product_standard_with_name_token_ranks_first(self):
        rows = [
            {
                "standard_no": "GB/T 9832-2026", "name_cn": "毛绒、布制玩具",
                "status": "即将实施", "std_type": "产品", "issue_date": "2026-05-25",
            },
            {
                "standard_no": "GB/T 9832-2007", "name_cn": "毛绒、布制玩具",
                "status": "现行", "std_type": "产品", "issue_date": "2007-06-14",
            },
            {
                "standard_no": "GB 6675.1-2014", "name_cn": "玩具安全 第1部分：基本规范",
                "status": "现行", "std_type": "产品", "issue_date": "2014-05-06",
            },
            {
                "standard_no": "FZ/T 01057.1-2007", "name_cn": "纺织纤维鉴别试验方法",
                "status": "现行", "std_type": "方法", "issue_date": "2007-11-01",
            },
        ]
        ranked = rank_standards(rows, product_name="小熊", category="毛绒（04）")
        self.assertEqual("GB/T 9832-2007", ranked[0]["standard_no"])
        self.assertEqual("GB/T 9832-2026", ranked[1]["standard_no"])
        self.assertNotEqual("FZ/T 01057.1-2007", ranked[0]["standard_no"])


class GbOverrideTests(unittest.TestCase):
    def lookup(self, standard_no):
        catalog = {
            "GB/T 9832-2007": {
                "samr_id": "old", "standard_no": "GB/T 9832-2007",
                "name_cn": "毛绒、布制玩具", "status": "现行",
            },
            "GB/T 9832-2026": {
                "samr_id": "new", "standard_no": "GB/T 9832-2026",
                "name_cn": "毛绒、布制玩具", "status": "即将实施",
            },
            "GB/T 1-1993": {
                "samr_id": "dead", "standard_no": "GB/T 1-1993",
                "name_cn": "已废止示例", "status": "废止",
            },
        }
        return catalog.get(standard_no)

    def test_normalize_rejects_non_object(self):
        with self.assertRaises(ValueError):
            normalize_gb_overrides(["GB/T 9832-2026"])

    def test_line_save_beats_sku_save(self):
        picked = pick_saved_standard(
            "101", "SKU-A",
            {"101": {"standard_no": "GB/T 9832-2026"}},
            {"SKU-A": {"standard_no": "GB/T 9832-2007"}},
        )
        self.assertEqual("GB/T 9832-2026", picked["standard_no"])

    def test_sku_save_used_when_line_has_none(self):
        picked = pick_saved_standard(
            "101", "SKU-A", {},
            {"SKU-A": {"standard_no": "GB/T 9832-2007"}},
        )
        self.assertEqual("GB/T 9832-2007", picked["standard_no"])

    def test_poi_override_wins_and_does_not_auto_pick(self):
        gb = resolve_line_gb(
            poi_id="101", sku="SKU-A",
            gb_overrides={"101": "GB/T 9832-2026"},
            line_saves={"101": {"standard_no": "GB/T 9832-2007"}},
            sku_saves={},
            lookup=self.lookup,
        )
        self.assertEqual("GB/T 9832-2026", gb["standard_no"])
        self.assertEqual("new", gb["samr_id"])

    def test_empty_override_clears_saved_value(self):
        gb = resolve_line_gb(
            poi_id="101", sku="SKU-A",
            gb_overrides={"101": ""},
            line_saves={"101": {"standard_no": "GB/T 9832-2007"}},
            sku_saves={},
            lookup=self.lookup,
        )
        self.assertEqual("", gb["standard_no"])

    def test_no_override_and_no_save_stays_empty(self):
        gb = resolve_line_gb(
            poi_id="101", sku="SKU-A",
            gb_overrides={}, line_saves={}, sku_saves={},
            lookup=self.lookup,
        )
        self.assertEqual("", gb["standard_no"])

    def test_unknown_standard_fails_closed(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_line_gb(
                poi_id="101", sku="SKU-A",
                gb_overrides={"101": "GB/T 9999-9999"},
                line_saves={}, sku_saves={},
                lookup=self.lookup,
            )
        self.assertIn("不在国标目录中", str(ctx.exception))

    def test_retired_standard_fails_closed(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_line_gb(
                poi_id="101", sku="SKU-A",
                gb_overrides={"101": "GB/T 1-1993"},
                line_saves={}, sku_saves={},
                lookup=self.lookup,
            )
        self.assertIn("废止", str(ctx.exception))

    def test_sku_keyed_override_is_accepted(self):
        gb = resolve_line_gb(
            poi_id="101", sku="SKU-A",
            gb_overrides={"SKU-A": "GB/T 9832-2007"},
            line_saves={}, sku_saves={},
            lookup=self.lookup,
        )
        self.assertEqual("GB/T 9832-2007", gb["standard_no"])


class ExcelLayoutTests(unittest.TestCase):
    def test_execution_standard_column_sits_after_barcode(self):
        text = (ROOT / "scripts" / "generate_contract.mjs").read_text(encoding="utf-8")
        self.assertIn('"国标码"', text)
        self.assertIn('"执行标准"', text)
        self.assertIn("=N${itemStart}*L${itemStart}", text)
        self.assertIn("=SUM(O${itemStart}:O${itemEnd})", text)
        self.assertIn("A1:P2", text)
        self.assertNotIn("=M${itemStart}*K${itemStart}", text)
        self.assertLess(text.index('"国标码"'), text.index('"执行标准"'))


if __name__ == "__main__":
    unittest.main()
