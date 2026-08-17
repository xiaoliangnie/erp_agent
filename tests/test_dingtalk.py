# -*- coding: utf-8 -*-
"""钉钉通道离线用例：身份绑定、消息处理、确认关键字。不连钉钉开放平台。"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agent.store import AgentStore
from backend.dingtalk.identity import StaffDirectory
from backend.dingtalk.sender import DingTalkError
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

    def chat(self, *, message, session_key, operator, channel, actor_id=""):
        self.chats.append({"message": message, "operator": operator, "channel": channel,
                           "session_key": session_key})
        return {"reply": f"收到 {operator}：{message}", "pendingActions": [], "steps": []}

    def confirm(self, action_id, operator, channel="dingtalk", **kwargs):
        self.confirms.append({"id": action_id, "operator": operator, "channel": channel, **kwargs})
        return {"title": "生成合同", "result": {"file": "ok.xlsx"}}

    def confirm_latest(self, operator, channel="dingtalk", **kwargs):
        return self.confirm("latest", operator, channel=channel, **kwargs)

    def cancel(self, action_id, operator, **kwargs):
        return {"title": "生成合同", "status": "cancelled"}

    def cancel_latest(self, operator, **kwargs):
        return self.cancel("latest", operator, **kwargs)


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
        self.assertIn("换鞋垫", help_text)
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

    def test_handle_async_leaves_the_event_loop(self):
        import asyncio
        import threading

        seen = {}
        original = self.channel.handle

        def wrapped(**kwargs):
            seen["handle_thread"] = threading.current_thread().name
            return original(**kwargs)

        self.channel.handle = wrapped

        async def go():
            seen["loop_thread"] = threading.current_thread().name
            return await self.channel.handle_async(
                text="帮助", message_id="async-1", conversation_id="cid",
                sender_id="u1", sender_name="小钉",
            )

        reply = asyncio.run(go())
        self.assertIn("绑定", reply)
        self.assertNotEqual(seen["loop_thread"], seen["handle_thread"])

    def test_bare_confirm_uses_latest_pending(self):
        self.directory.upsert("李四", dingtalk_user_id="u9")
        reply = self.handle("确认", sender_id="u9", sender_name="别名", message_id="cf0")
        self.assertIn("已执行", reply)
        self.assertEqual("latest", self.runner.confirms[-1]["id"])
        self.assertEqual("李四", self.runner.confirms[-1]["operator"])

    def test_confirm_with_trailing_note_still_confirms(self):
        self.directory.upsert("李四", dingtalk_user_id="u9")
        reply = self.handle("确认，并返回处理结果", sender_id="u9", sender_name="别名", message_id="cf2")
        self.assertIn("已执行", reply)
        self.assertEqual("latest", self.runner.confirms[-1]["id"])

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
        self.assertEqual("operator", self.directory.get("王五")["role"])

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
        self.assertFalse(buyer_names_equivalent("利特", "李佳冬（利特）"))
        self.assertFalse(buyer_names_equivalent("李佳冬（利特）", "利特"))
        self.assertTrue(buyer_names_equivalent("利特", "李佳冬（利特）", include_nick=True))
        self.assertTrue(buyer_names_equivalent("李佳冬", "李佳冬（利特）"))
        self.assertFalse(buyer_names_equivalent("刃海", "李迎(刃海)"))
        self.assertTrue(buyer_names_equivalent("刃海", "李迎(刃海)", include_nick=True))
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

    def test_known_operator_matches_alias_and_fails_closed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = StaffDirectory(AgentStore(Path(tmp.name) / "a.sqlite3"))
        self.assertFalse(directory.known_operator("利特"))
        directory.upsert("利特", dingtalk_user_id="u8")
        self.assertTrue(directory.known_operator("利特"))
        self.assertTrue(directory.known_operator("李佳冬（利特）"))
        self.assertFalse(directory.known_operator("韩立"))
        self.assertFalse(directory.known_operator(""))


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


class StreamSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.channel = DingTalkStreamChannel(
            runner=FakeRunner(), sender=None, client_id="app", client_secret="secret",
            audit=FakeAudit(), enabled=True,
            initial_backoff_seconds=0.05, max_backoff_seconds=0.1,
        )

    def tearDown(self):
        self.channel.stop()

    def test_disabled_channel_does_not_start(self):
        channel = DingTalkStreamChannel(
            runner=FakeRunner(), sender=None, client_id="app", client_secret="secret",
            audit=FakeAudit(), enabled=False,
        )
        status = channel.start()
        self.assertFalse(status["running"])
        self.assertEqual(0, status["restartCount"])

    def test_supervise_restarts_after_serve_crash_and_stop_joins(self):
        started = threading.Event()
        crashes = {"n": 0}

        def fake_serve():
            crashes["n"] += 1
            if crashes["n"] == 1:
                raise RuntimeError("boom")
            started.set()
            self.channel._stop.wait(5)

        with patch("backend.dingtalk.stream.sdk_available", return_value=True):
            with patch.object(self.channel, "_serve", fake_serve):
                status = self.channel.start()
                self.assertTrue(status["running"])
                self.assertTrue(started.wait(timeout=2))
                self.assertGreaterEqual(self.channel.restart_count, 1)
                self.assertIn("boom", self.channel.last_error)
                self.assertGreaterEqual(self.channel.status()["restartCount"], 1)
                self.channel.stop()
                self.assertFalse(self.channel.status()["running"])

    def test_stop_is_safe_before_start(self):
        self.channel.stop()
        self.assertFalse(self.channel.status()["running"])


class FakeSender:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail
        self.configured = True

    def send_markdown(self, title, text, **kwargs):
        self.calls.append({"title": title, "text": text, **kwargs})
        if self.fail:
            raise DingTalkError("钉钉接口连接失败：simulated")
        return {"channel": "webhook"}


def _overdue_row():
    return {
        "采购单号": "604264", "采购日期": "2026-07-01", "状态": "已确认",
        "采购员": "张三", "item_supplier_id": "佰特", "仓储方": "主仓",
        "商品编码": "SKU-1", "数量": 10, "item_in_qty": 0,
        "item_delivery_date": "2026-08-01", "最早预计到货日期": "",
        "基本售价": 20, "基本金额": 200,
    }


class ReminderIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tmp.name) / "agent.sqlite3")
        from backend.agent.audit import AuditLog
        self.audit = AuditLog(self.store)
        self.directory = StaffDirectory(self.store)
        self.sender = FakeSender()
        from backend.dingtalk.reminders import ReminderNotifier, DailyReminderScheduler
        self.notifier = ReminderNotifier(sender=self.sender, directory=self.directory, audit=self.audit)
        self.scheduler = DailyReminderScheduler(
            notifier=self.notifier,
            fetch_rows=lambda year: ([_overdue_row()], {}),
            send_time="08:30",
            retry_interval_seconds=0,
            max_attempts_per_day=3,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_is_idempotent_same_day(self):
        first = self.scheduler.run_once(today="2026-08-13")
        second = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(first.get("sent"))
        self.assertTrue(second.get("skipped"))
        self.assertEqual("同一批催办已经推送过", second["reason"])
        self.assertEqual(1, len(self.sender.calls))
        key = "daily-reminder-2026-08-13"
        self.assertTrue(self.audit.has_successful_delivery(key))
        sent = self.audit.list_deliveries(key_prefix=key, status="sent")
        self.assertEqual(1, len([row for row in sent if row["idempotencyKey"] == key]))

    def test_failure_can_retry_then_succeed(self):
        self.sender.fail = True
        with self.assertRaisesRegex(DingTalkError, "simulated"):
            self.scheduler.run_once(today="2026-08-13")
        key = "daily-reminder-2026-08-13"
        self.assertFalse(self.audit.has_successful_delivery(key))
        attempts = self.audit.list_deliveries(key_prefix=f"{key}-attempt-", status="failed")
        self.assertEqual(1, len(attempts))
        self.assertEqual(f"{key}-attempt-1", attempts[0]["idempotencyKey"])
        self.sender.fail = False
        result = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(result.get("sent"))
        self.assertEqual(2, len(self.sender.calls))
        self.assertTrue(self.audit.has_successful_delivery(key))

    def test_scheduler_caps_retries_after_three_failures(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.sender.fail = True
        now = datetime(2026, 8, 13, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        for _ in range(3):
            tick = self.scheduler.tick(now=now)
            self.assertTrue(tick.get("failed"))
        blocked = self.scheduler.tick(now=now)
        self.assertTrue(blocked.get("skipped"))
        self.assertIn("已失败 3 次", blocked["reason"])
        self.assertEqual(3, len(self.sender.calls))
        attempts = self.audit.list_deliveries(
            key_prefix="daily-reminder-2026-08-13-attempt-", status="failed",
        )
        self.assertEqual(3, len(attempts))
        self.assertIn("simulated", self.scheduler.last_error)
        self.sender.fail = False
        manual = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(manual.get("sent"))
        self.assertEqual(4, len(self.sender.calls))

    def test_scheduler_waits_retry_interval(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.scheduler.retry_interval_seconds = 15 * 60
        self.sender.fail = True
        now = datetime(2026, 8, 13, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        first = self.scheduler.tick(now=now)
        self.assertTrue(first.get("failed"))
        second = self.scheduler.tick(now=now)
        self.assertTrue(second.get("skipped"))
        self.assertIn("重试间隔", second["reason"])
        self.assertEqual(1, len(self.sender.calls))

    def test_stuck_sending_key_does_not_block_retry(self):
        from backend.agent.store import now
        key = "daily-reminder-2026-08-13"
        with self.store.write(immediate=True) as conn:
            conn.execute(
                """INSERT INTO notification_deliveries
                   (channel, target, kind, idempotency_key, status, detail_json, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("dingtalk", "group", "delivery_reminder", key, "sending", "{}", None, now()),
            )
        result = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(result.get("sent"))
        self.assertTrue(self.audit.has_successful_delivery(key))

    def test_buyer_filtered_push_uses_separate_idempotency_key(self):
        full = self.scheduler.run_once(today="2026-08-13")
        filtered = self.scheduler.run_once(today="2026-08-13", buyer="张三", operator="web")
        self.assertTrue(full.get("sent"))
        self.assertTrue(filtered.get("sent"))
        self.assertEqual(2, len(self.sender.calls))
        self.assertTrue(self.audit.has_successful_delivery("daily-reminder-2026-08-13"))
        self.assertTrue(self.audit.has_successful_delivery("daily-reminder-2026-08-13-web-张三"))
        again = self.scheduler.run_once(today="2026-08-13", buyer="张三", operator="web")
        self.assertTrue(again.get("skipped"))
        self.assertEqual("同一批催办已经推送过", again["reason"])
        self.assertEqual(2, len(self.sender.calls))

    def test_tick_records_retry_decision_errors(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        def boom(*_args, **_kwargs):
            raise RuntimeError("sqlite down")

        self.scheduler._retry_decision = boom
        now = datetime(2026, 8, 13, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        result = self.scheduler.tick(now=now)
        self.assertTrue(result.get("failed"))
        self.assertIn("sqlite down", self.scheduler.last_error)

    def test_loop_survives_tick_errors(self):
        import time

        def boom(**_kwargs):
            raise RuntimeError("sqlite down")

        self.scheduler.tick = boom
        self.scheduler.poll_seconds = 0.05
        self.scheduler.start()
        self.assertTrue(self.scheduler.status()["enabled"])
        deadline = time.time() + 2
        while time.time() < deadline and not self.scheduler.last_error:
            time.sleep(0.05)
        try:
            self.assertTrue(self.scheduler.status()["running"])
            self.assertIn("RuntimeError", self.scheduler.last_error)
        finally:
            self.scheduler.stop()


if __name__ == "__main__":
    unittest.main()
