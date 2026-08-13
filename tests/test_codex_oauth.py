# -*- coding: utf-8 -*-
"""Codex OAuth 报文转换与本地 auth.json 读取。不打 ChatGPT 网络。"""
import json
import tempfile
import unittest
from pathlib import Path

from backend.agent.codex_oauth import (
    CodexAuth,
    chat_messages_to_input,
    chat_tools_to_responses,
    collect_sse_response,
    parse_responses_output,
)
from backend.agent.llm import LLMClient


def _auth_file(folder: Path, *, access="aaa.bbb.ccc", refresh="refresh-token", account="acct"):
    path = folder / "auth.json"
    path.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access,
            "refresh_token": refresh,
            "account_id": account,
        },
    }), encoding="utf-8")
    return path


class ConversionTests(unittest.TestCase):
    def test_system_goes_to_instructions_and_tools_flatten(self):
        instructions, items = chat_messages_to_input([
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "查 604264"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "get_purchase_order", "arguments": '{"po_id":"604264"}'},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        ])
        self.assertEqual("你是助手", instructions)
        self.assertEqual("user", items[0]["role"])
        self.assertEqual("function_call", items[1]["type"])
        self.assertEqual("call-1", items[1]["call_id"])
        self.assertEqual("function_call_output", items[2]["type"])

        tools = chat_tools_to_responses([{
            "type": "function",
            "function": {"name": "get_purchase_order", "description": "查单",
                         "parameters": {"type": "object"}},
        }])
        self.assertEqual("function", tools[0]["type"])
        self.assertEqual("get_purchase_order", tools[0]["name"])
        self.assertNotIn("function", tools[0])

    def test_parse_output_and_sse(self):
        parsed = parse_responses_output([
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "先查单"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "好的"}]},
            {"type": "function_call", "call_id": "c1", "name": "delivery_reminders",
             "arguments": '{"bucket":"overdue"}'},
        ])
        self.assertIn("好的", parsed["content"])
        self.assertEqual("delivery_reminders", parsed["tool_calls"][0]["function"]["name"])

        raw = (
            "event: response.output_text.delta\n"
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"x\"}\n\n"
            "event: response.completed\n"
            "data: {\"type\":\"response.completed\",\"response\":{\"output\":"
            "[{\"type\":\"message\",\"content\":[{\"type\":\"output_text\",\"text\":\"完成\"}]}]}}\n\n"
        ).encode("utf-8")
        completed = collect_sse_response(raw)
        self.assertEqual("完成", parse_responses_output(completed["output"])["content"])


class AuthFileTests(unittest.TestCase):
    def test_status_and_client_configured_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _auth_file(Path(tmp))
            status = CodexAuth(path).status()
            self.assertTrue(status["configured"])
            client = LLMClient(
                api_base="", api_key="", model="gpt-5.3-codex",
                provider="codex_oauth", auth_file=str(path),
            )
            self.assertTrue(client.configured)
            self.assertTrue(client.endpoint.endswith("/responses"))
            self.assertEqual("codex_oauth", client.status()["provider"])

    def test_missing_file_is_not_configured(self):
        client = LLMClient(
            api_base="", api_key="", model="gpt-5.3-codex",
            provider="codex_oauth", auth_file="/tmp/no-such-codex-auth.json",
        )
        self.assertFalse(client.configured)

    def test_openai_compatible_still_needs_key(self):
        self.assertFalse(LLMClient(api_base="", api_key="", model="m").configured)
        self.assertTrue(LLMClient(api_base="https://api.deepseek.com", api_key="k", model="m").configured)


if __name__ == "__main__":
    unittest.main()
