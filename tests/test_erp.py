# -*- coding: utf-8 -*-
"""Digital Worker：配置不泄密、命令白名单、换货页注入用假页面。"""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from backend.business_time import BUSINESS_TIMEZONE
from backend.erp import (
    ALLOWED_COMMANDS,
    DigitalRuntime,
    DigitalWorkerLoop,
    ErpError,
    ErpKeepAlive,
    ErpUnknownResult,
    in_keepalive_window,
    load_digital_worker,
)
from backend.erp import evidence, exchange_page, purchase_page
from backend.erp.keepalive import parse_hhmm


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

    def test_enabled_by_default_without_secrets(self):
        status = load_digital_worker(lambda name, default="": default)
        self.assertTrue(status["enabled"])
        self.assertFalse(status["hasPassword"])
        self.assertEqual("10235039", status["ownerCoId"])
        self.assertEqual(("erp.exchange_items",), ALLOWED_COMMANDS)

    def test_explicit_disable_is_kill_switch(self):
        status = load_digital_worker(
            lambda name, default="": "false" if name == "ERP_AI_ENABLED" else default
        )
        self.assertFalse(status["enabled"])


class FakeResponse:
    def __init__(self, *, text="", status=200, body=b"", headers=None):
        self.status = status
        self._text = text
        self._body = body
        self.headers = headers or {}

    def text(self):
        return self._text

    def body(self):
        return self._body


class FakeRequest:
    def __init__(self, routes=None):
        self.routes = dict(routes or {})

    def get(self, url, **kwargs):
        for key, response in self.routes.items():
            if key in str(url):
                return response
        return FakeResponse(status=404, text="not found")


class FakePage:
    def __init__(self, *, url="", has_acp=True, ready=True, explode=None, orders=None, request=None):
        self.url = url or "https://www.erp321.com/app/order/order/list.aspx"
        self.has_acp = has_acp
        self.ready_flag = ready
        self.explode = explode
        self.scripts = []
        self.calls = []
        self.orders = {str(key): dict(value) for key, value in (orders or {}).items()}
        self.request = request

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_function(self, expr, timeout=0):
        if not self.has_acp:
            raise TimeoutError("no _ACP")

    def add_script_tag(self, content=""):
        self.scripts.append(content)

    def evaluate(self, expr, arg=None, **kwargs):
        self.calls.append(expr)
        if self.explode and "executeJob" in str(expr):
            raise RuntimeError(self.explode)
        if "hasAcp" in str(expr) or "JstOrderExchange.ready" in str(expr):
            return {
                "ready": bool(self.ready_flag and self.scripts),
                "version": exchange_page.CORE_VERSION,
                "hasAcp": self.has_acp,
                "href": self.url,
            }
        if "searchSku" in str(expr):
            sku = str((arg or {}).get("sku") or "")
            matches = []
            for oid, order in self.orders.items():
                items = order.get("items") or []
                if any(str(line.get("sku_id") or line.get("sku") or "") == sku for line in items):
                    matches.append({"o_id": oid, "sku": sku})
            return {"sku": sku, "matches": matches, "failures": []}
        if "planJob" in str(expr):
            return {
                "total": 1, "exchangeable": 1, "skipped": 0,
                "plans": [{"o_id": "10001", "ok": True, "mode": "ChangeItem"}],
            }
        if "loadOrders" in str(expr):
            oids = list((arg or {}).get("oids") or [])
            rows = []
            for oid in oids:
                key = str(oid or "")
                if key in self.orders:
                    rows.append({"o_id": key, **self.orders[key]})
                else:
                    rows.append({"o_id": key, "items": [], "load_error": "ERP 未返回该订单"})
            return {"orders": rows, "count": len(rows)}
        if "loadOrder" in str(expr):
            oid = str(arg or "")
            if oid in self.orders:
                return {"o_id": oid, **self.orders[oid]}
            return {"o_id": oid, "items": [], "load_error": "ERP 未返回该订单"}
        if "executeJob" in str(expr):
            if self.orders:
                job = (arg or {}).get("job") or {}
                plans = job.get("plans") or (job.get("plan") or {}).get("plans") or []
                succeeded = []
                for plan in plans:
                    oid = str(plan.get("o_id") or "")
                    source = str(plan.get("src_sku_id") or "")
                    target = str(plan.get("new_sku_id") or "")
                    current = dict(self.orders.get(oid) or {})
                    items = []
                    for line in current.get("items") or []:
                        sku = str(line.get("sku_id") or line.get("sku") or "")
                        items.append({**line, "sku_id": target if sku == source and target else sku})
                    if oid:
                        self.orders[oid] = {**current, "items": items}
                        succeeded.append({"o_id": oid})
                return {"succeeded": succeeded, "failed": [], "attempted": len(plans)}
            return {"succeeded": ["10001"], "failed": [], "attempted": 1}
        return None


class ExchangePageTests(unittest.TestCase):
    def test_core_version_matches_script(self):
        source = exchange_page.read_core()
        self.assertIn(f"const VERSION = '{exchange_page.CORE_VERSION}'", source)
        self.assertIn("concurrency", source)
        self.assertIn("loadOrders", source)

    def test_load_orders_uses_one_evaluate(self):
        page = FakePage(orders={
            "10001": {"items": [{"sku_id": "A"}]},
            "10002": {"items": [{"sku_id": "B"}]},
        })
        loaded = exchange_page.load_orders(page, ["10001", "10002", "10003"], concurrency=5)
        self.assertEqual("A", loaded["10001"]["items"][0]["sku_id"])
        self.assertEqual("B", loaded["10002"]["items"][0]["sku_id"])
        self.assertTrue(loaded["10003"].get("load_error"))
        self.assertEqual(1, sum(1 for call in page.calls if "loadOrders" in str(call)))

    def test_plan_and_execute_on_ready_page(self):
        page = FakePage()
        ready = exchange_page.ensure_order_page(page, "https://www.erp321.com/app/order/order/list.aspx", core_js="/*core*/")
        self.assertTrue(ready["ready"])
        self.assertEqual(["/*core*/"], page.scripts)
        found = exchange_page.search_sku(page, "OLD-01")
        self.assertEqual([], found["matches"])
        plan = exchange_page.plan_job(page, {"rules": {}, "targets": {"o_ids": ["10001"]}})
        self.assertEqual(1, plan["exchangeable"])
        result = exchange_page.execute_job(page, {"plan": {"plans": plan["plans"]}})
        self.assertEqual(["10001"], result["succeeded"])

    def test_ensure_order_page_reinjects_stale_core(self):
        page = FakePage()
        page.scripts.append("/*old*/")
        url = "https://www.erp321.com/app/order/order/list.aspx"
        original = page.evaluate

        def evaluate(expr, arg=None, **kwargs):
            result = original(expr, arg, **kwargs)
            if isinstance(result, dict) and "version" in result:
                result = dict(result)
                result["version"] = "0.6.0"
                result["ready"] = True
            return result

        page.evaluate = evaluate
        ready = exchange_page.ensure_order_page(page, url, core_js="/*core-v7*/")
        self.assertTrue(ready["ready"])
        self.assertEqual(["/*old*/", "/*core-v7*/"], page.scripts)

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

    def test_load_order_returns_load_error_instead_of_raising(self):
        page = FakePage()
        loaded = exchange_page.load_order(page, "10001")
        self.assertEqual("10001", loaded["o_id"])
        self.assertTrue(loaded.get("load_error"))


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
        runtime.close()

    def test_exclusive_blocks_a_second_job(self):
        entered = []
        mid = threading.Event()
        go = threading.Event()

        class Session:
            def __init__(self):
                self.page = FakePage()

            def login_if_needed(self, **kwargs):
                entered.append(threading.current_thread().name)
                if len(entered) == 1:
                    mid.set()
                    go.wait(2)
                return {"ok": True}

            def save_state(self):
                return None

            def close(self):
                return None

        runtime = DigitalRuntime({
            "enabled": True, "workerId": "erp-ai-procurement",
            "baseUrl": "https://www.erp321.com/epaas",
            "orderListUrl": "https://www.erp321.com/app/order/order/list.aspx",
            "headless": True, "writeDelayMs": 250,
            "storageStatePath": "unused.json",
        }, session=Session())

        def job():
            with runtime.exclusive():
                runtime.run("erp.exchange_items", {
                    "rules": {}, "targets": {"o_ids": ["10001"]},
                })

        first = threading.Thread(target=job)
        second = threading.Thread(target=job)
        first.start()
        self.assertTrue(mid.wait(2))
        second.start()
        time.sleep(0.15)
        self.assertEqual(1, len(entered))
        go.set()
        first.join(2)
        second.join(2)
        self.assertEqual(2, len(entered))
        runtime.close()


class FakeExchange:
    def __init__(self, jobs=None, searches=None, probes=None):
        self.jobs = list(jobs or [])
        self.searches = list(searches or [])
        self.probes = list(probes or [])
        self.heartbeats = []
        self.plans = []
        self.results = []
        self.search_results = []
        self.probe_results = []

    def heartbeat(self, worker_id, payload):
        self.heartbeats.append((worker_id, payload))

    def next_job(self, worker_id):
        return self.jobs.pop(0) if self.jobs else None

    def report_plan(self, job_id, worker_id, plan):
        self.plans.append((job_id, worker_id, plan))

    def report_result(self, job_id, worker_id, token, result):
        self.results.append((job_id, worker_id, token, result))

    def next_search(self, worker_id):
        return self.searches.pop(0) if self.searches else None

    def report_search(self, search_id, worker_id, result):
        self.search_results.append((search_id, worker_id, result))

    def next_probe(self, worker_id):
        return self.probes.pop(0) if self.probes else None

    def report_probe(self, probe_id, worker_id, result):
        self.probe_results.append((probe_id, worker_id, result))


class FakeImages:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.uploads = []
        self.finished = []

    def next(self, worker_id):
        return self.jobs.pop(0) if self.jobs else None

    def upload(self, job_id, worker_id, payload):
        self.uploads.append((job_id, worker_id, payload))
        return {"ok": True, "sku": payload.get("sku")}

    def finish(self, job_id, worker_id, result):
        self.finished.append((job_id, worker_id, result))
        return {"id": job_id, **result}


class FakeRuntime:
    def __init__(self, page=None):
        self.config = {
            "enabled": True, "workerId": "erp-ai-procurement",
            "orderListUrl": "https://www.erp321.com/app/order/order/list.aspx",
            "baseUrl": "https://www.erp321.com/epaas",
            "ownerCoId": "10235039",
            "username": "",
            "hasPassword": False,
            "storageStatePath": "",
        }
        self.calls = []
        self.page = page or FakePage()

    def status(self):
        return {"enabled": True, "workerId": "erp-ai-procurement"}

    def run(self, command, payload):
        self.calls.append((command, payload))
        if payload.get("confirm"):
            return {"succeeded": ["10001"], "failed": []}
        return {"total": 1, "exchangeable": 1, "skipped": 0, "plans": [{"o_id": "10001", "ok": True}]}

    def run_browser(self, func, *args, **kwargs):
        self.calls.append(("run_browser", func.__name__, args))
        return func(self.page, *args, **kwargs)

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

    def test_reconciliation_failed_is_reported_not_retried(self):
        class ReconRuntime(FakeRuntime):
            def run(self, command, payload):
                return {
                    "succeeded": [],
                    "failed": [{"o_id": "10001", "error": "回读与预览不一致"}],
                    "reconciliation": {"status": "reconciliation_failed"},
                }

        exchange = FakeExchange([{
            "id": "abc", "action": "execute", "executionToken": "tok",
        }])
        loop = DigitalWorkerLoop(ReconRuntime(), exchange)
        loop._tick()
        self.assertEqual(1, len(exchange.results))
        self.assertEqual("reconciliation_failed", exchange.results[0][3]["reconciliation"]["status"])

    def test_start_without_playwright_does_not_claim(self):
        with patch("backend.erp.loop.playwright_available", return_value=False):
            loop = DigitalWorkerLoop(FakeRuntime(), FakeExchange())
            status = loop.start()
        self.assertFalse(loop.claims_jobs)
        self.assertFalse(status["running"])
        self.assertIn("Playwright", status["lastError"])

    def test_start_without_login_does_not_claim(self):
        with patch("backend.erp.loop.playwright_available", return_value=True):
            loop = DigitalWorkerLoop(FakeRuntime(), FakeExchange())
            status = loop.start()
        self.assertFalse(loop.claims_jobs)
        self.assertFalse(status["running"])
        self.assertIn("登录", status["lastError"])

    def test_start_with_login_claims(self):
        runtime = FakeRuntime()
        runtime.config["username"] = "ai"
        runtime.config["hasPassword"] = True
        with patch("backend.erp.loop.playwright_available", return_value=True):
            loop = DigitalWorkerLoop(runtime, FakeExchange())
            try:
                status = loop.start()
                self.assertTrue(loop.claims_jobs)
                self.assertTrue(status["running"])
            finally:
                loop.stop()
        self.assertFalse(loop.claims_jobs)

    def test_probe_tick_reports_purchase_items(self):
        html = '<textarea id="_jt_data">{"datas":[{"sku_id":"SKU-1"}]}</textarea>'
        page = FakePage(request=FakeRequest({
            "purchaseitem.aspx": FakeResponse(text=html),
        }))
        exchange = FakeExchange(probes=[{
            "id": "probe1", "kind": "purchase_items", "reference": "604264",
        }])
        loop = DigitalWorkerLoop(FakeRuntime(page), exchange)
        loop._tick()
        self.assertEqual(1, len(exchange.probe_results))
        self.assertEqual(1, exchange.probe_results[0][2]["count"])
        self.assertEqual("SKU-1", exchange.probe_results[0][2]["items"][0]["sku_id"])

    def test_search_tick_reports_matches(self):
        page = FakePage(orders={"10001": {"items": [{"sku_id": "OLD-01"}]}})
        exchange = FakeExchange(searches=[{"id": "s1", "sku": "OLD-01"}])
        loop = DigitalWorkerLoop(FakeRuntime(page), exchange)
        loop._tick()
        self.assertEqual(1, len(exchange.search_results))
        self.assertEqual("10001", exchange.search_results[0][2]["matches"][0]["o_id"])

    def test_image_tick_uploads_and_finishes(self):
        html = (
            '<textarea id="_jt_data">'
            '{"datas":[{"sku_id":"SKU-1","pic300":"/pic/a.jpg"}]}'
            "</textarea>"
        )
        page = FakePage(request=FakeRequest({
            "purchaseitem.aspx": FakeResponse(text=html),
            "/pic/a.jpg": FakeResponse(
                body=b"\xff\xd8\xff\xdbfake",
                headers={"content-type": "image/jpeg"},
            ),
        }))
        images = FakeImages([{
            "id": "img1", "purchaseOrderNo": "604264",
            "targets": [{"sku": "SKU-1"}],
        }])
        loop = DigitalWorkerLoop(FakeRuntime(page), FakeExchange(), images=images)
        loop._tick()
        self.assertEqual(1, len(images.uploads))
        self.assertEqual("SKU-1", images.uploads[0][2]["sku"])
        self.assertEqual(1, len(images.finished))
        self.assertEqual([], images.finished[0][2]["failed"])


class PurchasePageTests(unittest.TestCase):
    def test_parse_purchase_items_from_textarea(self):
        html = '<html><textarea id="_jt_data">{"datas":[{"sku_id":"A"}]}</textarea></html>'
        rows = purchase_page.parse_purchase_items(html)
        self.assertEqual([{"sku_id": "A"}], rows)

    def test_parse_purchase_items_rejects_login_page(self):
        with self.assertRaises(ErpError) as ctx:
            purchase_page.parse_purchase_items("<html>login.aspx</html>")
        self.assertIn("登录", str(ctx.exception))

    def test_item_image_url_prefers_pic300(self):
        url = purchase_page.item_image_url(
            {"pic160": "/b.jpg", "pic300": "/a.jpg"},
            "https://www.erp321.com",
        )
        self.assertEqual("https://www.erp321.com/a.jpg", url)


class KeepAliveTests(unittest.TestCase):
    def test_window_is_beijing_office_hours(self):
        def at(hour, minute):
            return datetime(2026, 8, 17, hour, minute, tzinfo=BUSINESS_TIMEZONE)

        self.assertEqual((9, 30), parse_hhmm("09:30"))
        self.assertFalse(in_keepalive_window(at(9, 29)))
        self.assertTrue(in_keepalive_window(at(9, 30)))
        self.assertTrue(in_keepalive_window(at(17, 44)))
        self.assertTrue(in_keepalive_window(at(18, 30)))
        self.assertFalse(in_keepalive_window(at(18, 31)))

    def test_tick_keeps_session_not_order_page(self):
        calls = []

        class Runtime:
            def try_exclusive(self):
                return True

            def release_exclusive(self):
                calls.append("release")

            def keep_session(self):
                calls.append("keep")
                return {"ok": True, "method": "already", "url": "https://www.erp321.com/epaas"}

            def close_browser(self):
                calls.append("close")

        keeper = ErpKeepAlive(Runtime(), interval_seconds=180)
        now = datetime(2026, 8, 17, 10, 0, tzinfo=BUSINESS_TIMEZONE)
        result = keeper.tick(now=now)
        self.assertTrue(result.get("ok"))
        self.assertEqual(["keep", "release"], calls)
        self.assertTrue(keeper.status()["warmed"])

        later = datetime(2026, 8, 17, 10, 1, tzinfo=BUSINESS_TIMEZONE)
        skipped = keeper.tick(now=later)
        self.assertTrue(skipped.get("skipped"))
        self.assertEqual("未到保活间隔", skipped["reason"])

    def test_tick_skips_when_write_holds_the_lock(self):
        class Runtime:
            def try_exclusive(self):
                return False

            def keep_session(self):
                raise AssertionError("写入中不应保活")

        keeper = ErpKeepAlive(Runtime())
        result = keeper.tick(now=datetime(2026, 8, 17, 10, 0, tzinfo=BUSINESS_TIMEZONE))
        self.assertEqual("写入中", result["reason"])

    def test_after_hours_still_keeps_session(self):
        calls = []

        class Runtime:
            def try_exclusive(self):
                return True

            def release_exclusive(self):
                calls.append("release")

            def keep_session(self):
                calls.append("keep")
                return {"ok": True}

            def close_browser(self):
                calls.append("close")

        keeper = ErpKeepAlive(Runtime(), interval_seconds=30)
        night = datetime(2026, 8, 17, 22, 0, tzinfo=BUSINESS_TIMEZONE)
        result = keeper.tick(now=night)
        self.assertTrue(result.get("ok"))
        self.assertEqual(["keep", "release"], calls)

    def test_stop_closes_browser(self):
        calls = []

        class Runtime:
            def try_exclusive(self):
                return True

            def release_exclusive(self):
                calls.append("release")

            def close_browser(self):
                calls.append("close")

        keeper = ErpKeepAlive(Runtime())
        keeper._warmed = True
        keeper.stop()
        self.assertEqual(["close", "release"], calls)
        self.assertFalse(keeper._warmed)

    def test_keep_session_does_not_open_order_list(self):
        class Session:
            def __init__(self):
                self.page = FakePage(url="https://www.erp321.com/epaas")
                self.ensured = 0

            def login_if_needed(self, **kwargs):
                return {"ok": True, "loggedIn": True, "method": "already"}

            def save_state(self):
                return None

            def close(self):
                self.page = None

        session = Session()
        runtime = DigitalRuntime({
            "enabled": False, "workerId": "erp-ai-procurement",
            "baseUrl": "https://www.erp321.com/epaas",
            "orderListUrl": "https://www.erp321.com/app/order/order/list.aspx",
            "headless": True, "writeDelayMs": 250,
            "storageStatePath": "unused.json",
        }, session=session)
        original = exchange_page.ensure_order_page

        def boom(*args, **kwargs):
            session.ensured += 1
            return original(*args, **kwargs)

        exchange_page.ensure_order_page = boom
        try:
            result = runtime.keep_session()
        finally:
            exchange_page.ensure_order_page = original
            runtime.close()
        self.assertEqual(0, session.ensured)
        self.assertEqual("erp.keep_session", result["command"])
        self.assertIn("epaas", result.get("url") or "")


class CoreScriptTests(unittest.TestCase):
    def test_core_file_exists(self):
        self.assertTrue(exchange_page.CORE_PATH.exists())
        self.assertIn("JstOrderExchange", exchange_page.read_core())


class EvidenceTests(unittest.TestCase):
    def test_match_is_ok(self):
        expected = [{"oId": "1", "sourceSku": "OLD", "targetSku": "NEW"}]
        before = {"1": {"skus": ["OLD"], "loadError": ""}}
        after = {"1": {"skus": ["NEW"], "loadError": ""}}
        recon = evidence.reconcile(before, after, expected)
        self.assertTrue(recon["ok"])
        self.assertEqual(["1"], recon["confirmed"])

    def test_source_still_present_fails(self):
        expected = [{"oId": "1", "sourceSku": "OLD", "targetSku": "NEW"}]
        before = {"1": {"skus": ["OLD"], "loadError": ""}}
        after = {"1": {"skus": ["OLD"], "loadError": ""}}
        recon = evidence.reconcile(before, after, expected)
        self.assertEqual("reconciliation_failed", recon["status"])
        applied = evidence.apply_reconciliation(
            {"succeeded": [{"o_id": "1"}], "failed": []}, recon,
        )
        self.assertEqual([], applied["succeeded"])
        self.assertEqual("1", applied["failed"][0]["o_id"])

    def test_already_exchanged_is_idempotent(self):
        expected = [{"oId": "1", "sourceSku": "OLD", "targetSku": "NEW"}]
        before = {"1": {"skus": ["NEW"], "loadError": ""}}
        after = {"1": {"skus": ["NEW"], "loadError": ""}}
        recon = evidence.reconcile(before, after, expected)
        self.assertEqual(["1"], recon["alreadyDone"])
        self.assertTrue(recon["ok"])

    def test_after_load_error_is_unknown(self):
        expected = [{"oId": "1", "sourceSku": "OLD", "targetSku": "NEW"}]
        before = {"1": {"skus": ["OLD"], "loadError": ""}}
        after = {"1": {"skus": [], "loadError": "timeout"}}
        recon = evidence.reconcile(before, after, expected)
        self.assertEqual("unknown", recon["status"])

    def test_write_evidence_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = evidence.write_evidence(
                tmp, command="erp.exchange_items", command_id="abc123",
                before={"1": {"skus": ["OLD"]}}, after={"1": {"skus": ["NEW"]}},
                result={"succeeded": [{"o_id": "1"}]},
                reconciliation={"status": "ok"},
            )
            folder = Path(bundle["dir"])
            self.assertTrue((folder / "before.json").is_file())
            self.assertTrue((folder / "after.json").is_file())
            self.assertTrue((folder / "reconciliation.json").is_file())
            self.assertTrue((folder / "request-summary.json").is_file())


class RuntimeReadbackTests(unittest.TestCase):
    def _runtime(self, page, evidence_dir=""):
        class Session:
            def __init__(self):
                self.page = page

            def login_if_needed(self, **kwargs):
                return {"ok": True}

            def save_state(self):
                return None

            def close(self):
                return None

        return DigitalRuntime({
            "enabled": True, "workerId": "erp-ai-procurement",
            "baseUrl": "https://www.erp321.com/epaas",
            "orderListUrl": "https://www.erp321.com/app/order/order/list.aspx",
            "headless": True, "writeDelayMs": 250,
            "storageStatePath": "unused.json",
            "evidenceDir": evidence_dir,
        }, session=Session())

    def _plan(self, oid="10001", source="OLD", target="NEW"):
        return {
            "confirm": True,
            "plans": [{
                "o_id": oid, "ok": True, "mode": "ChangeItem",
                "src_sku_id": source, "new_sku_id": target,
            }],
        }

    def test_readback_match_counts_as_success(self):
        page = FakePage(orders={"10001": {"items": [{"sku_id": "OLD", "qty": 1}]}})
        runtime = self._runtime(page)
        with tempfile.TemporaryDirectory() as tmp:
            runtime.config["evidenceDir"] = tmp
            result = runtime.run("erp.exchange_items", self._plan())
            self.assertEqual(1, len(result["succeeded"]))
            self.assertEqual("ok", result["reconciliation"]["status"])
            self.assertTrue(Path(result["evidence"]["dir"], "after.json").is_file())
        runtime.close()

    def test_already_exchanged_skips_write(self):
        page = FakePage(orders={"10001": {"items": [{"sku_id": "NEW", "qty": 1}]}})
        runtime = self._runtime(page)
        result = runtime.run("erp.exchange_items", self._plan())
        runtime.close()
        self.assertTrue(result["succeeded"][0].get("alreadyDone"))
        self.assertFalse(any("executeJob" in str(call) for call in page.calls))

    def test_mismatch_is_not_success(self):
        page = FakePage(orders={"10001": {"items": [{"sku_id": "OLD", "qty": 1}]}})
        original = page.evaluate

        def evaluate(expr, arg=None, **kwargs):
            result = original(expr, arg, **kwargs)
            if "executeJob" in str(expr):
                page.orders["10001"] = {"items": [{"sku_id": "OLD", "qty": 1}]}
            return result

        page.evaluate = evaluate
        runtime = self._runtime(page)
        result = runtime.run("erp.exchange_items", self._plan())
        runtime.close()
        self.assertEqual("reconciliation_failed", result["reconciliation"]["status"])
        self.assertEqual([], result["succeeded"])
        self.assertEqual("10001", result["failed"][0]["o_id"])

    def test_unread_before_write_is_hard_error(self):
        page = FakePage()
        runtime = self._runtime(page)
        with self.assertRaisesRegex(ErpError, "写入前无法回读"):
            runtime.run("erp.exchange_items", self._plan())
        runtime.close()
        self.assertFalse(any("executeJob" in str(call) for call in page.calls))

    def test_unread_after_write_is_unknown(self):
        page = FakePage(orders={"10001": {"items": [{"sku_id": "OLD", "qty": 1}]}})
        original = page.evaluate
        loads = {"n": 0}

        def evaluate(expr, arg=None, **kwargs):
            if "loadOrders" in str(expr) or "loadOrder" in str(expr):
                loads["n"] += 1
                if loads["n"] > 1:
                    if "loadOrders" in str(expr):
                        oids = list((arg or {}).get("oids") or ["10001"])
                        return {
                            "orders": [
                                {"o_id": str(oid), "items": [], "load_error": "timeout"}
                                for oid in oids
                            ],
                        }
                    return {"o_id": "10001", "items": [], "load_error": "timeout"}
            return original(expr, arg, **kwargs)

        page.evaluate = evaluate
        runtime = self._runtime(page)
        with self.assertRaises(ErpUnknownResult):
            runtime.run("erp.exchange_items", self._plan())
        runtime.close()


if __name__ == "__main__":
    unittest.main()
