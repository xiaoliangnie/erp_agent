import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.gb_standards import (
    GbSyncError,
    SamrCatalogClient,
    build_queries,
    classify_changes,
    collect_status_transitions,
    compact_payload,
    compact_standard_no,
    family_ids_for,
    gb_change_markdown,
    load_category_map,
    lookup_product_standards,
    looks_like_standard_no,
    mark_recommended_options,
    match_categories,
    match_contract_gb_alerts,
    match_families,
    normalize_standard,
    noteworthy_transition,
    notify_contract_gb_changes,
    parse_csv_list,
    parse_recommended_nos,
    parse_search_response,
    query_is_standard_prefix,
    resolve_product_scope,
    search_params,
    sort_standard_hits,
    standard_starts_with,
)


FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "gb_advanced_search.json").read_text(encoding="utf-8")
)


class GbStandardsParsingTests(unittest.TestCase):
    def test_parses_advanced_search_json(self):
        total, page, rows = parse_search_response(FIXTURE)
        self.assertEqual(2, total)
        self.assertEqual(1, page)
        self.assertEqual(2, len(rows))

    def test_rejects_non_object_payload(self):
        with self.assertRaises(GbSyncError):
            parse_search_response([{"id": "x"}])

    def test_normalizes_catalog_row_and_strips_html(self):
        current, old = FIXTURE["rows"]
        fresh = normalize_standard(current, "2026-08-13 10:00:00")
        retired = normalize_standard(old, "2026-08-13 10:00:00")
        self.assertEqual("52AF72FB01A5403AE06397BE0A0AD6A8", fresh["samr_id"])
        self.assertEqual("GB/T 9832-2026", fresh["standard_no"])
        self.assertEqual("即将实施", fresh["status"])
        self.assertEqual("推荐性", fresh["nature"])
        self.assertEqual("Y57", fresh["ccs_norm"])
        self.assertEqual("2026-05-25", fresh["issue_date"])
        self.assertEqual("GB/T 9832-2007", fresh["replaced_standards"])
        self.assertIn("gbDetailed?id=", fresh["detail_url"])
        self.assertNotIn("DRAFT_STAFF", fresh["source_payload"])
        self.assertEqual("毛绒、布制玩具", retired["name_cn"])
        self.assertEqual("Y57", retired["ccs_norm"])
        self.assertEqual("", retired["replaced_standards"])

    def test_skips_rows_without_id_or_standard_no(self):
        self.assertIsNone(normalize_standard({"C_STD_CODE": "GB/T 1"}, "2026-08-13 10:00:00"))
        self.assertIsNone(normalize_standard({"id": "abc"}, "2026-08-13 10:00:00"))

    def test_compact_payload_drops_drafter_names(self):
        payload = compact_payload(FIXTURE["rows"][0])
        self.assertIn("C_STD_CODE", payload)
        self.assertNotIn("DRAFT_STAFF", payload)

    def test_content_hash_changes_when_status_changes(self):
        row = dict(FIXTURE["rows"][0])
        first = normalize_standard(row, "2026-08-13 10:00:00")
        row["STATE"] = "现行"
        second = normalize_standard(row, "2026-08-13 11:00:00")
        self.assertNotEqual(first["content_hash"], second["content_hash"])
        stats = classify_changes({first["samr_id"]: first["content_hash"]}, [second])
        self.assertEqual({"inserted": 0, "updated": 1, "unchanged": 0}, stats)

    def test_classifies_insert_and_unchanged(self):
        record = normalize_standard(FIXTURE["rows"][0], "2026-08-13 10:00:00")
        self.assertEqual(
            {"inserted": 1, "updated": 0, "unchanged": 0},
            classify_changes({}, [record]),
        )
        self.assertEqual(
            {"inserted": 0, "updated": 0, "unchanged": 1},
            classify_changes({record["samr_id"]: record["content_hash"]}, [record]),
        )

    def test_build_queries_filtered_union_and_all(self):
        queries = build_queries(scope="filtered", ccs="Y57", ics="97.200.50", keywords="玩具")
        labels = [item["label"] for item in queries]
        self.assertEqual(["CCS Y57", "ICS 97.200.50", "名称 玩具"], labels)
        all_queries = build_queries(scope="all", ccs="Y57")
        self.assertEqual(1, len(all_queries))
        self.assertEqual("全部国家标准", all_queries[0]["label"])

    def test_filtered_scope_requires_at_least_one_condition(self):
        with self.assertRaises(GbSyncError):
            build_queries(scope="filtered", ccs="", ics="", keywords="")

    def test_parse_csv_list_accepts_chinese_separators(self):
        self.assertEqual(["Y57", "W21"], parse_csv_list("Y57，W21、"))

    def test_search_params_map_to_samr_fields(self):
        params = search_params(
            {"keyword": "毛绒", "ccs": "Y57", "ics": "97.200.50", "status": "现行"},
            page=2, page_size=50,
        )
        self.assertEqual("std-param", params["typeRadio"])
        self.assertEqual("毛绒", params["std_p8"])
        self.assertEqual("现行", params["std_p6_1"])
        self.assertEqual("97.200.50", params["std_p14"])
        self.assertEqual("Y57", params["std_p15"])
        self.assertEqual("2", params["pageNumber"])
        self.assertEqual("50", params["pageSize"])


class SamrCatalogClientTests(unittest.TestCase):
    def test_paginates_until_total(self):
        pages = {
            1: {"total": 3, "pageNumber": 1, "rows": [{"id": "a"}, {"id": "b"}]},
            2: {"total": 3, "pageNumber": 2, "rows": [{"id": "c"}]},
        }

        def fetch(url: str):
            query = parse_qs(urlparse(url).query)
            page = int(query["pageNumber"][0])
            self.assertEqual("https://std.samr.gov.cn/gb/search/gbAdvancedSearchPage", url.split("?", 1)[0])
            return pages[page]

        client = SamrCatalogClient(page_size=2, request_interval=0, fetch_json=fetch)
        rows = list(client.iter_rows({"label": "CCS Y57", "ccs": "Y57"}))
        self.assertEqual(["a", "b", "c"], [row["id"] for row in rows])

    def test_max_pages_stops_early(self):
        def fetch(url: str):
            return {"total": 100, "pageNumber": 1, "rows": [{"id": "a"}, {"id": "b"}]}

        client = SamrCatalogClient(page_size=2, request_interval=0, fetch_json=fetch)
        rows = list(client.iter_rows({}, max_pages=1))
        self.assertEqual(2, len(rows))


class ProductScopeTests(unittest.TestCase):
    def setUp(self):
        self.mapping = {
            "ignore": ["", "其他"],
            "families": {
                "服装": {"label": "服装", "ccs": ["Y76"], "ics": ["61.020"], "keywords": []},
                "玩具": {"label": "玩具", "ccs": ["Y57"], "ics": ["97.200.50"], "keywords": []},
                "杯壶": {"label": "杯壶", "ccs": ["Y73"], "ics": [], "keywords": ["真空杯"]},
            },
            "categories": {
                "衬衫": "服装",
                "毛绒（04）": "玩具",
                "杯壶（03）": "杯壶",
                "杂货": "杯壶",
                "纺织": ["玩具", "杯壶"],
            },
        }

    def test_expands_only_categories_present_in_product_table(self):
        scope = resolve_product_scope(
            [
                {"category": "衬衫", "sku_count": 10},
                {"category": "毛绒（04）", "sku_count": 5},
                {"category": "", "sku_count": 2},
                {"category": "尚未维护的类", "sku_count": 1},
            ],
            self.mapping,
        )
        labels = [item["label"] for item in scope["queries"]]
        self.assertIn("CCS Y76 · 服装", labels)
        self.assertIn("CCS Y57 · 玩具", labels)
        self.assertNotIn("CCS Y73 · 杯壶", labels)
        self.assertEqual(["尚未维护的类"], scope["unmapped"])
        self.assertEqual([""], scope["ignored"])
        self.assertEqual(["衬衫"], scope["families"]["服装"])

    def test_merges_shared_catalog_query_across_families(self):
        scope = resolve_product_scope(["杯壶（03）", "纺织"], self.mapping)
        vacuum = [item for item in scope["queries"] if item.get("keyword") == "真空杯"][0]
        y73 = [item for item in scope["queries"] if item.get("ccs") == "Y73"][0]
        self.assertEqual(["杯壶"], vacuum["family_ids"])
        self.assertEqual(["杯壶"], y73["family_ids"])
        toy_ics = [item for item in scope["queries"] if item.get("ics") == "97.200.50"][0]
        self.assertEqual(["玩具"], toy_ics["family_ids"])

    def test_rejects_unknown_family_id(self):
        mapping = dict(self.mapping)
        mapping["categories"] = {"衬衫": "不存在的族"}
        with self.assertRaises(GbSyncError):
            resolve_product_scope(["衬衫"], mapping)

    def test_real_map_covers_contract_categories(self):
        mapping = load_category_map()
        self.assertEqual(["玩具"], family_ids_for("毛绒（04）", mapping))
        self.assertEqual(["服装"], family_ids_for("衬衫", mapping))
        self.assertEqual(["鞋类"], family_ids_for("作训鞋", mapping))
        self.assertIn("Y57", mapping["families"]["玩具"]["ccs"])
        self.assertIn("Y76", mapping["families"]["服装"]["ccs"])
        unknown = [
            family_id
            for family_ids in mapping["categories"].values()
            for family_id in ([family_ids] if isinstance(family_ids, str) else family_ids)
            if family_id not in mapping["families"]
        ]
        self.assertEqual([], unknown)


class GbLookupTests(unittest.TestCase):
    def test_detects_standard_numbers(self):
        self.assertTrue(looks_like_standard_no("GB/T 9832-2026"))
        self.assertTrue(looks_like_standard_no("GB 6675.1-2014"))
        self.assertTrue(looks_like_standard_no("gbt9832"))
        self.assertFalse(looks_like_standard_no("毛绒小熊"))
        self.assertEqual("GBT98322026", compact_standard_no("GB/T 9832-2026"))

    def test_matches_category_and_family_from_spoken_words(self):
        mapping = load_category_map()
        self.assertIn("毛绒（04）", match_categories("毛绒", mapping))
        self.assertIn("衬衫", match_categories("衬衫", mapping))
        self.assertIn("玩具", match_families("玩具", mapping))
        self.assertIn("玩具", match_families("毛绒", mapping))

    def test_unmapped_category_does_not_need_database(self):
        result = lookup_product_standards("unused.env", category="线下订单")
        self.assertFalse(result["mapped"])
        self.assertEqual([], result["standards"])
        self.assertIn("尚未映射", result["note"])

    def test_empty_lookup_stays_empty_without_database(self):
        result = lookup_product_standards("unused.env")
        self.assertEqual("empty", result["mode"])
        self.assertEqual([], result["standards"])
        self.assertIn("请提供", result["note"])


class GbSchedulerBackoffTests(unittest.TestCase):
    def test_failure_wait_doubles_and_caps(self):
        from backend.gb_standards import GbStandardsScheduler
        scheduler = GbStandardsScheduler(None, enabled=False, poll_seconds=30)
        self.assertEqual(30, scheduler._wait_after_failures(0))
        self.assertEqual(60, scheduler._wait_after_failures(1))
        self.assertEqual(120, scheduler._wait_after_failures(2))
        self.assertEqual(240, scheduler._wait_after_failures(3))
        self.assertEqual(480, scheduler._wait_after_failures(4))
        self.assertEqual(480, scheduler._wait_after_failures(8))
        scheduler.poll_seconds = 60
        self.assertEqual(900, scheduler._wait_after_failures(4))

    def test_loop_backs_off_after_sync_errors(self):
        from backend.gb_standards import GbStandardsScheduler

        class Boom:
            env_path = "/no/such/hanli.env"

            def sync(self):
                raise RuntimeError("samr down")

        scheduler = GbStandardsScheduler(
            Boom(), enabled=True, send_time="00:00",
            poll_seconds=30, initial_delay_seconds=0,
        )
        waits = []

        class FakeStop:
            def is_set(self):
                return len(waits) >= 4

            def set(self):
                return None

            def wait(self, timeout=None):
                waits.append(timeout)
                return len(waits) >= 4

        scheduler._stop = FakeStop()
        scheduler._loop()
        self.assertEqual([30, 60, 120, 240], waits)
        self.assertIn("samr down", scheduler.last_error)


class GbChangeAlertTests(unittest.TestCase):
    def test_noteworthy_transitions(self):
        self.assertEqual("即将实施 → 现行", noteworthy_transition("即将实施", "现行"))
        self.assertEqual("现行 → 废止", noteworthy_transition("现行", "废止"))
        self.assertEqual("即将实施 → 废止", noteworthy_transition("即将实施", "废止"))
        self.assertEqual("", noteworthy_transition("现行", "现行"))
        self.assertEqual("", noteworthy_transition("即将实施", "即将实施"))
        self.assertEqual("", noteworthy_transition("现行", "即将实施"))
        self.assertEqual("", noteworthy_transition("", "现行"))

    def test_name_only_change_is_not_a_transition(self):
        existing = {
            "a": {"content_hash": "1", "status": "即将实施", "standard_no": "GB/T 1", "name_cn": "旧名"},
        }
        records = [{
            "samr_id": "a", "content_hash": "2", "status": "即将实施",
            "standard_no": "GB/T 1", "name_cn": "新名",
        }]
        self.assertEqual([], collect_status_transitions(existing, records))

    def test_insert_is_not_a_transition(self):
        records = [{"samr_id": "new", "status": "废止", "standard_no": "X", "name_cn": "n"}]
        self.assertEqual([], collect_status_transitions({}, records))

    def test_only_contract_used_standards_alert(self):
        changes = collect_status_transitions(
            {
                "a": {"status": "即将实施", "standard_no": "GB/T 9832-2026", "name_cn": "毛绒玩具"},
                "b": {"status": "现行", "standard_no": "GB/T 1-1993", "name_cn": "已废止示例"},
            },
            [
                {"samr_id": "a", "status": "现行", "standard_no": "GB/T 9832-2026", "name_cn": "毛绒玩具"},
                {"samr_id": "b", "status": "废止", "standard_no": "GB/T 1-1993", "name_cn": "已废止示例"},
            ],
        )
        self.assertEqual(["a", "b"], [item["samrId"] for item in changes])
        alerts = match_contract_gb_alerts(changes, [
            {"po_id": "604264", "poi_id": "1", "sku_id": "SKU-A",
             "samr_id": "a", "standard_no": "GB/T 9832-2026"},
        ])
        self.assertEqual(["a"], [item["samrId"] for item in alerts])
        text = gb_change_markdown(alerts, today="2026-08-13")
        self.assertIn("604264", text)
        self.assertIn("即将实施 → 现行", text)
        self.assertNotIn("GB/T 1-1993", text)

    def test_notify_is_idempotent_per_standard_per_day(self):
        import tempfile
        from pathlib import Path
        from backend.agent.audit import AuditLog
        from backend.agent.store import AgentStore

        class FakeSender:
            configured = True

            def __init__(self):
                self.calls = []

            def send_markdown(self, title, text, **kwargs):
                self.calls.append({"title": title, "text": text})
                return {"channel": "fake"}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit = AuditLog(AgentStore(Path(tmp.name) / "agent.sqlite3"))
        sender = FakeSender()
        changes = [{
            "samrId": "a", "standardNo": "GB/T 9832-2026", "nameCn": "毛绒玩具",
            "fromStatus": "即将实施", "toStatus": "现行", "label": "即将实施 → 现行",
        }]
        usages = [{
            "po_id": "604264", "poi_id": "1", "sku_id": "SKU-A",
            "samr_id": "a", "standard_no": "GB/T 9832-2026",
        }]
        first = notify_contract_gb_changes(
            "unused.env", changes, sender=sender, audit=audit, usages=usages, today="2026-08-13",
        )
        second = notify_contract_gb_changes(
            "unused.env", changes, sender=sender, audit=audit, usages=usages, today="2026-08-13",
        )
        self.assertTrue(first.get("sent"))
        self.assertTrue(second.get("skipped"))
        self.assertEqual("同一批国标变更已经提醒过", second["reason"])
        self.assertEqual(1, len(sender.calls))
        self.assertIn("604264", sender.calls[0]["text"])


class ContractGbRecommendTests(unittest.TestCase):
    def test_prefix_matches_standard_number(self):
        self.assertTrue(query_is_standard_prefix("GB/T 36"))
        self.assertTrue(query_is_standard_prefix("9832"))
        self.assertFalse(query_is_standard_prefix("毛绒玩具"))
        self.assertTrue(standard_starts_with("GB/T 369-2008", "GB/T 36"))
        self.assertTrue(standard_starts_with("GB/T 9832-2026", "9832"))
        self.assertFalse(standard_starts_with("GB/T 136-2017", "GB/T 36"))

    def test_prefix_hits_rank_first(self):
        rows = [
            {"standard_no": "GB/T 136-2017", "status": "现行", "std_type": "产品",
             "name_cn": "无关", "issue_date": "2017-01-01"},
            {"standard_no": "GB/T 369-2008", "status": "现行", "std_type": "产品",
             "name_cn": "毛绒玩具", "issue_date": "2008-01-01"},
        ]
        ranked = sort_standard_hits(rows, "GB/T 36", product_name="毛绒熊", category="毛绒")
        self.assertEqual("GB/T 369-2008", ranked[0]["standard_no"])

    def test_model_can_only_pick_allowed_numbers(self):
        allowed = ["GB/T 9832-2026", "GB 6675.1-2014"]
        picked = parse_recommended_nos(
            '先看这些：["GB/T 9832-2026", "GB/T 99999-2099", "GB 6675.1-2014"]',
            allowed,
        )
        self.assertEqual(["GB/T 9832-2026", "GB 6675.1-2014"], picked)
        self.assertEqual([], parse_recommended_nos('["GB/T 1-2009"]', allowed))

    def test_recommend_marks_model_picks_first(self):
        options = [
            {"standardNo": "GB/T 1-2009", "status": "现行", "nameCn": "标准化工作导则"},
            {"standardNo": "GB/T 9832-2026", "status": "即将实施", "nameCn": "毛绒、布制玩具"},
        ]
        marked = mark_recommended_options(options, ["GB/T 9832-2026"])
        self.assertEqual("GB/T 9832-2026", marked[0]["standardNo"])
        self.assertTrue(marked[0]["recommended"])
        self.assertEqual("模型按商品信息推荐", marked[0]["recommendReason"])
        self.assertFalse(marked[1]["recommended"])


if __name__ == "__main__":
    unittest.main()
