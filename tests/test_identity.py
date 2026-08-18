# -*- coding: utf-8 -*-
"""User 表、采购员归一化和身份解析。全程离线。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agent.context import resolve_request_context, resolve_user_id
from backend.agent.router import ROUTE_AGENT, ROUTE_EXACT_QUERY, route_message
from backend.agent.store import AgentStore
from backend.agent.users import (
    CONFIRMED_IDENTITIES,
    UserRepository,
    analyze_buyer_records,
    resolve_user_by_erp_buyer,
)
from backend.dingtalk.identity import StaffDirectory
from backend.staff_names import cluster_staff_names, normalize_staff_name, parse_staff_name


class NormalizeStaffNameTests(unittest.TestCase):
    def test_fullwidth_parens_and_spaces(self):
        self.assertEqual("张三(阿飞)", normalize_staff_name(" 张三 （ 阿飞 ） "))
        self.assertEqual("张三(阿飞)", normalize_staff_name("张三(阿飞)"))
        self.assertEqual("焦志强(暖茶)", normalize_staff_name("焦志强 (暖茶)"))
        self.assertEqual("洪静茹(静静)", normalize_staff_name("洪静茹(静静）"))

    def test_parse_swapped_pair(self):
        left = parse_staff_name("阿飞（张三）")
        right = parse_staff_name("张三 ( 阿飞 )")
        self.assertEqual(left.pair, right.pair)


class ClusterStaffNameTests(unittest.TestCase):
    def test_merges_confirmed_aliases_only(self):
        clusters = cluster_staff_names([
            {"raw": "利特", "count": 10, "last_seen": "2026-08-07"},
            {"raw": "李佳冬（利特）", "count": 3, "last_seen": "2026-08-04"},
            {"raw": "岳甜甜（乐言）", "count": 5, "last_seen": "2026-08-06"},
            {"raw": "乐言", "count": 2, "last_seen": "2026-06-19"},
            {"raw": "韩立", "count": 1, "last_seen": "2026-08-01"},
            {"raw": "李迎(刃海)", "count": 2, "last_seen": "2026-08-06"},
            {"raw": "刃海", "count": 1, "last_seen": "2026-08-01"},
        ])
        by_name = {item.canonical_name: item for item in clusters if item.status == "auto"}
        self.assertIn("李佳冬", by_name)
        self.assertEqual({"利特", "李佳冬（利特）"}, set(by_name["李佳冬"].aliases))
        self.assertIn("岳甜甜", by_name)
        self.assertEqual({"乐言", "岳甜甜（乐言）"}, set(by_name["岳甜甜"].aliases))
        self.assertIn("韩立", by_name)
        self.assertEqual(["韩立"], by_name["韩立"].aliases)
        blade = next(item for item in clusters if "李迎(刃海)" in item.aliases)
        self.assertEqual("李迎", blade.real_name)
        self.assertEqual("刃海", blade.nickname)

    def test_role_account_with_unique_nick_merges(self):
        clusters = cluster_staff_names([
            {"raw": "三三", "count": 10},
            {"raw": "鞋子理单（三三）", "count": 2},
        ])
        self.assertEqual(1, len(clusters))
        cluster = clusters[0]
        self.assertEqual("auto", cluster.status)
        self.assertEqual("三三", cluster.canonical_name)
        self.assertEqual({"三三", "鞋子理单（三三）"}, set(cluster.aliases))

    def test_digit_suffix_and_temp_still_review(self):
        clusters = cluster_staff_names([
            {"raw": "黄娟", "count": 5},
            {"raw": "黄娟25", "count": 1},
            {"raw": "魏大可（01）", "count": 1},
            {"raw": "临时工-涂猛", "count": 1},
        ])
        auto = {item.canonical_name: item for item in clusters if item.status == "auto"}
        review_aliases = {alias for item in clusters if item.status != "auto" for alias in item.aliases}
        self.assertIn("黄娟", auto)
        self.assertNotIn("黄娟25", auto.get("黄娟").aliases)
        self.assertIn("黄娟25", review_aliases)
        self.assertIn("魏大可（01）", review_aliases)
        self.assertIn("临时工-涂猛", review_aliases)

    def test_ambiguous_shared_nickname_needs_review(self):
        clusters = cluster_staff_names([
            {"raw": "张三（阿飞）", "count": 2},
            {"raw": "李四（阿飞）", "count": 2},
            {"raw": "阿飞", "count": 1},
        ])
        self.assertTrue(all(item.status == "needs_review" for item in clusters))


class UserRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tmp.name) / "agent.sqlite3")
        self.users = UserRepository(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_and_resolve_aliases(self):
        analysis = analyze_buyer_records([
            {"raw": "利特", "count": 10},
            {"raw": "李佳冬（利特）", "count": 3},
            {"raw": "黄娟25", "count": 1},
        ])
        result = self.users.seed_clusters(analysis["clusters"])
        self.assertEqual(1, result["createdCount"])
        self.assertEqual(1, result["skippedCount"])
        hit = resolve_user_by_erp_buyer(self.users, "阿飞（不存在）")
        self.assertFalse(hit.matched)
        self.assertEqual("unknown_buyer", hit.reason)
        lite = resolve_user_by_erp_buyer(self.users, "利特")
        self.assertTrue(lite.matched)
        self.assertTrue(lite.user_id.startswith("usr_"))
        self.assertEqual(lite.user_id, resolve_user_by_erp_buyer(self.users, "李佳冬（利特）").user_id)
        self.assertEqual(lite.user_id, resolve_user_by_erp_buyer(self.users, "李佳冬( 利特 )").user_id)

    def test_resolve_does_not_create(self):
        before = self.users.list()
        miss = self.users.resolve_by_erp_buyer("陌生人")
        self.assertFalse(miss.matched)
        self.assertEqual(before, self.users.list())

    def test_request_context_prefers_users_table(self):
        created = self.users.create(
            canonical_name="李佳冬", display_name="李佳冬（利特）",
            aliases=["利特", "李佳冬（利特）"], source="test",
        )
        directory = StaffDirectory(self.store)
        directory.upsert("利特", dingtalk_user_id="u-lite")
        self.users.attach_staff_bindings()
        web = resolve_request_context(directory, users=self.users, operator="利特", channel="web")
        ding = resolve_request_context(
            directory, users=self.users, operator="利特", channel="dingtalk", actor_id="u-lite",
        )
        self.assertEqual(created["userId"], web.user_id)
        self.assertEqual(created["userId"], ding.user_id)
        self.assertTrue(web.user_id.startswith("usr_"))
        self.assertNotEqual("u-lite", web.user_id)

    def test_confirmed_hanli_merges_admin_alias(self):
        self.users.create(canonical_name="管理员", aliases=["管理员"], source="purchase_order_seed")
        result = self.users.apply_confirmed_identities()
        self.assertEqual(1, result["appliedCount"])
        hanli = resolve_user_by_erp_buyer(self.users, "韩立")
        admin = resolve_user_by_erp_buyer(self.users, "管理员")
        self.assertTrue(hanli.matched)
        self.assertEqual(hanli.user_id, admin.user_id)
        self.assertEqual("韩立", hanli.canonical_name)
        self.assertEqual("unknown_buyer", resolve_user_by_erp_buyer(self.users, "黄娟25").reason)
        self.assertEqual(1, len(CONFIRMED_IDENTITIES))

    def test_hanli_binding_is_admin_role(self):
        directory = StaffDirectory(self.store)
        directory.upsert("韩立", dingtalk_user_id="u-han")
        directory.promote_builtin_admins()
        self.assertEqual("admin", directory.get("韩立")["role"])
        ctx = resolve_request_context(
            directory, users=self.users, operator="韩立", channel="dingtalk", actor_id="u-han",
        )
        self.assertEqual("admin", ctx.role)

    def test_compat_fallback_without_users_table(self):
        directory = StaffDirectory(self.store)
        directory.upsert("张三", dingtalk_user_id="u-zhang")
        user_id = resolve_user_id(directory, operator="张三", channel="web")
        self.assertEqual("u-zhang", user_id)

    def test_unbound_hanli_display_name_is_not_admin(self):
        from backend.dingtalk.bindings import is_admin, is_super_admin
        directory = StaffDirectory(self.store)
        self.assertFalse(is_admin(directory, "attacker-id", sender_name="韩立"))
        self.assertFalse(is_super_admin(directory, "attacker-id", sender_name="韩立"))
        ctx = resolve_request_context(
            directory, users=self.users, operator="韩立", channel="web",
        )
        self.assertEqual("operator", ctx.role)
        directory.upsert("韩立", dingtalk_user_id="u-han", role="admin")
        self.assertTrue(is_admin(directory, "u-han", sender_name="路人"))
        self.assertTrue(is_super_admin(directory, "u-han"))
        self.assertFalse(is_admin(directory, "attacker-id", sender_name="韩立"))
        self.assertFalse(is_admin(directory, "extra-id", extra_ids=["extra-id"], sender_name="韩立"))
        self.assertFalse(is_super_admin(directory, "extra-id", extra_ids=["extra-id"], sender_name="韩立"))

    def test_set_role_requires_bound_staff(self):
        directory = StaffDirectory(self.store)
        directory.upsert("韩立", dingtalk_user_id="u-han", role="admin")
        directory.upsert("利特", dingtalk_user_id="u-lite")
        updated = directory.set_role("利特", "admin")
        self.assertEqual("admin", updated["role"])
        self.assertEqual("admin", directory.get("利特")["role"])
        directory.set_role("利特", "operator")
        self.assertEqual("operator", directory.get("利特")["role"])
        with self.assertRaisesRegex(ValueError, "找不到已绑定员工"):
            directory.set_role("路人", "admin")

    def test_web_auth_code_matches_name_and_is_one_use(self):
        from backend.agent.web_auth import WebAuth, WebAuthError
        directory = StaffDirectory(self.store)
        directory.upsert("利特", dingtalk_user_id="u-lite")
        auth = WebAuth(self.store)
        issued = auth.issue_code(sender_id="u-lite", buyer_name="利特", user_id="usr_lite")
        self.assertEqual(20, len(issued["code"]))
        with self.assertRaisesRegex(WebAuthError, "不一致"):
            auth.consume_code(operator="韩立", code=issued["code"], directory=directory)
        session = auth.consume_code(operator="李佳冬（利特）", code=issued["code"], directory=directory)
        self.assertEqual("利特", session["operator"])
        self.assertEqual("u-lite", session["senderId"])
        self.assertTrue(session["webToken"])
        self.assertEqual("利特", auth.get_session(session["webToken"])["buyerName"])
        with self.assertRaisesRegex(WebAuthError, "无效或已使用"):
            auth.consume_code(operator="利特", code=issued["code"], directory=directory)


class IntentRouterSkeletonTests(unittest.TestCase):
    def test_po_arrival_is_exact_query(self):
        decision = route_message("604264 到货了吗")
        self.assertEqual(ROUTE_EXACT_QUERY, decision.route)
        self.assertEqual("get_purchase_order", decision.tool)
        self.assertEqual("604264", decision.entities["po_id"])
        self.assertEqual("L0", decision.risk_level)

    def test_open_question_goes_to_agent(self):
        decision = route_message("看看最近采购有什么需要我注意的")
        self.assertEqual(ROUTE_AGENT, decision.route)
        self.assertEqual("", decision.tool)

    def test_sales_order_status_and_items_is_two_step(self):
        decision = route_message("11530151 还能不能发？里面是不是还挂着旧鞋垫码？")
        self.assertEqual(ROUTE_EXACT_QUERY, decision.route)
        self.assertEqual("inspect_sales_order", decision.operation)
        from backend.agent.intents import intent_calls
        calls = intent_calls(decision.intent)
        self.assertEqual(
            ["search_sales_orders", "get_sales_order_items"],
            [name for name, _ in calls],
        )
        self.assertEqual("11530151", calls[0][1]["query"])
        self.assertEqual(["11530151"], calls[1][1]["o_ids"])

    def test_contract_without_invoice_asks(self):
        from backend.agent.router import ROUTE_CLARIFY
        decision = route_message("给 604264 出合同。")
        self.assertEqual(ROUTE_CLARIFY, decision.route)
        self.assertEqual(["invoice_type"], decision.missing_slots)
        self.assertEqual("604264", decision.entities.get("po_id"))


if __name__ == "__main__":
    unittest.main()
