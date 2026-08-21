# -*- coding: utf-8 -*-
"""钉钉通道离线用例：身份绑定、消息处理、确认关键字。不连钉钉开放平台。"""
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agent.store import AgentStore
from backend.dingtalk.identity import StaffDirectory
from backend.dingtalk.sender import DingTalkError
from backend.dingtalk.stream import (
    DingTalkStreamChannel, inbound_progress_text,
    PROGRESS_INSOLE_QUERY, PROGRESS_QUERY,
)


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


class RecordingOtoSender:
    app_ready = True
    webhook_ready = False

    def __init__(self):
        self.sent = []

    def send_oto_markdown(self, title, text, *, user_ids=()):
        self.sent.append({"title": title, "text": text, "userIds": list(user_ids)})
        return {"channel": "oto", "atUserIds": list(user_ids)}

    def reply_text(self, *, conversation_id, text, at_user_ids=()):
        self.sent.append({
            "title": "group", "text": text, "userIds": list(at_user_ids),
            "conversationId": conversation_id,
        })
        return {"channel": "group", "atUserIds": list(at_user_ids)}


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

    def handle(self, text, *, sender_id="u1", sender_name="小钉", message_id="m1",
               conversation_type="2"):
        return self.channel.handle(
            text=text, message_id=message_id, conversation_id="cid",
            sender_id=sender_id, sender_name=sender_name,
            conversation_type=conversation_type,
        )

    def test_help_and_bind_then_chat_uses_buyer_name(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        help_text = self.handle("帮助", message_id="h1")
        self.assertIn("我是采购助手", help_text)
        self.assertIn("功能使用说明", help_text)
        self.assertIn("绑定身份", help_text)
        self.assertIn("抖音鞋垫更换操作", help_text)
        self.assertIn("管理员同意", help_text)
        mentioned = self.handle("@采购助手", message_id="h-at")
        self.assertEqual(help_text, mentioned)
        requested = self.handle("绑定 张三", message_id="b1")
        self.assertIn("已提交绑定申请", requested)
        self.assertIn("张三", requested)
        self.assertEqual({}, self.directory.get_by_dingtalk_user_id("u1"))
        approved = self.handle(
            "确认", sender_id="u-admin", sender_name="韩立",
            message_id="b1-ok", conversation_type="1",
        )
        self.assertIn("已同意", approved)
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

    def test_reply_later_does_not_block_ack_path(self):
        import asyncio
        from types import SimpleNamespace

        replies = []

        class Handler:
            def reply_text(self, reply, message):
                replies.append((reply, getattr(message, "message_id", "")))

        message = SimpleNamespace(
            text=SimpleNamespace(content="帮助"),
            message_id="async-ack",
            conversation_id="cid",
            sender_staff_id="u1",
            sender_id="u1",
            sender_nick="小钉",
            conversation_type="2",
        )
        asyncio.run(self.channel._reply_later(Handler(), message, {"msgtype": "text"}))
        self.assertTrue(replies)
        self.assertIn("绑定", replies[0][0])

    def test_inbound_progress_skips_fast_commands(self):
        self.assertEqual("", inbound_progress_text("帮助"))
        self.assertEqual("", inbound_progress_text("确认"))
        self.assertEqual("", inbound_progress_text("绑定 利特"))
        self.assertEqual("", inbound_progress_text("新话题"))
        self.assertEqual("", inbound_progress_text("查单", conversation_type="1"))
        self.assertEqual(PROGRESS_INSOLE_QUERY, inbound_progress_text(
            "查询一下现在抖音需要更换的鞋垫订单", conversation_type="2",
        ))
        self.assertEqual(PROGRESS_QUERY, inbound_progress_text(
            "604264 到货了吗", conversation_type="2",
        ))

    def test_reply_later_sends_progress_before_answer(self):
        import asyncio
        from types import SimpleNamespace

        replies = []

        class Handler:
            def reply_text(self, reply, message):
                replies.append(reply)

        message = SimpleNamespace(
            text=SimpleNamespace(content="604264 到货了吗"),
            message_id="async-progress",
            conversation_id="cid",
            sender_staff_id="u1",
            sender_id="u1",
            sender_nick="小钉",
            conversation_type="2",
        )
        asyncio.run(self.channel._reply_later(Handler(), message, {"msgtype": "text"}))
        self.assertGreaterEqual(len(replies), 2)
        self.assertEqual(PROGRESS_QUERY, replies[0])

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
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        reply = self.handle("绑定 利特、李佳冬（利特）", message_id="b-alias")
        self.assertIn("已提交绑定申请", reply)
        self.assertEqual({}, self.directory.get("利特"))
        approved = self.handle(
            "同意绑定全部", sender_id="u-admin", sender_name="韩立",
            message_id="b-alias-ok", conversation_type="1",
        )
        self.assertIn("利特", approved)
        self.assertIn("李佳冬（利特）", approved)
        self.assertEqual("u1", self.directory.get("利特")["dingtalkUserId"])
        self.assertEqual("u1", self.directory.get("李佳冬（利特）")["dingtalkUserId"])

    def test_employee_private_chat_is_confirm_only(self):
        self.directory.upsert("李四", dingtalk_user_id="u9")
        refused = self.handle(
            "今年逾期多少", sender_id="u9", sender_name="李四",
            message_id="dm-chat", conversation_type="1",
        )
        self.assertIn("私聊只接收", refused)
        self.assertEqual([], self.runner.chats)
        bind_dm = self.handle(
            "绑定 李四", sender_id="u9", sender_name="李四",
            message_id="dm-bind", conversation_type="1",
        )
        self.assertIn("请到群里", bind_dm)
        self.assertEqual(0, len(self.channel.bind_requests.list_pending()))
        confirm = self.handle(
            "确认 abcdef123456", sender_id="u9", sender_name="别名",
            message_id="dm-cf", conversation_type="1",
        )
        self.assertIn("已执行", confirm)
        self.assertEqual("李四", self.runner.confirms[-1]["operator"])

    def test_admin_private_chat_keeps_full_access(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        reply = self.handle(
            "今年逾期多少", sender_id="u-admin", sender_name="韩立",
            message_id="adm-chat", conversation_type="1",
        )
        self.assertIn("韩立", reply)
        self.assertEqual("韩立", self.runner.chats[-1]["operator"])
        help_text = self.handle(
            "帮助", sender_id="u-admin", sender_name="韩立",
            message_id="adm-help", conversation_type="1",
        )
        self.assertIn("我是采购助手", help_text)
        self.assertIn("确认", help_text)
        self.assertIn("拒绝绑定", help_text)
        self.assertIn("功能使用说明", help_text)
        self.assertIn("抖音鞋垫更换操作", help_text)

    def test_spoofed_admin_name_cannot_approve(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        requested = self.handle("绑定 张三", sender_id="u1", sender_name="张三", message_id="sp-1")
        self.assertIn("已提交绑定申请", requested)
        spoof = self.handle(
            "确认", sender_id="attacker-id", sender_name="韩立",
            message_id="sp-ok", conversation_type="1",
        )
        self.assertNotIn("已同意", spoof)
        self.assertEqual({}, self.directory.get_by_dingtalk_user_id("u1"))

    def test_web_bind_group_sends_oto_code(self):
        sender = RecordingOtoSender()
        self.channel.sender = sender
        self.directory.upsert("张三", dingtalk_user_id="u1")
        reply = self.handle("绑定网页", sender_id="u1", sender_name="张三", message_id="web-1")
        self.assertIn("私信", reply)
        self.assertEqual(1, len(sender.sent))
        match = re.search(r"密码：(\S+)", sender.sent[0]["text"])
        self.assertIsNotNone(match)
        self.assertIn("花名：张三", sender.sent[0]["text"])
        self.assertNotIn(match.group(1), reply)

    def test_web_bind_unbound_and_employee_private(self):
        sender = RecordingOtoSender()
        self.channel.sender = sender
        unbound = self.handle("绑定网页", sender_id="u-new", sender_name="路人", message_id="web-u")
        self.assertIn("请先到群里发「绑定", unbound)
        self.assertEqual([], sender.sent)
        self.directory.upsert("张三", dingtalk_user_id="u1")
        private = self.handle(
            "绑定网页", sender_id="u1", sender_name="张三",
            message_id="web-p", conversation_type="1",
        )
        self.assertIn("群里", private)
        self.assertEqual([], sender.sent)

    def test_admin_reissues_web_accounts(self):
        sender = RecordingOtoSender()
        self.channel.sender = sender
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        self.directory.upsert("张三", dingtalk_user_id="u1")
        self.directory.upsert("利特", dingtalk_user_id="u-lite")
        self.directory.upsert("李佳冬（利特）", dingtalk_user_id="u-lite")
        reply = self.handle(
            "补发网页账号", sender_id="u-admin", sender_name="韩立",
            message_id="reissue-all",
        )
        self.assertIn("已私信 3 人", reply)
        self.assertNotIn("密码：", reply)
        self.assertEqual(3, len(sender.sent))
        for item in sender.sent:
            self.assertEqual("网页登录", item["title"])
            self.assertIn("【网页登录】", item["text"])
            match = re.search(r"密码：(\S+)", item["text"])
            self.assertIsNotNone(match)
            self.assertNotIn(match.group(1), reply)
        one = self.handle(
            "补发网页账号 张三", sender_id="u-admin", sender_name="韩立",
            message_id="reissue-one",
        )
        self.assertIn("张三", one)
        self.assertIn("其中 1 人是重置", one)
        self.assertEqual(4, len(sender.sent))
        missing = self.handle(
            "补发网页账号 路人", sender_id="u-admin", sender_name="韩立",
            message_id="reissue-miss",
        )
        self.assertIn("找不到", missing)
        employee = self.handle(
            "补发网页账号", sender_id="u1", sender_name="张三",
            message_id="reissue-emp",
        )
        self.assertNotIn("已私信", employee)

    def test_bind_approve_sends_web_login(self):
        sender = RecordingOtoSender()
        self.channel.sender = sender
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        self.handle("绑定 张三", sender_id="u1", sender_name="小钉", message_id="bw-1")
        approved = self.handle(
            "同意绑定", sender_id="u-admin", sender_name="韩立",
            message_id="bw-ok", conversation_type="1",
        )
        self.assertIn("已同意", approved)
        web = [item for item in sender.sent if item["title"] == "网页登录"]
        self.assertEqual(1, len(web))
        self.assertIn("管理员已同意绑定「张三」", web[0]["text"])
        self.assertIn("花名：张三", web[0]["text"])
        self.assertIn("密码：", web[0]["text"])
        match = re.search(r"密码：(\S+)", web[0]["text"])
        self.assertIsNotNone(match)
        self.assertNotIn(match.group(1), approved)

    def test_super_admin_can_set_and_cannot_demote_hanli(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        self.directory.upsert("利特", dingtalk_user_id="u-lite")
        reply = self.handle(
            "设置管理员 利特", sender_id="u-admin", sender_name="韩立",
            message_id="role-1", conversation_type="1",
        )
        self.assertIn("设为管理员", reply)
        self.assertEqual("admin", self.directory.get("利特")["role"])
        deny = self.handle(
            "取消管理员 韩立", sender_id="u-admin", sender_name="韩立",
            message_id="role-2", conversation_type="1",
        )
        self.assertIn("最高管理员", deny)
        spoof = self.handle(
            "设置管理员 利特", sender_id="attacker-id", sender_name="韩立",
            message_id="role-3", conversation_type="1",
        )
        self.assertNotIn("设为管理员", spoof)
        ordinary = self.handle(
            "设置管理员 刃海", sender_id="u-lite", sender_name="利特",
            message_id="role-4", conversation_type="1",
        )
        self.assertNotIn("设为管理员", ordinary)
        self.assertEqual({}, self.directory.get("刃海"))

    def test_admin_can_batch_approve_and_reject(self):
        sender = RecordingOtoSender()
        self.channel.sender = sender
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        first = self.handle("绑定 利特", sender_id="u-lite", sender_name="利特", message_id="req-1")
        second = self.handle("绑定 刃海", sender_id="u-ren", sender_name="刃海", message_id="req-2")
        self.assertIn("已提交绑定申请", first)
        self.assertIn("已提交绑定申请", second)
        self.assertTrue(any(item["title"] == "绑定申请" for item in sender.sent))
        listed = self.handle(
            "待绑定", sender_id="u-admin", sender_name="韩立",
            message_id="list-1", conversation_type="1",
        )
        self.assertIn("待审批绑定 2 条", listed)
        pending = {item["names"][0]: item["id"] for item in self.channel.bind_requests.list_pending()}
        rejected = self.handle(
            f"拒绝绑定 {pending['刃海']}", sender_id="u-admin", sender_name="韩立",
            message_id="rej-1", conversation_type="1",
        )
        self.assertIn("已拒绝", rejected)
        self.assertEqual({}, self.directory.get("刃海"))
        approved = self.handle(
            f"同意绑定 {pending['利特']}", sender_id="u-admin", sender_name="韩立",
            message_id="ok-1", conversation_type="1",
        )
        self.assertIn("已同意", approved)
        self.assertEqual("u-lite", self.directory.get("利特")["dingtalkUserId"])
        self.assertTrue(any(item["title"] == "绑定结果" for item in sender.sent))
        third = self.handle("绑定 静静", sender_id="u-jing", sender_name="静静", message_id="req-3")
        fourth = self.handle("绑定 乐言", sender_id="u-yue", sender_name="乐言", message_id="req-4")
        self.assertIn("已提交", third)
        self.assertIn("已提交", fourth)
        batched = self.handle(
            "同意绑定 1 2", sender_id="u-admin", sender_name="韩立",
            message_id="ok-batch", conversation_type="1",
        )
        self.assertIn("已同意", batched)
        self.assertEqual("u-jing", self.directory.get("静静")["dingtalkUserId"])
        self.assertEqual("u-yue", self.directory.get("乐言")["dingtalkUserId"])

    def test_admin_confirm_approves_bind_and_notifies_group(self):
        sender = RecordingOtoSender()
        self.channel.sender = sender
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        requested = self.handle("绑定 景云", sender_id="u-jing", sender_name="景云", message_id="req-jy")
        self.assertIn("已提交绑定申请", requested)
        notice = next(item for item in sender.sent if item["title"] == "绑定申请")
        self.assertIn("回复「同意绑定」或「确认绑定」同意", notice["text"])
        approved = self.handle(
            "确认", sender_id="u-admin", sender_name="韩立",
            message_id="ok-jy", conversation_type="1",
        )
        self.assertIn("已同意", approved)
        self.assertEqual("u-jing", self.directory.get("景云")["dingtalkUserId"])
        self.assertEqual([], self.runner.confirms)
        group = next(item for item in sender.sent if item["title"] == "group")
        self.assertIn("绑定成功", group["text"])
        self.assertEqual(["u-jing"], group["userIds"])
        self.assertEqual("cid", group["conversationId"])

    def test_admin_confirm_without_pending_bind_still_runs_action(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        reply = self.handle(
            "确认", sender_id="u-admin", sender_name="韩立",
            message_id="adm-cf", conversation_type="1",
        )
        self.assertIn("已执行", reply)
        self.assertEqual("latest", self.runner.confirms[-1]["id"])

    def test_already_bound_does_not_open_another_request(self):
        self.directory.upsert("张三", dingtalk_user_id="u1")
        reply = self.handle("绑定 张三", message_id="again")
        self.assertIn("已经绑定", reply)
        self.assertEqual(0, len(self.channel.bind_requests.list_pending()))

    def test_unbound_extra_id_is_not_admin(self):
        from backend.dingtalk.bindings import admin_user_ids, is_admin
        self.channel.admin_user_ids = ("extra-id",)
        self.assertFalse(is_admin(
            self.directory, "extra-id", extra_ids=["extra-id"], sender_name="韩立",
        ))
        self.assertEqual([], admin_user_ids(self.directory, extra_ids=["extra-id"]))
        help_text = self.handle("帮助", sender_id="extra-id", sender_name="韩立", message_id="ex-1")
        self.assertNotIn("你是管理员", help_text)

    def test_ordinary_admin_cannot_set_role(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        self.directory.upsert("利特", dingtalk_user_id="u-lite", role="admin")
        self.directory.upsert("刃海", dingtalk_user_id="u-ren")
        reply = self.handle(
            "设置管理员 刃海", sender_id="u-lite", sender_name="利特",
            message_id="role-ord", conversation_type="1",
        )
        self.assertNotIn("设为管理员", reply)
        self.assertEqual("operator", self.directory.get("刃海")["role"])

    def test_approve_does_not_overwrite_existing_binding(self):
        from backend.dingtalk.bindings import apply_binding
        sender = RecordingOtoSender()
        self.channel.sender = sender
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        self.directory.upsert("利特", dingtalk_user_id="u-lite")
        with self.assertRaisesRegex(ValueError, "绑定冲突"):
            apply_binding(self.directory, names=["利特"], sender_id="u-thief", sender_name="路人")
        requested = self.handle("绑定 利特", sender_id="u-thief", sender_name="路人", message_id="steal-1")
        self.assertIn("已提交", requested)
        approved = self.handle(
            "同意绑定", sender_id="u-admin", sender_name="韩立",
            message_id="steal-ok", conversation_type="1",
        )
        self.assertIn("冲突", approved)
        self.assertEqual("u-lite", self.directory.get("利特")["dingtalkUserId"])
        self.assertEqual(1, len(self.channel.bind_requests.list_pending()))

    def test_two_admins_only_one_decides_bind(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        self.directory.upsert("利特", dingtalk_user_id="u-lite", role="admin")
        self.handle("绑定 刃海", sender_id="u-ren", sender_name="刃海", message_id="race-1")
        pending = self.channel.bind_requests.list_pending()[0]
        first = self.channel.bind_requests.decide(pending["id"], status="approved", decided_by="韩立")
        second = self.channel.bind_requests.decide(pending["id"], status="approved", decided_by="利特")
        self.assertEqual("approved", first["status"])
        self.assertEqual("韩立", first["decidedBy"])
        self.assertEqual("韩立", second["decidedBy"])
        self.assertEqual("approved", second["status"])

    def test_admin_bare_confirm_prefers_open_action(self):
        self.directory.upsert("韩立", dingtalk_user_id="u-admin", role="admin")
        self.handle("绑定 刃海", sender_id="u-ren", sender_name="刃海", message_id="pend-1")
        self.runner.sessions = type("S", (), {"ensure": staticmethod(lambda *a, **k: {"id": "s1"})})()
        self.runner.actions = type("A", (), {
            "latest_open": staticmethod(lambda **k: {
                "id": "act-1", "tool": "process_insole_orders", "title": "鞋垫",
            }),
        })()
        reply = self.handle(
            "确认", sender_id="u-admin", sender_name="韩立",
            message_id="cf-act", conversation_type="1",
        )
        self.assertIn("已执行", reply)
        self.assertEqual("latest", self.runner.confirms[-1]["id"])
        self.assertEqual(1, len(self.channel.bind_requests.list_pending()))


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
        self.calls.append({"title": title, "text": text, "via": "group", **kwargs})
        if self.fail:
            raise DingTalkError("钉钉接口连接失败：simulated")
        return {"channel": "webhook"}

    def send_oto_markdown(self, title, text, *, user_ids=()):
        self.calls.append({
            "title": title, "text": text, "via": "oto",
            "at_user_ids": list(user_ids),
        })
        if self.fail:
            raise DingTalkError("钉钉接口连接失败：simulated")
        return {"channel": "oto", "atUserIds": list(user_ids)}


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
        self.directory.upsert("张三", dingtalk_user_id="u-zhang")
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
        self.assertEqual("oto", self.sender.calls[0]["via"])
        self.assertEqual(["u-zhang"], self.sender.calls[0]["at_user_ids"])
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

    def test_each_buyer_is_notified_separately(self):
        self.directory.upsert("张三", dingtalk_user_id="u-zhang")
        self.directory.upsert("李四", dingtalk_user_id="u-li")
        other = dict(_overdue_row())
        other["采购单号"] = "604265"
        other["采购员"] = "李四"
        self.scheduler.fetch_rows = lambda year: ([_overdue_row(), other], {})
        result = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(result.get("sent"))
        self.assertEqual(2, result.get("messageCount"))
        self.assertEqual(2, len(self.sender.calls))
        first, second = self.sender.calls
        self.assertEqual("oto", first["via"])
        self.assertEqual("oto", second["via"])
        self.assertEqual(["u-zhang"], first["at_user_ids"])
        self.assertEqual(["u-li"], second["at_user_ids"])
        self.assertIn("张三", first["title"])
        self.assertIn("李四", second["title"])
        self.assertNotIn("李四", first["text"])
        self.assertNotIn("张三", second["text"])
        again = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(again.get("skipped"))
        self.assertEqual(2, len(self.sender.calls))

    def test_unbound_buyers_are_not_sent_to_group(self):
        other = dict(_overdue_row())
        other["采购单号"] = "604299"
        other["采购员"] = "路人甲"
        self.scheduler.fetch_rows = lambda year: ([other], {})
        result = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(result.get("skipped"))
        self.assertIn("未绑定", result["reason"])
        self.assertEqual(["路人甲"], result["unboundBuyers"])
        self.assertEqual([], self.sender.calls)

    def test_unbound_does_not_create_group_message_when_others_are_bound(self):
        other = dict(_overdue_row())
        other["采购单号"] = "604299"
        other["采购员"] = "路人甲"
        self.scheduler.fetch_rows = lambda year: ([_overdue_row(), other], {})
        result = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(result.get("sent"))
        self.assertEqual(1, result.get("messageCount"))
        self.assertEqual(["路人甲"], result["unboundBuyers"])
        self.assertEqual(1, len(self.sender.calls))
        self.assertEqual("oto", self.sender.calls[0]["via"])
        self.assertNotIn("路人甲", self.sender.calls[0]["text"])

    def test_equivalent_aliases_share_one_private_message(self):
        self.directory.upsert("利特", dingtalk_user_id="u-lite")
        first = dict(_overdue_row())
        first["采购员"] = "利特"
        first["采购单号"] = "604271"
        second = dict(_overdue_row())
        second["采购员"] = "李佳冬（利特）"
        second["采购单号"] = "604272"
        self.scheduler.fetch_rows = lambda year: ([first, second], {})
        result = self.scheduler.run_once(today="2026-08-13")
        self.assertTrue(result.get("sent"))
        self.assertEqual(1, result.get("messageCount"))
        self.assertEqual(1, len(self.sender.calls))
        self.assertEqual("oto", self.sender.calls[0]["via"])
        self.assertEqual(["u-lite"], self.sender.calls[0]["at_user_ids"])
        self.assertIn("604271", self.sender.calls[0]["text"])
        self.assertIn("604272", self.sender.calls[0]["text"])
        self.assertIn("采购", self.sender.calls[0]["text"])
        self.assertIn("待入库", self.sender.calls[0]["text"])

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


class MentionMarkupTests(unittest.TestCase):
    def test_webhook_text_mentions_userid(self):
        from backend.dingtalk.sender import DingTalkSender, markdown_to_plain, with_mentions

        self.assertEqual("@u-zhang", with_mentions("", at_user_ids=["u-zhang"]))
        self.assertTrue(with_mentions("清单", at_user_ids=["u-zhang"]).startswith("@u-zhang"))
        self.assertEqual("标题\n需催 27 单", markdown_to_plain("### 标题\n> 需催 **27** 单"))
        captured = {}

        def fake_post(url, payload, headers=None):
            captured["url"] = url
            captured["payload"] = payload
            return {"errcode": 0}

        sender = DingTalkSender(
            client_id="app", client_secret="secret",
            robot_code="robot", group_conversation_id="cid",
        )
        sender.remember_session_webhook(
            "cid", "https://oapi.example/session",
            expires_at=int(time.time() * 1000) + 3600_000,
        )
        with patch("backend.dingtalk.sender._post_json", side_effect=fake_post):
            result = sender.send_markdown(
                "跟单催办", "### 利特\n**27** 单", at_user_ids=["u-lite"],
            )
        self.assertEqual("webhook", result["channel"])
        self.assertEqual("https://oapi.example/session", captured["url"])
        self.assertEqual("text", captured["payload"]["msgtype"])
        self.assertEqual(["u-lite"], captured["payload"]["at"]["atUserIds"])
        self.assertIn("@u-lite", captured["payload"]["text"]["content"])
        self.assertIn("27 单", captured["payload"]["text"]["content"])
        self.assertNotIn("**", captured["payload"]["text"]["content"])

    def test_oto_markdown_posts_userids(self):
        from backend.dingtalk.sender import DingTalkSender

        captured = {}

        def fake_post(url, payload, headers=None):
            captured["url"] = url
            captured["payload"] = payload
            return {"processQueryKey": "q1"}

        sender = DingTalkSender(
            client_id="app", client_secret="secret",
            robot_code="robot", group_conversation_id="cid",
        )
        sender._token = "tok"
        sender._token_expires = 9e12
        with patch("backend.dingtalk.sender._post_json", side_effect=fake_post):
            result = sender.send_oto_markdown("跟单催办", "韩立的单", user_ids=["u-han"])
        self.assertEqual("oto", result["channel"])
        self.assertIn("oToMessages/batchSend", captured["url"])
        self.assertEqual(["u-han"], captured["payload"]["userIds"])
        self.assertEqual("sampleMarkdown", captured["payload"]["msgKey"])
        param = json.loads(captured["payload"]["msgParam"])
        self.assertEqual("韩立的单", param["text"])
        self.assertNotIn("@u-han", param["text"])

    def test_oto_file_posts_sample_file(self):
        from backend.dingtalk.sender import DingTalkSender

        captured = {}

        def fake_post(url, payload, headers=None):
            captured["url"] = url
            captured["payload"] = payload
            return {"processQueryKey": "q2"}

        sender = DingTalkSender(
            client_id="app", client_secret="secret",
            robot_code="robot", group_conversation_id="cid",
        )
        sender._token = "tok"
        sender._token_expires = 9e12
        with patch("backend.dingtalk.sender._post_json", side_effect=fake_post):
            result = sender.send_oto_file(["u-anan"], "mid-9", "260818-代发.xlsx")
        self.assertEqual("oto", result["channel"])
        self.assertIn("oToMessages/batchSend", captured["url"])
        self.assertEqual("sampleFile", captured["payload"]["msgKey"])
        param = json.loads(captured["payload"]["msgParam"])
        self.assertEqual("260818-代发.xlsx", param["fileName"])
        self.assertEqual("xlsx", param["fileType"])

    def test_app_markdown_does_not_fake_at(self):
        from backend.dingtalk.sender import DingTalkSender

        captured = {}

        def fake_post(url, payload, headers=None):
            captured["url"] = url
            captured["payload"] = payload
            return {}

        sender = DingTalkSender(
            client_id="app", client_secret="secret",
            robot_code="robot", group_conversation_id="cid",
        )
        sender._token = "tok"
        sender._token_expires = 9e12
        with patch("backend.dingtalk.sender._post_json", side_effect=fake_post):
            result = sender.send_markdown("跟单催办", "利特的单", at_user_ids=["u-lite"])
        self.assertEqual("app", result["channel"])
        self.assertIn("groupMessages/send", captured["url"])
        self.assertNotIn("atUserIds", captured["payload"])
        param = json.loads(captured["payload"]["msgParam"])
        self.assertEqual("利特的单", param["text"])
        self.assertNotIn("@u-lite", param["text"])

    def test_expired_session_webhook_falls_back_to_app(self):
        from backend.dingtalk.sender import DingTalkSender

        captured = {}

        def fake_post(url, payload, headers=None):
            captured["url"] = url
            captured["payload"] = payload
            return {}

        sender = DingTalkSender(
            client_id="app", client_secret="secret",
            robot_code="robot", group_conversation_id="cid",
        )
        sender._token = "tok"
        sender._token_expires = 9e12
        sender.remember_session_webhook("cid", "https://oapi.example/expired", expires_at=1)
        with patch("backend.dingtalk.sender._post_json", side_effect=fake_post):
            result = sender.send_markdown("跟单催办", "利特的单", at_user_ids=["u-lite"])
        self.assertEqual("app", result["channel"])
        self.assertIn("groupMessages/send", captured["url"])

    def test_session_webhook_persists_across_sender(self):
        from backend.dingtalk.sender import DingTalkSender

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            first = DingTalkSender(session_store_path=path, group_conversation_id="open-cid")
            first.remember_session_webhook(
                "stream-cid", "https://oapi.example/session",
                expires_at=int(time.time() * 1000) + 3600_000,
            )
            second = DingTalkSender(session_store_path=path, group_conversation_id="open-cid")
            self.assertEqual("https://oapi.example/session", second.mention_webhook("open-cid"))
            self.assertTrue(second.status()["sessionWebhook"])

    def test_stream_caches_incoming_webhook(self):
        store = AgentStore(":memory:")
        channel = DingTalkStreamChannel(
            runner=FakeRunner(), sender=None, client_id="id", client_secret="secret",
            audit=FakeAudit(), directory=StaffDirectory(store),
        )
        from backend.dingtalk.sender import DingTalkSender
        channel.sender = DingTalkSender(group_conversation_id="open-cid")

        class Incoming:
            conversation_id = "stream-cid"
            session_webhook = "https://oapi.example/session"
            session_webhook_expired_time = int(time.time() * 1000) + 3600_000

        channel.remember_incoming_webhook(Incoming())
        self.assertEqual(
            "https://oapi.example/session",
            channel.sender.mention_webhook("open-cid"),
        )


class RestartNotifyTests(unittest.TestCase):
    def test_restart_notify_private_chats_bound_staff(self):
        from backend.dingtalk.restart import notify_pending_after_restart

        store = AgentStore(Path(tempfile.mkdtemp()) / "agent.sqlite3")
        directory = StaffDirectory(store)
        directory.upsert("利特", dingtalk_user_id="u-lite")
        sent = []

        class Sender:
            app_ready = True

            def send_oto_markdown(self, title, text, *, user_ids=()):
                sent.append({"title": title, "text": text, "userIds": list(user_ids)})
                return {"channel": "oto"}

        result = notify_pending_after_restart(
            [{
                "id": "act1", "title": "处理鞋垫订单", "operator": "利特",
                "actorId": "", "channel": "dingtalk", "expiresAt": "soon",
                "preview": {"processableCount": 12, "oIds": ["1"]},
            }],
            sender=Sender(), directory=directory, audit=FakeAudit(),
        )
        self.assertEqual(1, result["sent"])
        self.assertEqual(["u-lite"], sent[0]["userIds"])
        self.assertIn("5 分钟", sent[0]["text"])
        self.assertIn("确认", sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
