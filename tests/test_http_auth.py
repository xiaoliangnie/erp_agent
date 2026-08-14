# -*- coding: utf-8 -*-
"""HTTP 鉴权矩阵：临时端口上的 ThreadingHTTPServer，不连钉钉、不发催办、不连镜像库。"""
import json
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

    def test_agent_status_requires_bearer(self):
        status, payload = self.request("/api/agent/status")
        self.assertEqual(401, status)
        self.assertFalse(payload.get("ok", True))
        status, _ = self.request("/api/agent/status", token="wrong")
        self.assertEqual(401, status)
        status, payload = self.request("/api/agent/status", token=AGENT_TOKEN)
        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"))

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

    def test_chat_returns_503_when_agent_unavailable(self):
        with patch.object(type(app_mod.AGENT), "available", PropertyMock(return_value=False)):
            status, payload = self.request(
                "/api/agent/chat", method="POST", token=AGENT_TOKEN,
                body={"message": "ping", "operator": "张三"},
            )
        self.assertEqual(503, status)
        self.assertFalse(payload.get("ok", True))

    def test_missing_agent_token_config_returns_503(self):
        def empty_agent(name, default=""):
            if name == "AGENT_API_TOKEN":
                return ""
            return _setting(name, default)

        with patch("backend.app.setting", empty_agent):
            status, payload = self.request("/api/agent/status", token=AGENT_TOKEN)
        self.assertEqual(503, status)
        self.assertIn("AGENT_API_TOKEN", payload.get("error", ""))

    def test_dashboard_and_contracts_have_no_bearer(self):
        with patch("backend.app.payloads", return_value=({"meta": {"rows": 0}}, {"meta": {"rows": 0}})):
            status, _ = self.request("/api/dashboard")
        self.assertEqual(200, status)
        with patch("backend.app.fetch_contract_order_choices", return_value=[]):
            status, payload = self.request("/api/contracts/orders")
        self.assertEqual(200, status)
        self.assertEqual([], payload.get("orders"))


if __name__ == "__main__":
    unittest.main()
