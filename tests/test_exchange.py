import tempfile
import unittest
from pathlib import Path

from backend.exchange import ExchangeError, ExchangeService


class ExchangeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ExchangeService(Path(self.tmp.name) / "exchange.sqlite3")
        self.payload = {
            "rules": {
                "strategy": "direct",
                "replacements": [{
                    "from": "OLD-01", "to": "NEW-01",
                    "sourceStyle": "STYLE-01", "targetStyle": "STYLE-01",
                }],
            },
            "targets": {"o_ids": ["10001", "10002"]},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **kwargs):
        return self.service.create_job(self.payload, operator="张三", **kwargs)

    def test_requires_explicit_orders_and_different_skus(self):
        bad = {**self.payload, "targets": {"o_ids": []}}
        with self.assertRaisesRegex(ExchangeError, "必须提供明确"):
            self.service.create_job(bad, operator="张三")
        bad = {
            **self.payload,
            "rules": {"strategy": "direct", "replacements": [{
                "from": "A", "to": "A", "sourceStyle": "STYLE-A", "targetStyle": "STYLE-A",
            }]},
        }
        with self.assertRaisesRegex(ExchangeError, "不能相同"):
            self.service.create_job(bad, operator="张三")

    def test_idempotent_create(self):
        first = self.create(idempotency_key="same-request")
        second = self.create(idempotency_key="same-request")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(self.service.list_jobs()))

    def test_different_orders_can_be_claimed_by_two_workers(self):
        first = self.create()
        second_payload = {
            **self.payload,
            "targets": {"o_ids": ["20001"]},
        }
        second = self.service.create_job(second_payload, operator="张三")

        first_claim = self.service.next_job("erp-one")
        second_claim = self.service.next_job("erp-two")
        self.assertEqual({first["id"], second["id"]}, {first_claim["id"], second_claim["id"]})
        self.assertEqual("plan", first_claim["action"])
        self.assertEqual("plan", second_claim["action"])
        self.assertIsNone(self.service.next_job("erp-three"))

    def test_same_order_cannot_have_overlapping_active_jobs(self):
        first = self.create()
        overlap = {
            **self.payload,
            "targets": {"o_ids": ["10002", "20001"]},
        }
        with self.assertRaisesRegex(ExchangeError, first["id"]):
            self.service.create_job(overlap, operator="李四")

        self.service.cancel(first["id"])
        created = self.service.create_job(overlap, operator="李四")
        self.assertEqual(["10002", "20001"], created["targets"]["o_ids"])

    def test_confirmed_execution_is_claimed_before_new_planning(self):
        ready = self.create()
        self.service.next_job("planner")
        self.service.report_plan(ready["id"], "planner", {
            "plans": [
                {"o_id": "10001", "ok": True, "mode": "ChangeItem", "src_sku_id": "OLD-01", "new_sku_id": "NEW-01"},
                {"o_id": "10002", "ok": False, "reason": "未找到源 SKU"},
            ],
        })
        self.service.confirm(ready["id"], "张三")
        pending = self.service.create_job({**self.payload, "targets": {"o_ids": ["20001"]}}, operator="张三")

        claimed = self.service.next_job("executor")
        self.assertEqual(ready["id"], claimed["id"])
        self.assertEqual("execute", claimed["action"])
        self.assertEqual("pending", self.service.get_job(pending["id"])["status"])

    def test_dry_run_confirm_and_single_delivery(self):
        job = self.create()
        claimed = self.service.next_job("erp-one")
        self.assertEqual(job["id"], claimed["id"])
        self.assertEqual("plan", claimed["action"])
        with self.assertRaisesRegex(ExchangeError, "dry-run"):
            self.service.confirm(job["id"], "张三")

        plan = {
            "plans": [
                {"o_id": "10001", "ok": True, "mode": "ChangeItem", "src_sku_id": "OLD-01", "new_sku_id": "NEW-01", "qty": 2},
                {"o_id": "10002", "ok": False, "reason": "未找到源 SKU"},
            ]
        }
        planned = self.service.report_plan(job["id"], "erp-one", plan)
        self.assertEqual("awaiting_confirm", planned["status"])
        self.assertEqual(1, planned["plan"]["exchangeable"])
        with self.assertRaisesRegex(ExchangeError, "创建该任务"):
            self.service.confirm(job["id"], "李四")

        confirmed = self.service.confirm(job["id"], "张三")
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual("confirmed", self.service.confirm(job["id"], "张三")["status"])
        execute = self.service.next_job("erp-one")
        self.assertEqual("execute", execute["action"])
        self.assertTrue(execute["executionToken"])
        self.assertIsNone(self.service.next_job("erp-two"))

        progress = self.service.report_progress(
            job["id"], "erp-one", execute["executionToken"], {"o_id": "10001", "status": "success"}
        )
        self.assertEqual(1, progress["progressCount"])
        result = self.service.report_result(
            job["id"], "erp-one", execute["executionToken"],
            {"succeeded": [{"o_id": "10001"}], "failed": []},
        )
        self.assertEqual("done", result["status"])
        again = self.service.report_result(
            job["id"], "erp-one", "already-consumed", {"succeeded": [{"o_id": "10001"}], "failed": []}
        )
        self.assertEqual("done", again["status"])

    def test_plan_must_cover_only_target_orders(self):
        job = self.create()
        self.service.next_job("erp-one")
        with self.assertRaisesRegex(ExchangeError, "没有覆盖全部"):
            self.service.report_plan(
                job["id"], "erp-one",
                {"plans": [{"o_id": "10001", "ok": False, "reason": "missing"}]},
            )
        with self.assertRaisesRegex(ExchangeError, "非目标"):
            self.service.report_plan(
                job["id"], "erp-one",
                {"plans": [
                    {"o_id": "10001", "ok": False, "reason": "missing"},
                    {"o_id": "99999", "ok": False, "reason": "missing"},
                ]},
            )

    def test_plan_cannot_replace_confirmed_rule(self):
        job = self.create()
        self.service.next_job("erp-one")
        with self.assertRaisesRegex(ExchangeError, "SKU 与任务规则不一致"):
            self.service.report_plan(
                job["id"], "erp-one",
                {"plans": [
                    {"o_id": "10001", "ok": True, "mode": "ChangeItem", "src_sku_id": "OLD-01", "new_sku_id": "ATTACKER", "qty": 1},
                    {"o_id": "10002", "ok": False, "reason": "未找到"},
                ]},
            )

    def test_regular_product_requires_matching_style_codes(self):
        payload = {
            **self.payload,
            "rules": {
                "strategy": "direct",
                "replacements": [{
                    "from": "STYLE-A-RED", "to": "STYLE-A-BLUE",
                    "sourceStyle": "STYLE-A", "targetStyle": "STYLE-A",
                }],
            },
        }
        job = self.service.create_job(payload, operator="张三")
        replacement = job["rules"]["replacements"][0]
        self.assertEqual("same_style", replacement["exchangeType"])
        self.assertEqual("STYLE-A", replacement["sourceStyle"])

        payload["rules"]["replacements"][0]["targetStyle"] = "STYLE-B"
        with self.assertRaisesRegex(ExchangeError, "只能在同一款式"):
            self.service.create_job(payload, operator="张三")

    def test_special_source_allows_only_maintained_targets(self):
        payload = {
            **self.payload,
            "rules": {
                "strategy": "direct",
                "replacements": [{
                    "from": "XZ25401308-101", "to": "XZ25401308-09901",
                    "sourceStyle": "XZ25401308-101", "targetStyle": "XZ25401308-099",
                }],
            },
        }
        job = self.service.create_job(payload, operator="张三")
        self.assertEqual("special_mapping", job["rules"]["replacements"][0]["exchangeType"])
        payload["rules"]["replacements"][0]["to"] = "XZ25401308-09999"
        with self.assertRaisesRegex(ExchangeError, "只能更换为"):
            self.service.create_job(payload, operator="张三")

    def test_read_only_sku_search_queue(self):
        search = self.service.create_search("XZ25401308-101")
        self.assertEqual("pending", search["status"])
        claimed = self.service.next_search("erp-one")
        self.assertEqual(search["id"], claimed["id"])
        done = self.service.report_search(search["id"], "erp-one", {
            "sku": "XZ25401308-101", "scannedOrders": 10,
            "matchedOrders": 1, "orders": [{"o_id": "10001", "quantity": 2}],
        })
        self.assertEqual("done", done["status"])
        self.assertEqual(1, done["result"]["matchedOrders"])

    def test_read_only_purchase_probe_queue(self):
        probe = self.service.create_probe("purchase_items", "628190")
        self.assertEqual("pending", probe["status"])
        claimed = self.service.next_probe("erp-one")
        self.assertEqual(probe["id"], claimed["id"])
        done = self.service.report_probe(probe["id"], "erp-one", {
            "poId": "628190", "count": 1,
            "items": [{"sku_id": "SKU-01", "qty": 2}],
        })
        self.assertEqual("done", done["status"])
        self.assertEqual("SKU-01", done["result"]["items"][0]["sku_id"])


if __name__ == "__main__":
    unittest.main()
