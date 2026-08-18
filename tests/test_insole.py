# -*- coding: utf-8 -*-
"""抖音换鞋垫：尺码映射、定位分桶、意图、确认后串行写入。全程离线。"""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agent import AgentRunner, AgentStore, AuditLog, PendingActions, SessionStore
from backend.agent.intents import INSOLE_PROCESS, INSOLE_QUERY, classify_intent
from backend.agent.router import needs_llm_review, route_message
from backend.agent.permissions import CAPABILITY_INSOLE_PROCESS, check_capability
from backend.agent.tools import PermissionDenied, ToolContext, build_registry
from backend.dingtalk.identity import StaffDirectory
from backend.dingtalk.stream import DingTalkStreamChannel
from backend.exchange.insole import (
    RESERVED_REASON, SOURCE_SKU, WRITTEN_REASON, classify_insole_row,
    execute_insole_orders, format_elapsed, format_insole_list, format_insole_result,
    load_executed_insole_writes, load_insole_writes, load_reserved_insole_orders,
    locate_insole_orders, mm_from_props, remember_insole_writes, resolve_insole_size,
    sync_insole_mirror, target_sku_for_mm,
)


LINES = [
    {
        "o_id": "11549976", "so_id": "so-255", "status": "Question",
        "shop_name": "抖音-蜀黍家运动鞋旗舰店", "order_date": "2026-08-17",
        "sku_id": SOURCE_SKU, "i_id": SOURCE_SKU, "name": "鞋垫",
        "properties_value": "默认:默认", "qty": "1",
    },
    {
        "o_id": "11549976", "so_id": "so-255", "status": "Question",
        "shop_name": "抖音-蜀黍家运动鞋旗舰店", "order_date": "2026-08-17",
        "sku_id": "XZ26401302029BL06", "i_id": "XZ26401302029", "name": "鞋",
        "properties_value": "颜色分类:网眼款;鞋码大小:41(255)", "qty": "1",
    },
    {
        "o_id": "11550001", "so_id": "so-half", "status": "WaitConfirm",
        "shop_name": "抖音-蜀黍家运动鞋旗舰店", "order_date": "2026-08-17",
        "sku_id": SOURCE_SKU, "properties_value": "默认:默认", "qty": "1",
    },
    {
        "o_id": "11550001", "so_id": "so-half", "status": "WaitConfirm",
        "shop_name": "抖音-蜀黍家运动鞋旗舰店", "order_date": "2026-08-17",
        "sku_id": "SHOE-HALF", "properties_value": "鞋码大小:40.5(252.5)", "qty": "1",
    },
    {
        "o_id": "11550002", "so_id": "so-ship", "status": "Delivering",
        "shop_name": "抖音-蜀黍家运动鞋旗舰店", "order_date": "2026-08-17",
        "sku_id": SOURCE_SKU, "properties_value": "默认:默认", "qty": "1",
    },
    {
        "o_id": "11550002", "so_id": "so-ship", "status": "Delivering",
        "shop_name": "抖音-蜀黍家运动鞋旗舰店", "order_date": "2026-08-17",
        "sku_id": "SHOE-SHIP", "properties_value": "鞋码大小:42(260)", "qty": "1",
    },
    {
        "o_id": "11550003", "so_id": "so-tmall", "status": "Question",
        "shop_name": "天猫旗舰店", "order_date": "2026-08-17",
        "sku_id": SOURCE_SKU, "properties_value": "默认:默认", "qty": "1",
    },
    {
        "o_id": "11550003", "so_id": "so-tmall", "status": "Question",
        "shop_name": "天猫旗舰店", "order_date": "2026-08-17",
        "sku_id": "SHOE-TM", "properties_value": "鞋码大小:41(255)", "qty": "1",
    },
]


class FakeLLM:
    configured = True
    model = "fake"

    def status(self):
        return {"configured": True, "model": self.model}

    def chat(self, messages, *, tools=None, tool_choice="auto"):
        raise AssertionError("固定意图不应调用 LLM")


class ReviewLLM:
    """鞋垫处理走 LLM 审核：先调 process_insole_orders，再组织确认话术。"""

    configured = True
    model = "fake-review"

    def __init__(self):
        self.calls = 0
        self.seen = []

    def status(self):
        return {"configured": True, "model": self.model}

    def chat(self, messages, *, tools=None, tool_choice="auto"):
        self.calls += 1
        self.seen.append({"tool_choice": tool_choice, "messages": messages})
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "call-insole",
                    "type": "function",
                    "function": {"name": "process_insole_orders", "arguments": "{}"},
                }],
            }
        return {"content": "请核对清单后回复确认", "tool_calls": []}


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


class FakeErp:
    def __init__(self):
        self.calls = []

    def prepare(self):
        self.calls.append({"prepare": True})
        return {"ok": True}

    def run(self, command, payload):
        self.calls.append(payload)
        if payload.get("confirm"):
            plans = payload.get("plans") or (payload.get("plan") or {}).get("plans") or []
            return {
                "succeeded": [{"o_id": item.get("o_id")} for item in plans],
                "failed": [],
                "attempted": len(plans),
            }
        oids = (payload.get("targets") or {}).get("o_ids") or []
        target = payload["rules"]["replacements"][0]["to"]
        return {
            "plans": [{
                "o_id": oid, "ok": True, "src_sku_id": SOURCE_SKU,
                "new_sku_id": target, "mode": "ChangeItem",
            } for oid in oids],
        }


class MappingTests(unittest.TestCase):
    def test_mm_from_shoe_props(self):
        self.assertEqual("255", mm_from_props("颜色分类:网眼款;鞋码大小:41(255)"))
        self.assertEqual("252.5", mm_from_props("鞋码大小:40.5(252.5)"))
        self.assertEqual("", mm_from_props("默认:默认"))
        self.assertEqual("", target_sku_for_mm("252.5"))
        self.assertEqual("XZ25401308-09907", target_sku_for_mm("255"))

    def test_half_size_drops_decimal_then_maps(self):
        sized = resolve_insole_size("颜色分类:网眼款;鞋码大小:40.5(252.5)")
        self.assertEqual("250", sized["shoe_mm"])
        self.assertEqual("XZ25401308-09906", sized["target_sku"])
        self.assertIn("40.5", sized["size_note"])
        sized = resolve_insole_size("鞋码大小:41.5(257.5)")
        self.assertEqual("255", sized["shoe_mm"])
        self.assertEqual("XZ25401308-09907", sized["target_sku"])

    def test_kuaishou_and_channels_props(self):
        kuaishou = resolve_insole_size("布面款;39 (245)")
        self.assertEqual("245", kuaishou["shoe_mm"])
        self.assertEqual("XZ25401308-09905", kuaishou["target_sku"])
        kuaishou = resolve_insole_size("网眼款;43 (265)")
        self.assertEqual("265", kuaishou["shoe_mm"])
        self.assertEqual("XZ25401308-09909", kuaishou["target_sku"])
        channels = resolve_insole_size("网眼款;42")
        self.assertEqual("260", channels["shoe_mm"])
        self.assertEqual("XZ25401308-09908", channels["target_sku"])

    def test_status_buckets(self):
        base = {
            "o_id": "1", "so_id": "s", "shop": "抖音店", "has_source": True,
            "shoe_mm": "255", "target_sku": "XZ25401308-09907",
        }
        self.assertEqual("processable", classify_insole_row({**base, "status": "Question"})["bucket"])
        self.assertEqual("processable", classify_insole_row({**base, "status": "WaitConfirm"})["bucket"])
        self.assertEqual("parked", classify_insole_row({**base, "status": "Delivering"})["bucket"])
        self.assertEqual("skipped", classify_insole_row({**base, "status": "Sent"})["bucket"])
        half = classify_insole_row({
            **base, "status": "Question", "shoe_mm": "", "target_sku": "",
            "shoe_props": "鞋码大小:40.5(252.5)",
        })
        self.assertEqual("processable", half["bucket"])
        self.assertEqual("XZ25401308-09906", half["target_sku"])


class LocateTests(unittest.TestCase):
    def test_locate_splits_processable_parked_and_other_shop(self):
        located = locate_insole_orders(lines=LINES, shop="抖音")
        self.assertEqual({"11549976", "11550001"}, set(located["oIds"]))
        self.assertEqual(2, located["processableCount"])
        by_oid = {row["o_id"]: row for row in located["processable"]}
        self.assertEqual("XZ25401308-09907", by_oid["11549976"]["target_sku"])
        self.assertEqual("XZ25401308-09906", by_oid["11550001"]["target_sku"])
        reasons = {row["o_id"]: row["reason"] for row in located["parked"]}
        self.assertIn("11550002", reasons)
        skipped = {row["o_id"] for row in located["skipped"]}
        self.assertIn("11550003", skipped)

    def test_default_pool_includes_kuaishou_and_channels(self):
        extra = [
            {
                "o_id": "11553977", "so_id": "ks-1", "status": "WaitConfirm",
                "shop_name": "快手-蜀黍家运动鞋服", "order_date": "2026-08-17",
                "sku_id": SOURCE_SKU, "properties_value": "默认:默认", "qty": "1",
            },
            {
                "o_id": "11553977", "so_id": "ks-1", "status": "WaitConfirm",
                "shop_name": "快手-蜀黍家运动鞋服",
                "sku_id": "SHOE-KS", "properties_value": "布面款;39 (245)", "qty": "1",
            },
            {
                "o_id": "11553117", "so_id": "ks-2", "status": "Question",
                "shop_name": "快手-蜀黍家运动鞋服",
                "sku_id": SOURCE_SKU, "properties_value": "默认:默认", "qty": "1",
            },
            {
                "o_id": "11553117", "so_id": "ks-2", "status": "Question",
                "shop_name": "快手-蜀黍家运动鞋服",
                "sku_id": "SHOE-KS2", "properties_value": "网眼款;43 (265)", "qty": "1",
            },
            {
                "o_id": "11553023", "so_id": "wx-1", "status": "Question",
                "shop_name": "微信视频号-蜀黍家通勤男鞋",
                "sku_id": SOURCE_SKU, "properties_value": "默认:默认", "qty": "1",
            },
            {
                "o_id": "11553023", "so_id": "wx-1", "status": "Question",
                "shop_name": "微信视频号-蜀黍家通勤男鞋",
                "sku_id": "SHOE-WX", "properties_value": "网眼款;42", "qty": "1",
            },
        ]
        located = locate_insole_orders(lines=LINES + extra)
        by_oid = {row["o_id"]: row for row in located["processable"]}
        self.assertEqual("XZ25401308-09905", by_oid["11553977"]["target_sku"])
        self.assertEqual("XZ25401308-09909", by_oid["11553117"]["target_sku"])
        self.assertEqual("XZ25401308-09908", by_oid["11553023"]["target_sku"])
        self.assertNotIn("11550003", by_oid)

    def test_locate_skips_reserved_orders(self):
        located = locate_insole_orders(
            lines=LINES, shop="抖音",
            reserved={"11549976": {"operator": "韩立", "action_id": "abc", "status": "pending"}},
        )
        self.assertEqual(["11550001"], located["oIds"])
        skipped = {row["o_id"]: row["reason"] for row in located["skipped"]}
        self.assertTrue(skipped["11549976"].startswith(RESERVED_REASON))
        self.assertIn("韩立", skipped["11549976"])
        self.assertIn("他人待确认或正在写入的 1 单", format_insole_list(located))

    def test_load_reserved_skips_current_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "agent.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE pending_actions (
                    id TEXT, operator TEXT, status TEXT, tool TEXT,
                    preview_json TEXT, arguments_json TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO pending_actions
                (id, operator, status, tool, preview_json, arguments_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("act-han", "韩立", "pending", "process_insole_orders",
                     '{"oIds":["11549976"],"orders":[{"oId":"11549976"}]}',
                     '{"o_ids":["11549976"]}'),
                    ("act-lite", "利特", "confirmed", "process_insole_orders",
                     '{"oIds":["11550001"]}', '{"o_ids":["11550001"]}'),
                ],
            )
            conn.commit()
            conn.close()
            reserved = load_reserved_insole_orders(db, exclude_action_id="act-lite")
            self.assertIn("11549976", reserved)
            self.assertNotIn("11550001", reserved)
            self.assertEqual("韩立", reserved["11549976"]["operator"])
            own = load_reserved_insole_orders(db, viewer="利特")
            self.assertIn("11549976", own)
            self.assertNotIn("11550001", own)
            other = load_reserved_insole_orders(db, viewer="韩立")
            self.assertNotIn("11549976", other)
            self.assertIn("11550001", other)

    def test_locate_skips_recently_written_orders(self):
        located = locate_insole_orders(
            lines=LINES, shop="抖音",
            written={"11549976": {"target_sku": "XZ25401308-09907"}},
        )
        self.assertEqual(["11550001"], located["oIds"])
        skipped = {row["o_id"]: row["reason"] for row in located["skipped"]}
        self.assertEqual(WRITTEN_REASON, skipped["11549976"])
        text = format_insole_list(located)
        self.assertIn("已排除刚写入、镜像尚未跟上的 1 单", text)
        self.assertIn("鞋码 → 目标鞋垫", text)

    def test_list_and_result_show_five_rows(self):
        rows = []
        for index in range(12):
            rows.extend([
                {
                    "o_id": str(1000 + index), "so_id": f"so-{index}",
                    "status": "Question", "shop_name": "抖音旗舰店",
                    "sku_id": SOURCE_SKU, "properties_value": "默认:默认", "qty": "1",
                },
                {
                    "o_id": str(1000 + index), "so_id": f"so-{index}",
                    "status": "Question", "shop_name": "抖音旗舰店",
                    "sku_id": f"SHOE-{index}",
                    "properties_value": "鞋码大小:41(255)", "qty": "1",
                },
            ])
        text = format_insole_list(locate_insole_orders(lines=rows, shop="抖音"))
        self.assertIn("待处理 12 单", text)
        self.assertIn("还有 7 单未展开", text)
        self.assertEqual(5, text.count(" → XZ25401308-09907"))
        self.assertNotIn("抖音旗舰店", text.split("\n", 1)[-1])
        result = format_insole_result({
            "okCount": 12, "skippedCount": 0, "failedCount": 0,
            "log": [{"oId": str(1000 + index), "targetSku": "XZ25401308-09907", "result": "ok"}
                    for index in range(12)],
        })
        self.assertIn("内部单号　结果　目标鞋垫", result)
        self.assertIn("其余 7 单已缩略", result)

    def test_remembered_and_executed_writes_are_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remember_insole_writes(
                [{"o_id": "11549976", "target_sku": "XZ25401308-09907"}],
                root=root,
            )
            loaded = load_insole_writes(root=root)
            self.assertEqual("XZ25401308-09907", loaded["11549976"]["target_sku"])
            db = root / "agent.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE pending_actions (
                    preview_json TEXT, result_json TEXT, executed_at TEXT,
                    tool TEXT, status TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO pending_actions
                (tool, status, preview_json, result_json, executed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "process_insole_orders", "executed",
                    '{"orders":[{"oId":"11550001","targetSku":"XZ25401308-09906"}]}',
                    '{"log":[{"oId":"11550001","result":"ok"}]}',
                    "2099-01-01T00:00:00+00:00",
                ),
            )
            conn.commit()
            conn.close()
            executed = load_executed_insole_writes(db)
            self.assertEqual("XZ25401308-09906", executed["11550001"]["target_sku"])


class ExecuteTests(unittest.TestCase):
    def test_execute_holds_exclusive_across_plan_and_write(self):
        events = []
        lock = threading.RLock()
        mid = threading.Event()
        go = threading.Event()

        class Runtime:
            def exclusive(self):
                return lock

            def prepare(self):
                events.append(f"p:{threading.current_thread().name}")
                if threading.current_thread().name == "t1":
                    mid.set()
                    go.wait(2)

            def run(self, command, payload):
                events.append(f"r:{threading.current_thread().name}")
                if payload.get("confirm"):
                    return {"succeeded": [{"o_id": "1"}], "failed": [], "attempted": 1}
                return {"plans": [{"o_id": "1", "ok": True}]}

        runtime = Runtime()

        def job(name, oid):
            execute_insole_orders(runtime, [{"o_id": oid, "target_sku": "XZ25401308-09907"}])

        first = threading.Thread(target=job, args=("t1", "1"), name="t1")
        second = threading.Thread(target=job, args=("t2", "2"), name="t2")
        first.start()
        self.assertTrue(mid.wait(1))
        second.start()
        time.sleep(0.1)
        self.assertEqual(["p:t1"], events)
        go.set()
        first.join(2)
        second.join(2)
        self.assertEqual("p:t1", events[0])
        self.assertIn("p:t2", events)
        self.assertLess(events.index("p:t1"), events.index("p:t2"))
        self.assertLess(events.index("r:t1"), events.index("p:t2"))

    def test_groups_by_target_then_serial_execute(self):
        runtime = FakeErp()
        result = execute_insole_orders(runtime, [
            {"o_id": "1", "target_sku": "XZ25401308-09907"},
            {"o_id": "2", "target_sku": "XZ25401308-09907"},
            {"o_id": "3", "target_sku": "XZ25401308-09908"},
        ])
        self.assertTrue(any(call.get("prepare") for call in runtime.calls))
        self.assertEqual(0, len([
            call for call in runtime.calls if not call.get("confirm") and not call.get("prepare")
        ]))
        confirms = [call for call in runtime.calls if call.get("confirm")]
        self.assertEqual(1, len(confirms))
        self.assertEqual(50, confirms[0].get("delayMs"))
        self.assertEqual(3, confirms[0].get("concurrency"))
        self.assertEqual(5, confirms[0].get("readConcurrency"))
        self.assertEqual(3, result["okCount"])
        self.assertEqual(0, result["failedCount"])

    def test_sync_mirror_writes_all_orders_in_one_batch(self):
        calls = []

        def fake_batch(env_path, replacements):
            calls.append((env_path, list(replacements)))
            return [str(item["o_id"]) for item in replacements]

        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        tmp.write("MYSQL_HOST=127.0.0.1\n")
        tmp.close()
        with patch("backend.realtime_mirror.replace_order_item_skus", side_effect=fake_batch):
            result = sync_insole_mirror(tmp.name, [
                {"o_id": "1", "target_sku": "XZ25401308-09907"},
                {"o_id": "2", "target_sku": "XZ25401308-09908"},
            ])
        Path(tmp.name).unlink(missing_ok=True)
        self.assertEqual(1, len(calls))
        self.assertEqual(2, len(calls[0][1]))
        self.assertEqual(["1", "2"], result["applied"])
        self.assertEqual(SOURCE_SKU, calls[0][1][0]["source_sku"])

    def test_result_log_abbreviates(self):
        text = format_insole_result({
            "okCount": 20, "skippedCount": 1, "failedCount": 0,
            "log": [{"oId": str(i), "targetSku": "XZ25401308-09907", "result": "ok"} for i in range(20)],
        })
        self.assertIn("成功 20", text)
        self.assertIn("其余 15 单已缩略", text)

    def test_result_log_includes_elapsed(self):
        self.assertEqual("11 秒", format_elapsed(11000))
        self.assertEqual("1 分 3 秒", format_elapsed(63000))
        self.assertEqual("1 小时 2 分", format_elapsed(3720000))
        text = format_insole_result({
            "okCount": 2, "skippedCount": 0, "failedCount": 0,
            "elapsedMs": 63000,
            "prepareMs": 48000,
            "writeMs": 14000,
            "readMs": 50000,
            "log": [{"oId": "1", "targetSku": "XZ25401308-09907", "result": "ok"}],
        })
        self.assertIn("用时 1 分 3 秒", text)
        self.assertIn("开页 48 秒", text)
        self.assertIn("写入 14 秒", text)
        self.assertIn("回读 50 秒", text)


class IntentTests(unittest.TestCase):
    def test_query_and_process_and_exclusions(self):
        self.assertEqual(INSOLE_QUERY, classify_intent("查询一下现在抖音需要更换的鞋垫订单").name)
        self.assertEqual(
            INSOLE_PROCESS,
            classify_intent("查询一下现在抖音需要更换的鞋垫订单，进行处理").name,
        )
        self.assertEqual(INSOLE_PROCESS, classify_intent("进行处理").name)
        self.assertEqual(INSOLE_PROCESS, classify_intent("处理这些").name)
        also = classify_intent("218 单也需要进行换鞋垫的动作")
        self.assertEqual(INSOLE_PROCESS, also.name)
        self.assertNotIn("o_ids", also.arguments)
        pasted = classify_intent(
            "11553977\n11553117\n11553023\n这三单，也需要做下换鞋垫的动作",
        )
        self.assertEqual(INSOLE_PROCESS, pasted.name)
        self.assertEqual(
            ["11553977", "11553117", "11553023"],
            pasted.arguments.get("o_ids"),
        )
        self.assertNotIn("shop", pasted.arguments)
        status = classify_intent("11530151 还能不能发？里面是不是还挂着旧鞋垫码？")
        self.assertNotEqual("locate_insole_orders", getattr(status, "tool", ""))
        self.assertNotEqual("process_insole_orders", getattr(status, "tool", ""))
        self.assertEqual({}, classify_intent("查询一下现在抖音需要更换的鞋垫订单").arguments)
        process = classify_intent("218 单也需要进行换鞋垫的动作")
        self.assertFalse(needs_llm_review(process))
        self.assertFalse(needs_llm_review(classify_intent("查询一下现在抖音需要更换的鞋垫订单")))
        self.assertEqual("workflow", route_message("218 单也需要进行换鞋垫的动作").route)
        self.assertEqual({"shop": "快手"}, classify_intent("查询快手鞋垫订单").arguments)
        quality = classify_intent("品控 佰特 604264 鞋垫开胶 3 双")
        self.assertEqual("record_quality_issue", quality.tool)
        exchange = classify_intent("把订单 11530151 里的 XZ25401308-101 换成 XZ25401308-09906")
        self.assertEqual("submit_exchange_dry_run", exchange.tool)
        overdue = classify_intent("今年逾期多少")
        self.assertEqual("dashboard_summary", overdue.tool)


class PermissionTests(unittest.TestCase):
    def test_viewer_and_future_capability_list(self):
        with self.assertRaises(PermissionDenied):
            check_capability(None, operator="看客", role="viewer", capability=CAPABILITY_INSOLE_PROCESS)
        check_capability(None, operator="利特", role="operator", capability=CAPABILITY_INSOLE_PROCESS)
        directory = type("Dir", (), {
            "get_by_dingtalk_user_id": lambda self, uid: {
                "buyerName": "利特", "dingtalkUserId": uid, "role": "operator",
                "capabilities": ["notify"],
            },
        })()
        with self.assertRaises(PermissionDenied):
            check_capability(
                directory, operator="利特", actor_id="u1", channel="dingtalk",
                role="operator", capability=CAPABILITY_INSOLE_PROCESS,
            )


class AgentInsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tmp.name) / "agent.sqlite3")
        self.erp = FakeErp()
        self.runner = AgentRunner(
            registry=build_registry(with_forecast=False, with_exchange=True, with_notifier=False),
            llm=FakeLLM(),
            sessions=SessionStore(self.store),
            actions=PendingActions(self.store),
            audit=AuditLog(self.store),
            context=ToolContext(
                env_path="unused.env", root=Path(self.tmp.name),
                fetch_rows=lambda year=None: ([], {}),
                setting=lambda name, default="": default, erp=self.erp,
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_query_does_not_write(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            answer = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单",
                session_key="q1", operator="利特",
            )
        self.assertEqual(INSOLE_QUERY, answer["intent"])
        self.assertIn("11549976", answer["reply"])
        self.assertIn("09907", answer["reply"])
        self.assertEqual([], answer["pendingActions"])
        self.assertEqual([], self.erp.calls)

    def test_process_lists_orders_then_confirm_writes(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            answer = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单，进行处理",
                session_key="p1", operator="利特",
            )
            self.assertEqual(INSOLE_PROCESS, answer["intent"])
            self.assertEqual(1, len(answer["pendingActions"]))
            self.assertIn("11549976", answer["reply"])
            self.assertEqual([], self.erp.calls)
            executed = self.runner.confirm(answer["pendingActions"][0]["id"], "利特")
        self.assertEqual(2, executed["result"]["okCount"])
        self.assertTrue(any(call.get("confirm") for call in self.erp.calls))
        remembered = load_insole_writes(root=Path(self.tmp.name))
        self.assertIn("11549976", remembered)
        self.assertIn("11550001", remembered)

    def test_other_user_cannot_process_reserved_orders(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            first = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单，进行处理",
                session_key="han", operator="韩立",
            )
        reserved = load_reserved_insole_orders(Path(self.tmp.name) / "agent.sqlite3")
        self.assertIn("11549976", reserved)
        located = locate_insole_orders(lines=LINES, shop="抖音", reserved=reserved)
        self.assertEqual([], located["oIds"])
        self.assertEqual(first["pendingActions"][0]["id"], reserved["11549976"]["action_id"])

    def test_repeat_process_reuses_pending_and_drops_older(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            first = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单，进行处理",
                session_key="p-reuse", operator="利特",
            )
            second = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单，进行处理",
                session_key="p-reuse", operator="利特",
            )
        self.assertEqual(first["pendingActions"][0]["id"], second["pendingActions"][0]["id"])
        self.assertEqual(1, len(self.runner.actions.list(session_id=first["sessionId"])))

    def test_repeat_process_sees_own_reserved_orders(self):
        db = Path(self.tmp.name) / "agent.sqlite3"

        def fake_locate(*_args, **kwargs):
            return locate_insole_orders(
                lines=LINES,
                shop=kwargs.get("shop") or "抖音",
                o_ids=kwargs.get("o_ids"),
                reserved=kwargs.get("reserved") or {},
                written=kwargs.get("written") or {},
            )

        self.runner.context.setting = (
            lambda name, default="", path=str(db): path if name == "AGENT_DATABASE_PATH" else default
        )
        with patch("backend.agent.tools.locate_insole_orders", side_effect=fake_locate):
            first = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单，进行处理",
                session_key="p-own", operator="利特",
            )
            second = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单，进行处理",
                session_key="p-own", operator="利特",
            )
        self.assertIn("11549976", first["reply"])
        self.assertIn("11549976", second["reply"])
        self.assertEqual(first["pendingActions"][0]["id"], second["pendingActions"][0]["id"])

    def test_repeat_process_keeps_larger_pending(self):
        full = locate_insole_orders(lines=LINES)
        partial_lines = [row for row in LINES if str(row.get("o_id")) == "11550001"]
        partial = locate_insole_orders(lines=partial_lines)
        with patch(
            "backend.agent.tools.locate_insole_orders",
            side_effect=[full, partial],
        ):
            first = self.runner.handle_intent(
                "查询一下现在抖音需要更换的鞋垫订单，进行处理",
                session_key="p-keep", operator="利特",
            )
            second = self.runner.handle_intent(
                "218 单也需要进行换鞋垫的动作",
                session_key="p-keep", operator="利特",
            )
        self.assertEqual(first["pendingActions"][0]["id"], second["pendingActions"][0]["id"])
        self.assertIn("11549976", second["reply"])

    def test_chat_short_circuits_intent_without_llm(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            answer = self.runner.chat(
                message="查询一下现在抖音需要更换的鞋垫订单",
                session_key="c1", operator="利特",
            )
        self.assertEqual(INSOLE_QUERY, answer["intent"])
        self.assertIn("11549976", answer["reply"])

    def test_chat_process_short_circuits_without_llm(self):
        llm = ReviewLLM()
        self.runner.llm = llm
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            answer = self.runner.chat(
                message="218 单也需要进行换鞋垫的动作",
                session_key="c-review", operator="利特",
            )
        self.assertEqual(0, llm.calls)
        self.assertEqual(INSOLE_PROCESS, answer["intent"])
        self.assertEqual(1, len(answer["pendingActions"]))
        self.assertIn("11549976", answer["reply"])


class DingTalkInsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tmp.name) / "agent.sqlite3")
        self.directory = StaffDirectory(self.store)
        self.directory.upsert("利特", dingtalk_user_id="u-lite")
        self.erp = FakeErp()
        self.runner = AgentRunner(
            registry=build_registry(with_forecast=False, with_exchange=True, with_notifier=False),
            llm=ReviewLLM(),
            sessions=SessionStore(self.store),
            actions=PendingActions(self.store),
            audit=AuditLog(self.store),
            context=ToolContext(
                env_path="unused.env", root=Path(self.tmp.name),
                fetch_rows=lambda year=None: ([], {}),
                setting=lambda name, default="": default, erp=self.erp,
            ),
            directory=self.directory,
        )
        self.channel = DingTalkStreamChannel(
            runner=self.runner, sender=None, client_id="app", client_secret="secret",
            audit=FakeAudit(), enabled=True, directory=self.directory,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_dingtalk_process_requires_second_confirm(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            reply = self.channel.handle(
                text="查询一下现在抖音需要更换的鞋垫订单，进行处理",
                message_id="m-insole", conversation_id="cid",
                sender_id="u-lite", sender_name="利特",
            )
        self.assertIn("11549976", reply)
        self.assertIn("确认", reply)
        self.assertEqual([], self.erp.calls)

    def test_dingtalk_bare_confirm_writes_and_returns_log(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            listed = self.channel.handle(
                text="查询一下现在抖音需要更换的鞋垫订单，进行处理",
                message_id="m-insole-2", conversation_id="cid",
                sender_id="u-lite", sender_name="利特",
            )
            self.assertIn("11549976", listed)
            self.assertEqual([], self.erp.calls)
            reply = self.channel.handle(
                text="确认",
                message_id="m-confirm", conversation_id="cid",
                sender_id="u-lite", sender_name="利特",
            )
        self.assertIn("成功 2", reply)
        self.assertIn("11549976", reply)
        self.assertIn("11550001", reply)
        self.assertTrue(any(call.get("confirm") for call in self.erp.calls))

    def test_dingtalk_confirm_notifies_when_done(self):
        class RecordingSender:
            app_ready = True
            webhook_ready = False

            def __init__(self):
                self.replies = []

            def reply_text(self, *, conversation_id, text, at_user_ids=()):
                self.replies.append({
                    "conversation_id": conversation_id, "text": text,
                    "at": list(at_user_ids),
                })
                return {"ok": True}

        sender = RecordingSender()
        self.channel.sender = sender
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            listed = self.channel.handle(
                text="查询一下现在抖音需要更换的鞋垫订单，进行处理",
                message_id="m-insole-3", conversation_id="cid",
                sender_id="u-lite", sender_name="利特",
            )
            self.assertIn("11549976", listed)
            started = self.channel.handle(
                text="确认",
                message_id="m-confirm-later", conversation_id="cid",
                sender_id="u-lite", sender_name="利特",
            )
            self.assertIn("已开始写入", started)
            self.assertIn("任务完成", started)
            self.assertEqual([], self.erp.calls)
            for thread in self.channel._confirm_threads:
                thread.join(timeout=5)
        self.assertTrue(self.erp.calls)
        self.assertEqual(1, len(sender.replies))
        done = sender.replies[0]["text"]
        self.assertIn("【任务完成】", done)
        self.assertIn("成功 2", done)
        self.assertIn("用时", done)
        self.assertIn("11549976", done)
        self.assertEqual(["u-lite"], sender.replies[0]["at"])

    def test_unbound_cannot_process(self):
        with patch("backend.agent.tools.locate_insole_orders", return_value=locate_insole_orders(lines=LINES)):
            reply = self.channel.handle(
                text="查询一下现在抖音需要更换的鞋垫订单，进行处理",
                message_id="m-unbound", conversation_id="cid",
                sender_id="u-new", sender_name="路人",
            )
        self.assertIn("绑定", reply)
        self.assertEqual([], self.erp.calls)


if __name__ == "__main__":
    unittest.main()
