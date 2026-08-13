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
            name="make_file", description="生成产物", parameters={"type": "object", "properties": {}},
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
        return AgentRunner(
            registry=self.registry, llm=FakeLLM(script), sessions=self.sessions,
            actions=self.actions, audit=self.audit, context=self.context,
            **{"max_steps": 4, "enabled": True, **kwargs},
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
        self.assertEqual({"po": "604264", "willCost": "一次真实生成"}, action["preview"])
        self.assertEqual("awaiting_confirm", self.audit.recent_tools()[0]["status"])
        tool_reply = json.loads(agent.llm.calls[1]["messages"][-1]["content"])
        self.assertEqual("awaiting_confirm", tool_reply["status"])

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

    def test_parenthetical_alias_can_confirm(self):
        agent, _, action = self.start()
        executed = agent.confirm(action["id"], "张三（小张）")
        self.assertEqual("executed", executed["status"])
        self.assertEqual(1, len(self.executed))

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
            risk="L1", handler=failing,
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

    def test_risk_levels_and_confirmation_requirements(self):
        registry = build_registry()
        by_name = {item["name"]: item for item in registry.catalog()}
        self.assertEqual("L0", by_name["delivery_reminders"]["risk"])
        self.assertFalse(by_name["delivery_reminders"]["needsConfirm"])
        self.assertEqual("L0", by_name["gb_catalog_status"]["risk"])
        self.assertFalse(by_name["gb_catalog_status"]["needsConfirm"])
        self.assertEqual("L0", by_name["lookup_gb_standards"]["risk"])
        self.assertFalse(by_name["lookup_gb_standards"]["needsConfirm"])
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


if __name__ == "__main__":
    unittest.main()
