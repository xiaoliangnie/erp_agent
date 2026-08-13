# -*- coding: utf-8 -*-
"""钉钉通道离线用例：身份绑定、消息处理、确认关键字。不连钉钉开放平台。"""
import os
import tempfile
import unittest
from pathlib import Path

from backend.agent.store import AgentStore
from backend.dingtalk.identity import StaffDirectory
from backend.dingtalk.stream import DingTalkStreamChannel


class FakeAudit:
    def __init__(self):
        self.keys = []

    def record_delivery(self, **kwargs):
        key = kwargs.get("idempotency_key") or ""
        if key in self.keys:
            return False
        if key:
            self.keys.append(key)
        return True


class FakeRunner:
    def __init__(self):
        self.chats = []
        self.confirms = []

    def chat(self, *, message, session_key, operator, channel):
        self.chats.append({"message": message, "operator": operator, "channel": channel,
                           "session_key": session_key})
        return {"reply": f"收到 {operator}：{message}", "pendingActions": [], "steps": []}

    def confirm(self, action_id, operator, channel="dingtalk"):
        self.confirms.append({"id": action_id, "operator": operator, "channel": channel})
        return {"title": "生成合同", "result": {"file": "ok.xlsx"}}

    def cancel(self, action_id, operator):
        return {"title": "生成合同", "status": "cancelled"}


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tmp.name) / "agent.sqlite3")
        self.directory = StaffDirectory(self.store)
        self.runner = FakeRunner()
        self.audit = FakeAudit()
        self.channel = DingTalkStreamChannel(
            runner=self.runner, sender=None, client_id="app", client_secret="secret",
            audit=self.audit, enabled=True, directory=self.directory,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, text, *, sender_id="u1", sender_name="小钉", message_id="m1"):
        return self.channel.handle(
            text=text, message_id=message_id, conversation_id="cid",
            sender_id=sender_id, sender_name=sender_name,
        )

    def test_help_and_bind_then_chat_uses_buyer_name(self):
        help_text = self.handle("帮助", message_id="h1")
        self.assertIn("绑定", help_text)
        bound = self.handle("绑定 张三", message_id="b1")
        self.assertIn("张三", bound)
        self.assertEqual("张三", self.directory.get_by_dingtalk_user_id("u1")["buyerName"])
        reply = self.handle("今年逾期多少", message_id="c1")
        self.assertIn("张三", reply)
        self.assertEqual("张三", self.runner.chats[-1]["operator"])
        self.assertNotIn("还没绑定", reply)

    def test_unbound_chat_nags_once(self):
        reply = self.handle("查单", message_id="q1")
        self.assertIn("还没绑定", reply)
        self.assertEqual("小钉", self.runner.chats[-1]["operator"])

    def test_confirm_keyword_uses_bound_operator(self):
        self.directory.upsert("李四", dingtalk_user_id="u9")
        reply = self.handle("确认 abcdef123456", sender_id="u9", sender_name="别名", message_id="cf1")
        self.assertIn("已执行", reply)
        self.assertEqual("李四", self.runner.confirms[-1]["operator"])

    def test_duplicate_message_id_is_ignored(self):
        first = self.handle("帮助", message_id="dup")
        second = self.handle("帮助", message_id="dup")
        self.assertTrue(first)
        self.assertEqual("", second)

    def test_seed_json(self):
        path = Path(self.tmp.name) / "staff.json"
        path.write_text('{"王五": {"dingtalk_user_id": "u5", "mobile": "139"}}', encoding="utf-8")
        self.assertEqual(1, self.directory.seed_from_json(path))
        self.assertEqual("u5", self.directory.get("王五")["dingtalkUserId"])

    def test_bind_multiple_names_share_user_id(self):
        reply = self.handle("绑定 利特、李佳冬（利特）", message_id="b-alias")
        self.assertIn("利特", reply)
        self.assertIn("李佳冬（利特）", reply)
        self.assertEqual("u1", self.directory.get("利特")["dingtalkUserId"])
        self.assertEqual("u1", self.directory.get("李佳冬（利特）")["dingtalkUserId"])


class BuyerNameAliasTests(unittest.TestCase):
    def test_split_and_equivalent(self):
        from backend.staff_names import buyer_names_equivalent, parse_buyer_names, split_buyer_name
        self.assertEqual(("李佳冬", "利特"), split_buyer_name("李佳冬（利特）"))
        self.assertEqual(("李迎", "刃海"), split_buyer_name("李迎(刃海)"))
        self.assertEqual(("洪静茹", "静静"), split_buyer_name("洪静茹(静静）"))
        self.assertTrue(buyer_names_equivalent("利特", "李佳冬（利特）"))
        self.assertTrue(buyer_names_equivalent("李佳冬（利特）", "利特"))
        self.assertTrue(buyer_names_equivalent("刃海", "李迎(刃海)"))
        self.assertFalse(buyer_names_equivalent("利特", "韩立"))
        self.assertEqual(["利特", "李佳冬（利特）"], parse_buyer_names("利特、李佳冬（利特）"))

    def test_resolve_hits_parenthetical_alias(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = StaffDirectory(AgentStore(Path(tmp.name) / "a.sqlite3"))
        directory.upsert("利特", dingtalk_user_id="u-lite")
        resolved = directory.resolve(["李佳冬（利特）", "韩立", "利特"])
        self.assertEqual(["李佳冬（利特）", "利特"], resolved["matched"])
        self.assertEqual(["韩立"], resolved["unbound"])
        self.assertEqual(["u-lite"], resolved["userIds"])

    def test_seed_json_aliases(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = StaffDirectory(AgentStore(Path(tmp.name) / "a.sqlite3"))
        path = Path(tmp.name) / "staff.json"
        path.write_text(
            '{"利特": {"dingtalk_user_id": "u8", "aliases": ["李佳冬（利特）"]}}',
            encoding="utf-8",
        )
        self.assertEqual(1, directory.seed_from_json(path))
        self.assertEqual("u8", directory.get("利特")["dingtalkUserId"])
        self.assertEqual("u8", directory.get("李佳冬（利特）")["dingtalkUserId"])


class StreamSslTests(unittest.TestCase):
    def test_patch_stream_ssl_sets_cafile(self):
        from backend.dingtalk.stream import patch_stream_ssl
        previous = os.environ.get("SSL_CERT_FILE")
        os.environ.pop("SSL_CERT_FILE", None)
        try:
            cafile = patch_stream_ssl()
            self.assertTrue(cafile)
            self.assertTrue(Path(cafile).is_file())
            self.assertEqual(cafile, os.environ.get("SSL_CERT_FILE"))
        finally:
            if previous is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous

    def test_sender_ssl_context_loads(self):
        from backend.dingtalk.sender import _ssl_context
        ctx = _ssl_context()
        self.assertTrue(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
