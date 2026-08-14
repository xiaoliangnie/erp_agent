# -*- coding: utf-8 -*-
"""Agent Core：工具循环、pending-action 确认状态机、审计。

用假 LLM 和假工具，全程离线：不连 ERP 数据库、不调模型接口。
"""
import json
import tempfile
import unittest
from pathlib import Path

from backend.agent import (
    ActionError,
    AgentDisabled,
    AgentRunner,
    AgentStore,
    AuditLog,
    PendingActions,
    SessionStore,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    build_registry,
    flag,
)
from backend.agent.llm import LLMClient
from backend.dingtalk.identity import StaffDirectory
from backend.staff_names import WEB_OPERATOR_UNBOUND


class FakeLLM:
    """按脚本逐轮返回 assistant 消息，记录收到的 messages 以便断言上下文。"""

    def __init__(self, script):
        self.script = list(script)
        self.model = "fake-model"
        self.configured = True
        self.calls = []

    def status(self):
        return {"configured": True, "model": self.model, "endpoint": "fake://"}

    def chat(self, messages, *, tools=None, tool_choice="auto"):
        self.calls.append({"messages": [dict(item) for item in messages], "tools": tools})
        if not self.script:
            return {"role": "assistant", "content": "没有更多脚本了", "tool_calls": []}
        return self.script.pop(0)


def text_answer(content):
    return {"role": "assistant", "content": content, "tool_calls": []}


def tool_answer(name, arguments, *, call_id="call-1", content=""):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }],
    }


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tmp.name) / "agent.sqlite3")
        self.audit = AuditLog(self.store)
        self.actions = PendingActions(self.store, ttl_seconds=1800)
        self.sessions = SessionStore(self.store, history_limit=20)
        self.executed = []
        self.registry = ToolRegistry()
        self.registry.register(Tool(
            name="read_orders", description="只读工具", parameters={"type": "object", "properties": {}},
            risk="L0", handler=lambda args, ctx: {"orders": [args.get("query", "")], "operator": ctx.operator},
        ))
        self.registry.register(Tool(
            name="explode", description="总是失败的工具", parameters={"type": "object", "properties": {}},
            risk="L0", handler=self._explode,
        ))
        self.registry.register(Tool(
            name="make_file", description="生成产物",
            parameters={"type": "object", "properties": {"po_id": {"type": "string"}}},
            risk="L1", handler=self._make_file,
            preview=lambda args, ctx: {"po": args.get("po_id"), "willCost": "一次真实生成"},
            title=lambda args: f"生成 {args.get('po_id')}",
        ))
        self.context = ToolContext(
            env_path="unused.env", root=Path(self.tmp.name),
            fetch_rows=lambda year=None: ([], {"year": year}), audit=self.audit,
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _explode(arguments, ctx):
        raise ToolError("这个单号不存在")

    def _make_file(self, arguments, ctx):
        self.executed.append({"args": arguments, "operator": ctx.operator, "actionId": ctx.action_id})
        return {"contractId": "abc", "downloadUrl": "/api/agent/contracts/abc/file"}

    def runner(self, script, **kwargs):
        options = {"registry": self.registry, "max_steps": 4, "enabled": True, **kwargs}
        return AgentRunner(
            llm=FakeLLM(script), sessions=self.sessions,
            actions=self.actions, audit=self.audit, context=self.context,
            **options,
        )


class ToolLoopTests(AgentTestCase):
    def test_read_only_tool_runs_and_answer_returns(self):
        agent = self.runner([tool_answer("read_orders", {"query": "604264"}), text_answer("查到了")])
        answer = agent.chat(message="查一下 604264", session_key="s1", operator="张三")
        self.assertEqual("查到了", answer["reply"])
        self.assertEqual(["read_orders"], [step["tool"] for step in answer["steps"]])
        self.assertEqual([], answer["pendingActions"])
        recorded = self.audit.recent_tools()
        self.assertEqual("ok", recorded[0]["status"])
        self.assertIn("604264", recorded[0]["resultSummary"])

    def test_tool_result_is_fed_back_to_the_model(self):
        llm = FakeLLM([tool_answer("read_orders", {"query": "A"}), text_answer("好")])
        agent = AgentRunner(registry=self.registry, llm=llm, sessions=self.sessions,
                            actions=self.actions, audit=self.audit, context=self.context)
        agent.chat(message="问", session_key="s1", operator="张三")
        second_round = llm.calls[1]["messages"]
        self.assertEqual("tool", second_round[-1]["role"])
        self.assertIn("orders", second_round[-1]["content"])
        self.assertEqual("read_orders", second_round[-1]["name"])

    def test_tool_error_is_reported_not_raised(self):
        agent = self.runner([tool_answer("explode", {}), text_answer("这个单号查不到")])
        answer = agent.chat(message="查 999", session_key="s1", operator="张三")
        self.assertEqual("error", answer["steps"][0]["status"])
        self.assertEqual("这个单号查不到", answer["reply"])
        self.assertEqual("error", self.audit.recent_tools()[0]["status"])

    def test_unknown_tool_is_rejected(self):
        agent = self.runner([tool_answer("drop_table", {}), text_answer("换个说法")])
        answer = agent.chat(message="删库", session_key="s1", operator="张三")
        self.assertEqual("error", answer["steps"][0]["status"])

    def test_step_limit_stops_the_loop(self):
        agent = self.runner([tool_answer("read_orders", {}) for _ in range(6)], max_steps=3)
        answer = agent.chat(message="一直查", session_key="s1", operator="张三")
        self.assertEqual(3, len(answer["steps"]))
        self.assertIn("步数上限", answer["reply"])

    def test_history_carries_between_turns_and_starts_at_a_user_message(self):
        agent = self.runner([text_answer("第一轮"), text_answer("第二轮")])
        first = agent.chat(message="问题一", session_key="s1", operator="张三")
        agent.llm.script = [text_answer("第二轮")]
        agent.chat(message="问题二", session_key="s1", operator="张三")
        history = agent.llm.calls[-1]["messages"]
        self.assertEqual("system", history[0]["role"])
        self.assertEqual("user", history[1]["role"])
        self.assertEqual("问题一", history[1]["content"])
        self.assertEqual("问题二", history[-1]["content"])
        self.assertEqual(first["sessionId"], agent.sessions.ensure("web", "s1")["id"])

    def test_session_is_isolated_by_channel_and_key(self):
        agent = self.runner([text_answer("a"), text_answer("b")])
        web = agent.chat(message="问", session_key="same", operator="张三", channel="web")
        ding = agent.chat(message="问", session_key="same", operator="张三", channel="dingtalk")
        self.assertNotEqual(web["sessionId"], ding["sessionId"])

    def test_disabled_agent_refuses(self):
        agent = self.runner([text_answer("x")], enabled=False)
        with self.assertRaisesRegex(AgentDisabled, "未启用"):
            agent.chat(message="问", session_key="s1", operator="张三")

    def test_blank_message_is_rejected(self):
        agent = self.runner([text_answer("x")])
        with self.assertRaisesRegex(ValueError, "不能为空"):
            agent.chat(message="   ", session_key="s1", operator="张三")

    def test_run_is_audited(self):
        agent = self.runner([text_answer("答案")])
        agent.chat(message="问题", session_key="s1", operator="张三")
        run = self.audit.recent_runs()[0]
        self.assertEqual("ok", run["status"])
        self.assertEqual("问题", run["request"])
        self.assertEqual("答案", run["reply"])
        self.assertEqual("张三", run["operator"])


class ConfirmFlowTests(AgentTestCase):
    def start(self):
        agent = self.runner([tool_answer("make_file", {"po_id": "604264"}),
                             text_answer("要点如上，请确认")])
        answer = agent.chat(message="给 604264 生成合同", session_key="s1", operator="张三")
        return agent, answer, answer["pendingActions"][0]

    def test_l1_tool_does_not_execute_before_confirmation(self):
        agent, answer, action = self.start()
        self.assertEqual([], self.executed)
        self.assertEqual("生成 604264", action["title"])
        self.assertEqual("604264", action["preview"]["po"])
        self.assertEqual("一次真实生成", action["preview"]["willCost"])
        self.assertEqual({"po_id": "604264"}, action["preview"]["arguments"])
        self.assertEqual("awaiting_confirm", self.audit.recent_tools()[0]["status"])
        tool_reply = json.loads(agent.llm.calls[1]["messages"][-1]["content"])
        self.assertEqual("awaiting_confirm", tool_reply["status"])

    def test_undeclared_arguments_are_dropped_before_pending_action(self):
        agent = self.runner([
            tool_answer("make_file", {
                "po_id": "604264",
                "price_overrides": {"SKU": -1},
                "evil": 1,
            }),
            text_answer("请确认"),
        ])
        answer = agent.chat(message="生成", session_key="s1", operator="张三")
        action = self.actions.get(answer["pendingActions"][0]["id"])
        self.assertEqual({"po_id": "604264"}, action["arguments"])
        self.assertEqual({"po_id": "604264"}, action["preview"]["arguments"])
        self.assertNotIn("price_overrides", action["arguments"])
        executed = agent.confirm(action["id"], "张三")
        self.assertEqual("executed", executed["status"])
        self.assertEqual({"po_id": "604264"}, self.executed[0]["args"])

    def test_confirmation_executes_once_and_replays_result(self):
        agent, _, action = self.start()
        executed = agent.confirm(action["id"], "张三")
        self.assertEqual("executed", executed["status"])
        self.assertEqual(1, len(self.executed))
        self.assertEqual(action["id"], self.executed[0]["actionId"])
        again = agent.confirm(action["id"], "张三")
        self.assertEqual("executed", again["status"])
        self.assertEqual(1, len(self.executed))
        self.assertEqual(executed["result"], again["result"])

    def test_only_the_initiator_can_confirm(self):
        agent, _, action = self.start()
        with self.assertRaisesRegex(ActionError, "发起该动作的员工"):
            agent.confirm(action["id"], "李四")
        self.assertEqual([], self.executed)

    def test_empty_operator_cannot_confirm(self):
        agent, _, action = self.start()
        with self.assertRaisesRegex(ActionError, "操作人姓名"):
            agent.confirm(action["id"], "")
        with self.assertRaisesRegex(ActionError, "操作人姓名"):
            agent.confirm(action["id"], "   ")
        self.assertEqual([], self.executed)

    def test_parenthetical_alias_can_confirm(self):
        agent, _, action = self.start()
        executed = agent.confirm(action["id"], "张三（小张）")
        self.assertEqual("executed", executed["status"])
        self.assertEqual(1, len(self.executed))

    def test_empty_operator_cannot_cancel(self):
        agent, _, action = self.start()
        with self.assertRaisesRegex(ActionError, "操作人姓名"):
            agent.cancel(action["id"], "")
        self.assertEqual("pending", self.actions.get(action["id"])["status"])

    def test_cancelled_action_cannot_be_executed(self):
        agent, _, action = self.start()
        self.assertEqual("cancelled", agent.cancel(action["id"], "张三")["status"])
        with self.assertRaisesRegex(ActionError, "不能再执行"):
            agent.confirm(action["id"], "张三")
        self.assertEqual([], self.executed)

    def test_expired_action_cannot_be_executed(self):
        self.actions.ttl_seconds = -1
        agent, _, action = self.start()
        with self.assertRaises(ActionError):
            agent.confirm(action["id"], "张三")
        self.assertEqual([], self.executed)
        self.assertEqual("expired", self.actions.get(action["id"])["status"])

    def test_failed_execution_marks_the_action_failed(self):
        agent = self.runner([tool_answer("make_file", {"po_id": "boom"}), text_answer("请确认")])
        answer = agent.chat(message="生成", session_key="s1", operator="张三")
        action_id = answer["pendingActions"][0]["id"]

        def failing(arguments, ctx):
            raise RuntimeError("Node 没装")

        agent.registry = ToolRegistry()
        agent.registry.register(Tool(
            name="make_file", description="生成产物", parameters={"type": "object", "properties": {}},
            risk="L1", handler=failing, preview=lambda args, ctx: {},
        ))
        with self.assertRaisesRegex(RuntimeError, "Node 没装"):
            agent.confirm(action_id, "张三")
        self.assertEqual("failed", self.actions.get(action_id)["status"])
        self.assertEqual([], self.executed)

    def test_pending_list_only_shows_open_actions(self):
        agent, answer, action = self.start()
        self.assertEqual([action["id"]], [item["id"] for item in agent.pending()])
        agent.confirm(action["id"], "张三")
        self.assertEqual([], agent.pending())

    def test_result_is_appended_to_the_session_transcript(self):
        agent, answer, action = self.start()
        agent.confirm(action["id"], "张三")
        transcript = self.sessions.transcript(answer["sessionId"])
        self.assertTrue(any("已执行" in item["content"] for item in transcript))


class RegistryTests(unittest.TestCase):
    def test_default_registry_reflects_enabled_subsystems(self):
        full = build_registry(with_forecast=True, with_exchange=True, with_notifier=True)
        self.assertIn("forecast_demand", full.names())
        self.assertIn("search_sales_orders", full.names())
        self.assertIn("get_sales_order_items", full.names())
        self.assertIn("submit_exchange_dry_run", full.names())
        self.assertIn("send_delivery_reminder", full.names())
        lean = build_registry(with_forecast=False, with_exchange=False, with_notifier=False)
        self.assertNotIn("forecast_demand", lean.names())
        self.assertNotIn("search_sales_orders", lean.names())
        self.assertNotIn("send_delivery_reminder", lean.names())
        self.assertIn("delivery_reminders", lean.names())
        self.assertIn("gb_catalog_status", lean.names())
        self.assertIn("lookup_gb_standards", lean.names())
        self.assertIn("master_data_gaps", full.names())
        self.assertIn("master_data_gaps", lean.names())

    def test_system_prompt_requires_exchange_disambiguation_and_two_confirmations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStore(Path(tmp) / "prompt.sqlite3")
            runner = AgentRunner(
                registry=build_registry(with_forecast=False, with_exchange=True, with_notifier=False),
                llm=FakeLLM([]), sessions=SessionStore(store),
                actions=PendingActions(store), audit=AuditLog(store),
                context=ToolContext(env_path="unused.env", root=Path(tmp), fetch_rows=lambda year=None: ([], {})),
            )
            prompt = runner.system_prompt("张三", "web")
        self.assertIn("把/将 A 换成 B", prompt)
        self.assertIn("二次确认", prompt)
        self.assertIn("不得自行定义", prompt)
        self.assertIn("不是采购催办", prompt)
        self.assertIn("SKU 替换", prompt)
        self.assertIn("lookup_gb_standards", prompt)
        self.assertIn("执行标准", prompt)
        self.assertIn("gb_catalog_status", prompt)
        self.assertIn("master_data_gaps", prompt)

    def test_risk_levels_and_confirmation_requirements(self):
        registry = build_registry()
        by_name = {item["name"]: item for item in registry.catalog()}
        self.assertEqual("L0", by_name["delivery_reminders"]["risk"])
        self.assertFalse(by_name["delivery_reminders"]["needsConfirm"])
        self.assertEqual("L0", by_name["gb_catalog_status"]["risk"])
        self.assertFalse(by_name["gb_catalog_status"]["needsConfirm"])
        self.assertEqual("L0", by_name["lookup_gb_standards"]["risk"])
        self.assertFalse(by_name["lookup_gb_standards"]["needsConfirm"])
        self.assertEqual("L0", by_name["master_data_gaps"]["risk"])
        self.assertFalse(by_name["master_data_gaps"]["needsConfirm"])
        self.assertEqual("L1", by_name["generate_purchase_contract"]["risk"])
        self.assertTrue(by_name["generate_purchase_contract"]["needsConfirm"])
        self.assertEqual("L2", by_name["send_delivery_reminder"]["risk"])
        self.assertTrue(by_name["send_delivery_reminder"]["needsConfirm"])

    def test_schemas_are_openai_function_shaped(self):
        for schema in build_registry().schemas():
            self.assertEqual("function", schema["type"])
            self.assertIn("name", schema["function"])
            self.assertEqual("object", schema["function"]["parameters"]["type"])

    def test_duplicate_and_unknown_risk_are_rejected(self):
        registry = ToolRegistry()
        tool = Tool(name="x", description="", parameters={"type": "object"}, risk="L0",
                    handler=lambda args, ctx: {})
        registry.register(tool)
        with self.assertRaisesRegex(ValueError, "已注册"):
            registry.register(tool)
        with self.assertRaisesRegex(ValueError, "未知风险级"):
            registry.register(Tool(name="y", description="", parameters={"type": "object"},
                                   risk="L9", handler=lambda args, ctx: {}))

    def test_lookup_gb_standards_requires_an_anchor(self):
        ctx = ToolContext(env_path="unused.env", root=Path("."), fetch_rows=lambda year=None: ([], {}))
        tool = build_registry(with_forecast=False, with_exchange=False, with_notifier=False).get(
            "lookup_gb_standards",
        )
        with self.assertRaisesRegex(ToolError, "请提供"):
            tool.handler({}, ctx)


class ConfigTests(unittest.TestCase):
    def test_flag_parsing(self):
        for value in ("true", "TRUE", "1", "yes", "on", "enabled"):
            self.assertTrue(flag(value))
        for value in ("false", "0", "no", "", None, "maybe"):
            self.assertFalse(flag(value))
        self.assertTrue(flag("", True))

    def test_llm_endpoint_normalisation(self):
        self.assertEqual("https://api.deepseek.com/v1/chat/completions",
                         LLMClient(api_base="https://api.deepseek.com", api_key="k", model="m").endpoint)
        self.assertEqual("https://x/v1/chat/completions",
                         LLMClient(api_base="https://x/v1/", api_key="k", model="m").endpoint)
        self.assertEqual("https://x/v1/chat/completions",
                         LLMClient(api_base="https://x/v1/chat/completions", api_key="k", model="m").endpoint)
        self.assertFalse(LLMClient(api_base="", api_key="", model="").configured)


GOLDEN_PATH = Path(__file__).parent / "fixtures" / "golden_dialogues.json"


def _argument_subset(actual, expected) -> bool:
    for key, value in expected.items():
        if actual.get(key) != value:
            return False
    return True


class GoldenReplayTests(AgentTestCase):
    """假 LLM 按黄金夹具脚本吐 tool_calls，断言 runner 入参与 L1/L2 pending。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        cls.live = build_registry(with_forecast=True, with_exchange=True, with_notifier=True)

    def _stub_registry(self, names):
        registry = ToolRegistry()
        invoked = []
        for name in names:
            live = self.live.get(name)
            registry.register(Tool(
                name=name, description=live.description, parameters=live.parameters,
                risk=live.risk,
                handler=lambda args, ctx, n=name: invoked.append({"name": n, "arguments": args}) or {
                    "ok": True, "tool": n, "arguments": args,
                },
                preview=lambda args, ctx: {"stub": True},
                title=lambda args, n=name: n,
            ))
        return registry, invoked

    def test_fixture_is_well_formed(self):
        ids = []
        for case in self.payload["cases"]:
            case_id = case["id"]
            ids.append(case_id)
            expect = case["expect"]
            kind = expect["kind"]
            self.assertIn(kind, ("tools", "ask", "refuse"), case_id)
            tools = expect.get("tools") or []
            must_not = expect.get("must_not") or []
            if kind == "tools":
                self.assertTrue(tools, case_id)
            for item in tools:
                live = self.live.get(item["name"])
                properties = (live.parameters or {}).get("properties") or {}
                for key in (item.get("arguments") or {}):
                    self.assertIn(key, properties, f"{case_id} 入参 {key} 不在 {item['name']} schema")
                if expect.get("pending"):
                    self.assertTrue(live.needs_confirm, f"{case_id} pending 但 {item['name']} 不是 L1/L2")
            for name in must_not:
                self.live.get(name)
            overlap = {item["name"] for item in tools} & set(must_not)
            self.assertEqual(set(), overlap, case_id)
        self.assertEqual(len(ids), len(set(ids)))
        missing_oid = next(item for item in self.payload["cases"] if item["id"] == "exc-handle-missing-oid")
        self.assertNotIn(
            "submit_exchange_dry_run",
            [item["name"] for item in missing_oid["expect"]["tools"]],
        )
        self.assertIn("submit_exchange_dry_run", missing_oid["expect"]["must_not"])

    def test_scripted_replay(self):
        for case in self.payload["cases"]:
            with self.subTest(case["id"]):
                expect = case["expect"]
                names = [item["name"] for item in expect.get("tools") or []]
                names.extend(expect.get("must_not") or [])
                if not names:
                    names = ["delivery_reminders"]
                registry, invoked = self._stub_registry(sorted(set(names)))
                if expect["kind"] == "tools":
                    script = [
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {
                                    "name": item["name"],
                                    "arguments": json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                                },
                            } for index, item in enumerate(expect["tools"], 1)],
                        },
                        text_answer("要点如上" if expect.get("pending") else "查到了"),
                    ]
                else:
                    script = [text_answer(expect.get("reason") or "需要补充信息后再办。")]
                answer = self.runner(script, registry=registry).chat(
                    message=case["utterance"], session_key=case["id"], operator="利特",
                )
                invoked_names = [item["name"] for item in invoked]
                for name in expect.get("must_not") or []:
                    self.assertNotIn(name, invoked_names)
                    self.assertNotIn(name, [step["tool"] for step in answer["steps"]])
                if expect["kind"] != "tools":
                    self.assertEqual([], answer["steps"])
                    self.assertEqual([], answer["pendingActions"])
                    continue
                recorded = list(invoked)
                for action in answer["pendingActions"]:
                    row = self.actions.get(action["id"])
                    recorded.append({"name": row["tool"], "arguments": row["arguments"]})
                expected_names = [item["name"] for item in expect["tools"]]
                self.assertEqual(expected_names, [item["name"] for item in recorded])
                for item, call in zip(expect["tools"], recorded):
                    self.assertTrue(
                        _argument_subset(call["arguments"], item.get("arguments") or {}),
                        f"{case['id']} {item['name']} 入参不足 {item.get('arguments')}",
                    )
                if expect.get("pending"):
                    self.assertTrue(answer["pendingActions"])
                else:
                    self.assertEqual([], answer["pendingActions"])


class WebStaffBindingTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.directory = StaffDirectory(self.store)
        self.directory.upsert("利特", dingtalk_user_id="u-lite", aliases=["李佳冬（利特）"])

    def test_web_l1_refuses_unbound_operator(self):
        agent = self.runner(
            [tool_answer("make_file", {"po_id": "604264"}), text_answer("请确认")],
            directory=self.directory,
        )
        answer = agent.chat(message="生成合同", session_key="s1", operator="张三", channel="web")
        self.assertEqual([], self.executed)
        self.assertEqual([], answer["pendingActions"])
        self.assertEqual("error", answer["steps"][0]["status"])
        self.assertEqual(
            WEB_OPERATOR_UNBOUND,
            json.loads(agent.llm.calls[1]["messages"][-1]["content"])["error"],
        )

    def test_web_l1_allows_bound_alias(self):
        agent = self.runner(
            [tool_answer("make_file", {"po_id": "604264"}), text_answer("请确认")],
            directory=self.directory,
        )
        answer = agent.chat(
            message="生成合同", session_key="s2", operator="李佳冬（利特）", channel="web",
        )
        self.assertEqual(1, len(answer["pendingActions"]))
        self.assertEqual([], self.executed)

    def test_web_l0_still_runs_when_unbound(self):
        agent = self.runner(
            [tool_answer("read_orders", {"query": "604264"}), text_answer("查到了")],
            directory=self.directory,
        )
        answer = agent.chat(message="查单", session_key="s3", operator="张三", channel="web")
        self.assertEqual("查到了", answer["reply"])
        self.assertEqual([], answer["pendingActions"])

    def test_dingtalk_skips_web_staff_check(self):
        agent = self.runner(
            [tool_answer("make_file", {"po_id": "604264"}), text_answer("请确认")],
            directory=self.directory,
        )
        answer = agent.chat(
            message="生成合同", session_key="s4", operator="张三",
            channel="dingtalk", actor_id="u-lite",
        )
        self.assertEqual(1, len(answer["pendingActions"]))

    def test_empty_bindings_fail_closed_on_web(self):
        empty = StaffDirectory(AgentStore(Path(self.tmp.name) / "empty.sqlite3"))
        agent = self.runner(
            [tool_answer("make_file", {"po_id": "1"}), text_answer("请确认")],
            directory=empty,
        )
        answer = agent.chat(message="生成", session_key="s5", operator="利特", channel="web")
        self.assertEqual([], answer["pendingActions"])

    def test_web_confirm_requires_binding(self):
        agent = self.runner(
            [tool_answer("make_file", {"po_id": "604264"}), text_answer("请确认")],
            directory=self.directory,
        )
        answer = agent.chat(message="生成合同", session_key="s6", operator="利特", channel="web")
        action_id = answer["pendingActions"][0]["id"]
        with self.assertRaisesRegex(ActionError, "员工绑定表"):
            agent.confirm(action_id, "张三", channel="web")
        executed = agent.confirm(action_id, "利特", channel="web")
        self.assertEqual("executed", executed["status"])
        self.assertEqual(1, len(self.executed))


class MasterDataGapsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = self.root / "config"
        images = config / "product-images"
        images.mkdir(parents=True)
        (images / "SKU-HAS.png").write_bytes(b"png")
        (config / "suppliers.json").write_text(json.dumps({"甲": {"name": "甲公司"}}, ensure_ascii=False), encoding="utf-8")
        (config / "products.json").write_text(json.dumps({
            "SKU-HAS": {
                "name": "有图有价",
                "prices": {"no_invoice": 1, "normal_invoice": 1, "special_invoice": 1},
            },
            "SKU-NOPRICE": {
                "name": "缺专票",
                "prices": {"no_invoice": 1, "normal_invoice": 1, "special_invoice": None},
            },
        }, ensure_ascii=False), encoding="utf-8")
        (config / "gb_category_map.json").write_text(json.dumps({
            "ignore": ["其他"],
            "families": {"服装": {"label": "服装", "ccs": [], "ics": [], "keywords": []}},
            "categories": {"衬衫": "服装"},
        }, ensure_ascii=False), encoding="utf-8")
        self.rows = [
            {"采购单号": "1", "采购日期": "2026-08-01", "采购员": "利特",
             "item_supplier_id": "甲", "商品编码": "SKU-HAS", "款式编码": "",
             "商品名称": "有图有价", "item_sku_other_3": "衬衫"},
            {"采购单号": "2", "采购日期": "2026-08-01", "采购员": "利特",
             "item_supplier_id": "乙", "商品编码": "SKU-NO", "款式编码": "",
             "商品名称": "无主数据", "item_sku_other_3": "毛绒"},
            {"采购单号": "3", "采购日期": "2026-08-02", "采购员": "利特",
             "item_supplier_id": "甲", "商品编码": "SKU-NOPRICE", "款式编码": "",
             "商品名称": "缺专票", "item_sku_other_3": "其他"},
            {"采购单号": "9", "采购日期": "2026-06-01", "采购员": "利特",
             "item_supplier_id": "丙", "商品编码": "SKU-OLD", "款式编码": "",
             "商品名称": "过窗", "item_sku_other_3": "毛绒"},
        ]
        self.ctx = ToolContext(
            env_path="unused.env", root=self.root,
            fetch_rows=lambda year=None: (self.rows, {"year": year}),
        )
        self.tool = build_registry(with_forecast=False, with_exchange=False, with_notifier=False).get(
            "master_data_gaps",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_summarises_recent_gaps(self):
        result = self.tool.handler({"days": 30, "today": "2026-08-13"}, self.ctx)
        self.assertEqual(3, result["rowCount"])
        self.assertEqual(["乙"], [item["name"] for item in result["missingSuppliers"]])
        self.assertEqual({"SKU-NO", "SKU-NOPRICE"}, {item["sku"] for item in result["missingImages"]})
        self.assertEqual({"SKU-NO", "SKU-NOPRICE"}, {item["sku"] for item in result["missingPrices"]})
        noprice = next(item for item in result["missingPrices"] if item["sku"] == "SKU-NOPRICE")
        self.assertEqual(["专票"], noprice["missingLabels"])
        self.assertEqual(["毛绒"], [item["category"] for item in result["unmappedCategories"]])
        self.assertIn("供应商未维护", result["markdown"])
        self.assertNotIn("SKU-OLD", result["markdown"])
        self.assertNotIn("丙", result["markdown"])

    def test_invoice_type_narrows_price_gaps(self):
        result = self.tool.handler(
            {"days": 30, "today": "2026-08-13", "invoice_type": "special_invoice"}, self.ctx,
        )
        self.assertEqual({"SKU-NO", "SKU-NOPRICE"}, {item["sku"] for item in result["missingPrices"]})
        normal = self.tool.handler(
            {"days": 30, "today": "2026-08-13", "invoice_type": "normal_invoice"}, self.ctx,
        )
        self.assertEqual(["SKU-NO"], [item["sku"] for item in normal["missingPrices"]])


class DeclaredArgumentsTests(unittest.TestCase):
    def test_drops_undeclared_fields(self):
        from backend.agent.tools import declared_arguments
        tool = Tool(
            name="x", description="",
            parameters={"type": "object", "properties": {"po_id": {"type": "string"}}},
            risk="L0", handler=lambda args, ctx: args,
        )
        self.assertEqual(
            {"po_id": "1"},
            declared_arguments(tool, {"po_id": "1", "price_overrides": {"A": 1}}),
        )

    def test_empty_properties_keeps_all(self):
        from backend.agent.tools import declared_arguments
        tool = Tool(
            name="x", description="",
            parameters={"type": "object", "properties": {}},
            risk="L0", handler=lambda args, ctx: args,
        )
        self.assertEqual({"query": "a"}, declared_arguments(tool, {"query": "a"}))

    def test_generate_contract_handler_does_not_forward_price_overrides(self):
        from unittest.mock import patch
        from backend.agent.tools import ToolContext, _generate_contract
        ctx = ToolContext(env_path="unused.env", root=Path(tempfile.gettempdir()),
                          fetch_rows=lambda year=None: ([], {}))
        with patch("backend.agent.tools.generate_contract") as generate:
            _generate_contract({
                "po_id": "604264",
                "invoice_type": "special_invoice",
                "price_overrides": {"SKU": 1},
            }, ctx)
        self.assertNotIn("price_overrides", generate.call_args.kwargs)


class SessionEpochTests(AgentTestCase):
    def test_idle_rotates_epoch_and_history_stays_in_current(self):
        self.sessions.idle_minutes = 1
        session = self.sessions.ensure("web", "k1", "张三")
        self.sessions.add_message(session["id"], "user", "昨天的合同")
        with self.store.write() as conn:
            conn.execute(
                "UPDATE agent_sessions SET updated_at=? WHERE id=?",
                ("2020-01-01T00:00:00+00:00", session["id"]),
            )
        again = self.sessions.ensure("web", "k1", "张三")
        self.assertEqual(1, again["epoch"])
        self.sessions.add_message(again["id"], "user", "今天催办")
        history = self.sessions.history(again["id"])
        self.assertEqual(["今天催办"], [item["content"] for item in history if item["role"] == "user"])
        transcript = self.sessions.transcript(again["id"])
        self.assertTrue(any(item["content"] == "昨天的合同" for item in transcript))

    def test_rotate_keeps_audit_and_isolates_history(self):
        session = self.sessions.ensure("web", "k2", "张三")
        self.sessions.add_message(session["id"], "user", "旧话题")
        self.sessions.rotate(session["id"])
        self.sessions.add_message(session["id"], "user", "新话题")
        self.assertEqual(["新话题"], [item["content"] for item in self.sessions.history(session["id"])])

    def test_dangling_tool_calls_are_dropped(self):
        session = self.sessions.ensure("web", "k3", "张三")
        self.sessions.add_message(session["id"], "user", "查一下")
        self.sessions.add_message(
            session["id"], "assistant", "",
            tool_calls=[{"id": "call-missing", "function": {"name": "read_orders"}}],
        )
        history = self.sessions.history(session["id"])
        self.assertEqual(["user"], [item["role"] for item in history])
        self.assertFalse(any(item.get("tool_calls") for item in history))


class ContextBudgetTests(AgentTestCase):
    def test_old_tool_results_are_compressed_and_envelope_is_json(self):
        from backend.agent.sessions import encode_tool_result
        envelope = json.loads(encode_tool_result("X" * 20000, limit=80))
        self.assertTrue(envelope["truncated"])
        self.assertIn("preview", envelope)

        self.sessions.char_budget = 800
        session = self.sessions.ensure("web", "k4", "张三")
        self.sessions.add_message(session["id"], "user", "第一问")
        self.sessions.add_message(
            session["id"], "assistant", "",
            tool_calls=[{"id": "c1", "function": {"name": "read_orders"}}],
        )
        self.sessions.add_message(
            session["id"], "tool", "Y" * 400, name="read_orders", tool_call_id="c1",
        )
        self.sessions.add_message(session["id"], "user", "第二问")
        packed = self.sessions.context_messages(session["id"], system="sys")
        old_tool = next(item for item in packed if item.get("role") == "tool")
        payload = json.loads(old_tool["content"])
        self.assertTrue(payload["truncated"])
        self.assertEqual("user", packed[1]["role"])

    def test_summary_trigger_writes_incrementally(self):
        session = self.sessions.ensure("web", "k5", "张三")
        for index in range(6):
            self.sessions.add_message(session["id"], "user", f"问{index}")
            self.sessions.add_message(session["id"], "assistant", f"答{index}")
        agent = self.runner(
            [text_answer("本轮"), text_answer("摘要正文")],
            summary_enabled=True, summary_trigger=4, summary_keep=2,
        )
        agent.chat(message="再问一次", session_key="k5", operator="张三")
        self.assertTrue(self.sessions.latest_summary(session["id"]))


class MemoryTests(AgentTestCase):
    def test_remember_inject_forget_and_unbound_skips(self):
        from backend.agent.memories import OperatorMemories
        memories = OperatorMemories(self.store, enabled=True)
        memories.remember("利特", "利特负责佰特和佳裕")
        with self.assertRaisesRegex(ValueError, "数字"):
            memories.remember("利特", "上次在办 604264")
        directory = StaffDirectory(self.store)
        directory.upsert("利特", dingtalk_user_id="u-lite")
        agent = self.runner([text_answer("好"), text_answer("也好")],
                            memories=memories, directory=directory)
        agent.chat(message="你好", session_key="mem-1", operator="利特")
        texts = [item["content"] for item in agent.llm.calls[0]["messages"] if item["role"] == "system"]
        self.assertTrue(any("佰特" in text for text in texts))
        agent.chat(message="你好", session_key="mem-2", operator="路人")
        unbound = [item["content"] for item in agent.llm.calls[1]["messages"] if item["role"] == "system"]
        self.assertFalse(any("佰特" in text for text in unbound))
        forgotten = memories.forget("利特", "佰特")
        self.assertEqual(1, len(forgotten))
        self.assertEqual([], memories.list_active("利特"))


class RetentionTests(AgentTestCase):
    def test_maintenance_deletes_old_rows_and_keeps_recent(self):
        from backend.agent.maintenance import MaintenanceScheduler
        session = self.sessions.ensure("web", "old", "张三")
        self.sessions.add_message(session["id"], "user", "该留")
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO agent_messages
                   (session_id, role, content, created_at, epoch)
                   VALUES (?, 'user', '该删', '2020-01-01T00:00:00', 0)""",
                (session["id"],),
            )
        out = Path(self.tmp.name) / "outputs" / "generated"
        out.mkdir(parents=True)
        stale = out / "old.xlsx"
        stale.write_text("x", encoding="utf-8")
        import os
        os.utime(stale, (1_000_000_000, 1_000_000_000))
        maint = MaintenanceScheduler(store=self.store, root=Path(self.tmp.name),
                                     retention_days=30, output_days=30, poll_seconds=60)
        result = maint.run_once()
        self.assertGreaterEqual(result["deleted"]["agent_messages"], 1)
        remaining = [item["content"] for item in self.sessions.transcript(session["id"])]
        self.assertIn("该留", remaining)
        self.assertNotIn("该删", remaining)
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()

