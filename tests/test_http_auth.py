# -*- coding: utf-8 -*-
"""HTTP 鉴权矩阵：临时端口上的 ThreadingHTTPServer，不连钉钉、不发催办、不连镜像库。"""
import json
import secrets
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import PropertyMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import backend.app as app_mod


AGENT_TOKEN = "p18-agent-token"
EXCHANGE_TOKEN = "p18-exchange-token"
WORKER_TOKEN = "p18-worker-token"
_real_setting = app_mod.setting


def _setting(name, default=""):
    overrides = {
        "AGENT_API_TOKEN": AGENT_TOKEN,
        "EXCHANGE_API_TOKEN": EXCHANGE_TOKEN,
        "EXCHANGE_WORKER_TOKEN": WORKER_TOKEN,
    }
    if name in overrides:
        return overrides[name]
    return _real_setting(name, default)


class HttpAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patch = patch("backend.app.setting", _setting)
        cls._patch.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_mod.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._patch.stop()

    def issue_web_session(self, buyer="测网页", role="operator"):
        sender_id = "u-web-" + secrets.token_hex(4)
        unique = f"{buyer}-{sender_id[-4:]}"
        app_mod.STAFF_DIRECTORY.upsert(unique, dingtalk_user_id=sender_id, role=role)
        issued = app_mod.WEB_AUTH.issue_code(sender_id=sender_id, buyer_name=unique)
        session = app_mod.WEB_AUTH.consume_code(
            operator=unique, code=issued["code"], directory=app_mod.STAFF_DIRECTORY,
        )
        return {**session, "operator": unique, "senderId": sender_id}

    def request(self, path, *, method="GET", token=None, body=None, headers=None):
        header = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            header["Content-Type"] = "application/json"
            header["Content-Length"] = str(len(data))
        if token is not None:
            header["Authorization"] = f"Bearer {token}"
        request = Request(self.base + path, data=data, headers=header, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return response.status, payload
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw[:200]}
            return exc.code, payload

    def test_workbench_requires_bearer_and_web_session(self):
        self.assertEqual(401, self.request("/api/agent/workbench")[0])
        status, payload = self.request("/api/agent/workbench?operator=lite", token=AGENT_TOKEN)
        self.assertEqual(401, status)
        self.assertIn("绑定网页", payload.get("error", ""))
        session = self.issue_web_session()
        status, payload = self.request(
            "/api/agent/workbench", token=AGENT_TOKEN,
            headers={"X-Agent-Web-Token": session["webToken"]},
        )
        self.assertEqual(200, status)
        self.assertIn("items", payload)
        self.assertIn("outbox", payload)
        session_only = self.issue_web_session()
        status, payload = self.request(
            "/api/agent/workbench",
            headers={"X-Agent-Web-Token": session_only["webToken"]},
        )
        self.assertEqual(200, status)

    def test_agent_status_requires_bearer(self):
        status, payload = self.request("/api/agent/status")
        self.assertEqual(401, status)
        self.assertFalse(payload.get("ok", True))
        status, _ = self.request("/api/agent/status", token="wrong")
        self.assertEqual(401, status)
        status, payload = self.request("/api/agent/status", token=AGENT_TOKEN)
        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"))
        self.assertIn("quality", payload)
        self.assertIn("dropship", payload)
        self.assertIn("jobs", payload)
        self.assertIn("outbox", payload)

    def test_agent_status_accepts_web_session_without_shared_token(self):
        session = self.issue_web_session()
        status, payload = self.request(
            "/api/agent/status",
            headers={"X-Agent-Web-Token": session["webToken"]},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"))

    def test_web_bind_does_not_need_shared_token(self):
        sender_id = "u-web-bind"
        app_mod.STAFF_DIRECTORY.upsert("测绑定", dingtalk_user_id=sender_id, role="operator")
        issued = app_mod.WEB_AUTH.issue_code(sender_id=sender_id, buyer_name="测绑定")
        status, payload = self.request(
            "/api/agent/web-bind", method="POST",
            body={"operator": "测绑定", "code": issued["code"]},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload.get("webToken"))

    def test_login_does_not_need_shared_token(self):
        sender_id = "u-web-login"
        app_mod.STAFF_DIRECTORY.upsert("测登录", dingtalk_user_id=sender_id, role="operator")
        issued = app_mod.WEB_AUTH.issue_account(sender_id=sender_id, buyer_name="测登录")
        status, payload = self.request(
            "/api/agent/login", method="POST",
            body={"username": "测登录", "password": issued["password"]},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload.get("webToken"))
        me_status, me = self.request(
            "/api/agent/me",
            headers={"X-Agent-Web-Token": payload["webToken"]},
        )
        self.assertEqual(200, me_status)
        self.assertEqual("测登录", me.get("operator"))

    def test_quality_decide_requires_bearer(self):
        self.assertEqual(401, self.request("/api/agent/quality/abcdef/resolve", method="POST", body={})[0])

    def test_forecast_status_uses_agent_token(self):
        self.assertEqual(401, self.request("/api/forecast/status")[0])
        status, payload = self.request("/api/forecast/status", token=AGENT_TOKEN)
        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"))

    def test_exchange_page_and_worker_tokens_are_distinct(self):
        self.assertEqual(401, self.request("/api/exchange/status")[0])
        self.assertEqual(401, self.request("/api/exchange/status", token=WORKER_TOKEN)[0])
        status, _ = self.request("/api/exchange/status", token=EXCHANGE_TOKEN)
        self.assertEqual(200, status)
        self.assertEqual(401, self.request("/api/exchange/worker/jobs/next", token=EXCHANGE_TOKEN)[0])
        status, payload = self.request(
            "/api/exchange/worker/jobs/next?worker_id=w1", token=WORKER_TOKEN,
        )
        self.assertEqual(200, status)
        self.assertIn("job", payload)
        self.assertIsNone(payload.get("job"))
        self.assertEqual("backend", payload.get("executor"))
        for path, key in (
            ("/api/exchange/worker/searches/next?worker_id=w1", "search"),
            ("/api/exchange/worker/probes/next?worker_id=w1", "probe"),
            ("/api/exchange/worker/images/next?worker_id=w1", "job"),
        ):
            status, payload = self.request(path, token=WORKER_TOKEN)
            self.assertEqual(200, status)
            self.assertIsNone(payload.get(key))
            self.assertEqual("backend", payload.get("executor"))

    def test_chat_requires_web_session(self):
        status, payload = self.request(
            "/api/agent/chat", method="POST", token=AGENT_TOKEN,
            body={"message": "ping", "operator": "张三"},
        )
        self.assertEqual(401, status)
        self.assertIn("绑定网页", payload.get("error", ""))

    def test_chat_returns_503_when_agent_unavailable(self):
        session = self.issue_web_session()
        with patch.object(type(app_mod.AGENT), "available", PropertyMock(return_value=False)):
            status, payload = self.request(
                "/api/agent/chat", method="POST", token=AGENT_TOKEN,
                body={"message": "ping", "operator": session["operator"]},
                headers={"X-Agent-Web-Token": session["webToken"]},
            )
        self.assertEqual(503, status)
        self.assertFalse(payload.get("ok", True))

    def test_web_bind_and_operator_mismatch(self):
        session = self.issue_web_session()
        status, payload = self.request(
            "/api/agent/chat", method="POST", token=AGENT_TOKEN,
            body={"message": "ping", "operator": "韩立"},
            headers={"X-Agent-Web-Token": session["webToken"]},
        )
        self.assertEqual(403, status)
        self.assertIn("不一致", payload.get("error", ""))

    def test_staff_post_requires_admin_web_session(self):
        session = self.issue_web_session()
        status, payload = self.request(
            "/api/agent/staff", method="POST", token=AGENT_TOKEN,
            body={"buyerName": "路人", "dingtalkUserId": "u-x"},
            headers={"X-Agent-Web-Token": session["webToken"]},
        )
        self.assertEqual(403, status)
        self.assertIn("管理员", payload.get("error", ""))

    def test_missing_agent_token_config_returns_503(self):
        def empty_agent(name, default=""):
            if name == "AGENT_API_TOKEN":
                return ""
            return _setting(name, default)

        with patch("backend.app.setting", empty_agent):
            status, payload = self.request("/api/agent/status", token=AGENT_TOKEN)
        self.assertEqual(503, status)
        self.assertIn("AGENT_API_TOKEN", payload.get("error", ""))

    def test_health_redacts_erp_secrets(self):
        fake = {
            "enabled": False,
            "username": "secret-erp-user",
            "storageStatePath": "C:/secrets/erp-ai-state.json",
            "loginFields": {"account": "#login-account"},
            "hasPassword": True,
            "workerId": "erp-ai-procurement",
        }
        class _FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self):
                return self

            def execute(self, *args, **kwargs):
                return self

            def fetchone(self):
                return (1,)

        with patch.object(app_mod.DIGITAL_WORKER, "status", return_value=fake), \
                patch("backend.app.connect", return_value=_FakeConn()), \
                patch("backend.app.fetch_realtime_sync_state", return_value={
                    "syncedAt": "", "syncLagMinutes": None,
                }):
            status, payload = self.request("/api/health")
        self.assertIn(status, {200, 503})
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-erp-user", blob)
        self.assertNotIn("erp-ai-state.json", blob)
        self.assertNotIn("loginFields", blob)
        self.assertNotIn("storageStatePath", blob)
        erp = payload.get("erpWorker") or {}
        self.assertTrue(erp.get("hasUsername"))
        self.assertTrue(erp.get("hasPassword"))

    def test_now_requires_web_login(self):
        status, payload = self.request("/api/now")
        self.assertEqual(401, status)
        self.assertIn("绑定网页", payload.get("error", ""))
        session = self.issue_web_session()
        status, payload = self.request(
            "/api/now", headers={"X-Agent-Web-Token": session["webToken"]},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"))
        self.assertRegex(str(payload.get("now") or ""), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual("Asia/Shanghai", payload.get("tz"))

    def test_dashboard_and_contracts_require_web_login(self):
        with patch.object(app_mod, "source_cache", return_value={"dashboard": {"meta": {"rows": 0}}}):
            status, payload = self.request("/api/dashboard")
        self.assertEqual(401, status)
        self.assertIn("绑定网页", payload.get("error", ""))
        session = self.issue_web_session()
        with patch.object(app_mod, "source_cache", return_value={"dashboard": {"meta": {"rows": 0}}}):
            status, _ = self.request(
                "/api/dashboard",
                headers={"X-Agent-Web-Token": session["webToken"]},
            )
        self.assertEqual(200, status)
        with patch("backend.app.fetch_contract_order_choices", return_value=[]):
            status, payload = self.request(
                "/api/contracts/orders",
                headers={"X-Agent-Web-Token": session["webToken"]},
            )
        self.assertEqual(200, status)
        self.assertEqual([], payload.get("orders"))
        self.assertEqual(401, self.request("/api/dashboard", token=AGENT_TOKEN)[0])


if __name__ == "__main__":
    unittest.main()
