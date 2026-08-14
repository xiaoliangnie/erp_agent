from datetime import datetime, timedelta, timezone
import json
import os
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

        self.service.cancel(first["id"], "张三")
        created = self.service.create_job(overlap, operator="李四")
        self.assertEqual(["10002", "20001"], created["targets"]["o_ids"])

    def test_cancel_requires_creating_operator(self):
        job = self.create()
        with self.assertRaisesRegex(ExchangeError, "操作人姓名"):
            self.service.cancel(job["id"], "")
        with self.assertRaisesRegex(ExchangeError, "创建该任务的操作人"):
            self.service.cancel(job["id"], "李四")
        cancelled = self.service.cancel(job["id"], "张三")
        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual("cancelled", self.service.cancel(job["id"], "李四")["status"])

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

    def _age(self, table: str, row_id: str, minutes: int = 10):
        past = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with self.service._connect() as conn:
            conn.execute(
                f"UPDATE {table} SET claimed_at=?, updated_at=? WHERE id=?",
                (past, past, row_id),
            )

    def test_planning_timeout_returns_to_pending(self):
        job = self.create()
        claimed = self.service.next_job("erp-one")
        self.assertEqual("planning", claimed["status"])
        self._age("exchange_jobs", job["id"])
        jobs = self.service.list_jobs()
        self.assertEqual("pending", jobs[0]["status"])
        self.assertEqual(1, jobs[0]["attempts"])
        again = self.service.next_job("erp-two")
        self.assertEqual(job["id"], again["id"])
        self.assertEqual("plan", again["action"])

    def test_planning_timeout_fails_after_max_attempts(self):
        job = self.create()
        for _ in range(self.service.max_claim_attempts):
            claimed = self.service.next_job("erp-one")
            self.assertIsNotNone(claimed)
            self._age("exchange_jobs", job["id"])
            self.service.list_jobs()
        failed = self.service.get_job(job["id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual(self.service.max_claim_attempts, failed["attempts"])
        self.assertIsNone(self.service.next_job("erp-two"))

    def _confirm_execute(self, job_id: str, worker: str = "erp-one") -> dict:
        self.service.next_job(worker)
        self.service.report_plan(job_id, worker, {
            "plans": [
                {"o_id": "10001", "ok": True, "mode": "ChangeItem", "src_sku_id": "OLD-01", "new_sku_id": "NEW-01"},
                {"o_id": "10002", "ok": False, "reason": "未找到源 SKU"},
            ],
        })
        self.service.confirm(job_id, "张三")
        return self.service.next_job(worker)

    def test_executing_timeout_marks_stuck_and_alerts(self):
        seen = []
        self.service.on_stuck = seen.append
        job = self.create()
        execute = self._confirm_execute(job["id"])
        self.assertEqual("execute", execute["action"])
        self._age("exchange_jobs", job["id"], minutes=20)
        listed = self.service.list_jobs()[0]
        self.assertEqual("stuck", listed["status"])
        self.assertEqual(1, len(seen))
        self.assertEqual(job["id"], seen[0]["id"])
        self.assertIsNone(self.service.next_job("erp-two"))

    def test_stuck_job_does_not_block_new_job_on_same_orders(self):
        job = self.create()
        self._confirm_execute(job["id"])
        self._age("exchange_jobs", job["id"], minutes=20)
        self.service.list_jobs()
        created = self.create()
        self.assertNotEqual(job["id"], created["id"])
        self.assertEqual("pending", created["status"])

    def test_stuck_job_accepts_late_worker_result(self):
        job = self.create()
        execute = self._confirm_execute(job["id"])
        token = execute["executionToken"]
        self._age("exchange_jobs", job["id"], minutes=20)
        self.service.list_jobs()
        self.assertEqual("stuck", self.service.get_job(job["id"])["status"])
        done = self.service.report_result(
            job["id"], "erp-one", token,
            {"succeeded": ["10001"], "failed": []},
        )
        self.assertEqual("done", done["status"])
        self.assertEqual(["10001"], done["result"]["succeeded"])

    def test_search_timeout_returns_to_pending(self):
        search = self.service.create_search("OLD-01")
        self.service.next_search("erp-one")
        self._age("exchange_searches", search["id"])
        again = self.service.next_search("erp-two")
        self.assertEqual(search["id"], again["id"])
        self.assertEqual("searching", again["status"])
        self.assertEqual(1, self.service.get_search(search["id"])["attempts"])

    def test_probe_timeout_returns_to_pending(self):
        probe = self.service.create_probe("purchase_items", "628190")
        self.service.next_probe("erp-one")
        self._age("erp_read_probes", probe["id"])
        again = self.service.next_probe("erp-two")
        self.assertEqual(probe["id"], again["id"])
        self.assertEqual("reading", again["status"])
        self.assertEqual(1, self.service.get_probe(probe["id"])["attempts"])


class ExchangePolicyReloadTests(unittest.TestCase):
    def test_load_policy_picks_up_file_mtime_change(self):
        from backend.exchange import policy as policy_mod

        original = policy_mod.RULE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exchange-rules.json"
            path.write_text(json.dumps({
                "defaultPolicy": "same_style",
                "specialMappings": [{
                    "name": "A",
                    "sourceSku": "SKU-A",
                    "sourceStyle": "A",
                    "targetStyle": "B",
                    "targetSkus": ["T1"],
                }],
            }), encoding="utf-8")
            policy_mod.RULE_PATH = path
            policy_mod._load_policy.cache_clear()
            try:
                first = policy_mod.load_policy()
                self.assertEqual("SKU-A", first["specialMappings"][0]["sourceSku"])
                path.write_text(json.dumps({
                    "defaultPolicy": "same_style",
                    "specialMappings": [{
                        "name": "B",
                        "sourceSku": "SKU-B",
                        "sourceStyle": "B",
                        "targetStyle": "C",
                        "targetSkus": ["T2", "T3"],
                    }],
                }), encoding="utf-8")
                later = path.stat().st_mtime + 2
                os.utime(path, (later, later))
                second = policy_mod.load_policy()
                self.assertEqual("SKU-B", second["specialMappings"][0]["sourceSku"])
                self.assertEqual(["T2", "T3"], second["specialMappings"][0]["targetSkus"])
            finally:
                policy_mod.RULE_PATH = original
                policy_mod._load_policy.cache_clear()


if __name__ == "__main__":
    unittest.main()
