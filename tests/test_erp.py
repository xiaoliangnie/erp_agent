# -*- coding: utf-8 -*-
"""Digital Worker：配置不泄密、命令白名单、换货页注入用假页面。"""
import json
import unittest

from backend.erp import (
    ALLOWED_COMMANDS,
    DigitalRuntime,
    DigitalWorkerLoop,
    ErpError,
    ErpUnknownResult,
    load_digital_worker,
)
from backend.erp import exchange_page


class DigitalWorkerConfigTests(unittest.TestCase):
    def test_hides_secrets_and_defaults_portal(self):
        values = {
            "ERP_AI_ENABLED": "true",
            "ERP_AI_USERNAME": "ai-procurement",
            "ERP_AI_PASSWORD": "hunter2-not-logged",
            "ERP_AI_TOTP_SECRET": "totp-seed-not-logged",
        }
        status = load_digital_worker(lambda name, default="": values.get(name, default))
        self.assertTrue(status["enabled"])
        self.assertEqual("erp-ai-procurement", status["workerId"])
        self.assertEqual("https://www.erp321.com/epaas", status["baseUrl"])
        self.assertEqual("https://www.erp321.com/app/order/order/list.aspx", status["orderListUrl"])
        self.assertEqual("ai-procurement", status["username"])
        self.assertTrue(status["hasPassword"])
        self.assertEqual("#login_id", status["loginFields"]["account"])
        self.assertNotIn("password", status)
        dumped = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("hunter2-not-logged", dumped)
        self.assertNotIn("totp-seed-not-logged", dumped)

    def test_disabled_when_empty(self):
        status = load_digital_worker(lambda name, default="": default)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["hasPassword"])
        self.assertEqual(("erp.exchange_items",), ALLOWED_COMMANDS)


class FakePage:
    def __init__(self, *, url="", has_acp=True, ready=True, explode=None):
        self.url = url or "https://www.erp321.com/app/order/order/list.aspx"
        self.has_acp = has_acp
        self.ready_flag = ready
        self.explode = explode
        self.scripts = []
        self.calls = []

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_function(self, expr, timeout=0):
        if not self.has_acp:
            raise TimeoutError("no _ACP")

    def add_script_tag(self, content=""):
        self.scripts.append(content)

    def evaluate(self, expr, arg=None):
        self.calls.append(expr)
        if self.explode and "executeJob" in str(expr):
            raise RuntimeError(self.explode)
        if "hasAcp" in str(expr) or "JstOrderExchange.ready" in str(expr):
            return {
                "ready": bool(self.ready_flag and self.scripts),
                "version": "0.6.0",
                "hasAcp": self.has_acp,
                "href": self.url,
            }
        if "planJob" in str(expr):
            return {
                "total": 1, "exchangeable": 1, "skipped": 0,
                "plans": [{"o_id": "10001", "ok": True, "mode": "ChangeItem"}],
            }
        if "executeJob" in str(expr):
            return {"succeeded": ["10001"], "failed": [], "attempted": 1}
        return None


class ExchangePageTests(unittest.TestCase):
    def test_plan_and_execute_on_ready_page(self):
        page = FakePage()
        ready = exchange_page.ensure_order_page(page, "https://www.erp321.com/app/order/order/list.aspx", core_js="/*core*/")
        self.assertTrue(ready["ready"])
        self.assertEqual(["/*core*/"], page.scripts)
        plan = exchange_page.plan_job(page, {"rules": {}, "targets": {"o_ids": ["10001"]}})
        self.assertEqual(1, plan["exchangeable"])
        result = exchange_page.execute_job(page, {"plan": {"plans": plan["plans"]}})
        self.assertEqual(["10001"], result["succeeded"])

    def test_ensure_order_page_skips_reload_when_ready(self):
        page = FakePage()
        gotos = []
        original = page.goto

        def goto(url, **kwargs):
            gotos.append(url)
            return original(url, **kwargs)

        page.goto = goto
        url = "https://www.erp321.com/app/order/order/list.aspx"
        first = exchange_page.ensure_order_page(page, url, core_js="/*core*/")
        second = exchange_page.ensure_order_page(page, url, core_js="/*again*/")
        self.assertTrue(first["ready"])
        self.assertTrue(second["ready"])
        self.assertEqual(0, len(gotos))
        self.assertEqual(["/*core*/"], page.scripts)

    def test_login_redirect_is_a_hard_error(self):
        page = FakePage(url="https://www.erp321.com/login.aspx")
        page.goto = lambda url, **kwargs: setattr(page, "url", "https://www.erp321.com/login.aspx")
        with self.assertRaisesRegex(ErpError, "登录页"):
            exchange_page.ensure_order_page(page, "https://www.erp321.com/app/order/order/list.aspx", core_js="")

    def test_unknown_write_result_is_not_a_plain_error(self):
        page = FakePage(explode="target closed")
        with self.assertRaises(ErpUnknownResult):
            exchange_page.execute_job(page, {"plan": {"plans": []}})


class RuntimeCommandTests(unittest.TestCase):
    def test_unknown_command_is_rejected(self):
        runtime = DigitalRuntime({
            "enabled": True, "workerId": "erp-ai-procurement",
            "baseUrl": "https://www.erp321.com/epaas",
            "orderListUrl": "https://www.erp321.com/app/order/order/list.aspx",
            "headless": True, "writeDelayMs": 250,
            "storageStatePath": "unused.json",
        })
        with self.assertRaisesRegex(ErpError, "未注册"):
            runtime.run("erp.create_purchase_order", {})

    def test_run_from_asyncio_loop_stays_off_the_loop_thread(self):
        import asyncio
        import threading

        class RecordingSession:
            def __init__(self):
                self.page = FakePage()
                self.thread_name = ""

            def login_if_needed(self, **kwargs):
                self.thread_name = threading.current_thread().name
                return {"ok": True}

            def save_state(self):
                return None

            def close(self):
                return None

        session = RecordingSession()
        runtime = DigitalRuntime({
            "enabled": True, "workerId": "erp-ai-procurement",
            "baseUrl": "https://www.erp321.com/epaas",
            "orderListUrl": "https://www.erp321.com/app/order/order/list.aspx",
            "headless": True, "writeDelayMs": 250,
            "storageStatePath": "unused.json",
        }, session=session)

        async def go():
            loop_thread = threading.current_thread().name
            result = runtime.run("erp.exchange_items", {
                "rules": {}, "targets": {"o_ids": ["10001"]},
            })
            return loop_thread, result

        loop_thread, result = asyncio.run(go())
        self.assertEqual("erp-playwright", session.thread_name)
        self.assertNotEqual(loop_thread, session.thread_name)
        self.assertEqual(1, result["exchangeable"])


class FakeExchange:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.heartbeats = []
        self.plans = []
        self.results = []

    def heartbeat(self, worker_id, payload):
        self.heartbeats.append((worker_id, payload))

    def next_job(self, worker_id):
        return self.jobs.pop(0) if self.jobs else None

    def report_plan(self, job_id, worker_id, plan):
        self.plans.append((job_id, worker_id, plan))

    def report_result(self, job_id, worker_id, token, result):
        self.results.append((job_id, worker_id, token, result))


class FakeRuntime:
    def __init__(self):
        self.config = {
            "enabled": True, "workerId": "erp-ai-procurement",
            "orderListUrl": "https://www.erp321.com/app/order/order/list.aspx",
        }
        self.calls = []

    def status(self):
        return {"enabled": True, "workerId": "erp-ai-procurement"}

    def run(self, command, payload):
        self.calls.append((command, payload))
        if payload.get("confirm"):
            return {"succeeded": ["10001"], "failed": []}
        return {"total": 1, "exchangeable": 1, "skipped": 0, "plans": [{"o_id": "10001", "ok": True}]}

    def close(self):
        return None


class WorkerLoopTests(unittest.TestCase):
    def test_plan_tick_reports_and_does_not_claim_when_disabled(self):
        exchange = FakeExchange([{
            "id": "abc", "action": "plan", "rules": {}, "targets": {"o_ids": ["10001"]},
        }])
        loop = DigitalWorkerLoop(FakeRuntime(), exchange)
        loop._tick()
        self.assertEqual(1, len(exchange.plans))
        self.assertEqual("abc", exchange.plans[0][0])
        self.assertFalse(loop.claims_jobs)

    def test_unknown_execute_does_not_report_result(self):
        class BoomRuntime(FakeRuntime):
            def run(self, command, payload):
                raise ErpUnknownResult("page crashed")

        exchange = FakeExchange([{
            "id": "abc", "action": "execute", "executionToken": "tok",
        }])
        loop = DigitalWorkerLoop(BoomRuntime(), exchange)
        loop._tick()
        self.assertEqual([], exchange.results)


class CoreScriptTests(unittest.TestCase):
    def test_core_file_exists(self):
        self.assertTrue(exchange_page.CORE_PATH.exists())
        self.assertIn("JstOrderExchange", exchange_page.read_core())


if __name__ == "__main__":
    unittest.main()
