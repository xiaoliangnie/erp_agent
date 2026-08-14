# -*- coding: utf-8 -*-
"""模型客户端：默认 OpenAI 兼容 chat/completions，可选 Codex OAuth。

`AGENT_PROVIDER=openai_compatible`（缺省）：`AGENT_API_BASE` + `AGENT_API_KEY`，
DeepSeek / 通义 / Kimi / 内网模型只改这两项和 `AGENT_MODEL`。

`AGENT_PROVIDER=codex_oauth`：复用本机 ChatGPT 登录（`~/.codex/auth.json`），走
`chatgpt.com/backend-api/codex/responses`，消耗 Codex 订阅额度。和 OpenClaw 同一条
通道，不是 `api.openai.com` 的按量 API Key。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

from .codex_oauth import (
    DEFAULT_BASE_URL as CODEX_BASE_URL,
    DEFAULT_ORIGINATOR,
    CodexAuth,
    CodexAuthError,
    chat_messages_to_input,
    chat_tools_to_responses,
    collect_sse_response,
    default_auth_path,
    parse_responses_output,
    ssl_context,
)


class LLMError(RuntimeError):
    """模型调用失败；错误信息可以回给员工，但不含密钥。"""


class LLMClient:
    def __init__(self, *, api_base: str, api_key: str, model: str,
                 temperature: float = 0.1, timeout: int = 60,
                 provider: str = "openai_compatible", auth_file: str = "",
                 originator: str = ""):
        self.provider = str(provider or "openai_compatible").strip() or "openai_compatible"
        self.api_base = str(api_base or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.temperature = float(temperature)
        self.timeout = int(timeout)
        self.originator = str(originator or "").strip() or DEFAULT_ORIGINATOR
        self.auth_file = str(auth_file or "").strip()
        self._codex_auth = CodexAuth(self.auth_file or default_auth_path()) if self.provider == "codex_oauth" else None

    @property
    def configured(self) -> bool:
        if not self.model:
            return False
        if self.provider == "codex_oauth":
            return bool(self._codex_auth and self._codex_auth.status().get("configured"))
        return bool(self.api_base and self.api_key)

    @property
    def endpoint(self) -> str:
        if self.provider == "codex_oauth":
            base = self.api_base or CODEX_BASE_URL
            return base.rstrip("/") + "/responses"
        base = self.api_base
        if base.endswith("/chat/completions"):
            return base
        if not base.endswith("/v1"):
            base += "/v1"
        return base + "/chat/completions"

    def status(self) -> dict:
        payload = {
            "configured": self.configured,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint if (self.api_base or self.provider == "codex_oauth") else "",
        }
        if self._codex_auth is not None:
            payload["codexAuth"] = self._codex_auth.status()
        return payload

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
             tool_choice: str = "auto") -> dict:
        """发一轮对话，返回 assistant 消息（可能带 tool_calls）。"""
        if not self.model:
            raise LLMError("尚未配置 AGENT_MODEL")
        if self.provider == "codex_oauth":
            return self._chat_codex(messages, tools=tools, tool_choice=tool_choice)
        return self._chat_openai_compatible(messages, tools=tools, tool_choice=tool_choice)

    def _chat_openai_compatible(self, messages, *, tools, tool_choice) -> dict:
        if not self.api_base or not self.api_key:
            raise LLMError("尚未配置 AGENT_API_BASE / AGENT_API_KEY / AGENT_MODEL")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        body = self._post(request, "模型接口")
        choices = body.get("choices") or []
        if not choices:
            raise LLMError("模型接口没有返回任何回复")
        message = choices[0].get("message") or {}
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "finishReason": choices[0].get("finish_reason") or "",
            "usage": body.get("usage") or {},
        }

    def _chat_codex(self, messages, *, tools, tool_choice) -> dict:
        if self._codex_auth is None:
            raise LLMError("Codex OAuth 未初始化")
        try:
            access_token, account_id = self._codex_auth.credentials()
        except CodexAuthError as exc:
            raise LLMError(str(exc)) from exc
        instructions, items = chat_messages_to_input(messages)
        payload = {
            "model": self.model,
            "store": False,
            "stream": True,
            "input": items,
            "tool_choice": tool_choice or "auto",
            "parallel_tool_calls": False,
        }
        if instructions:
            payload["instructions"] = instructions
        converted = chat_tools_to_responses(tools)
        if converted:
            payload["tools"] = converted
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/event-stream",
            "originator": self.originator,
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=ssl_context()) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code == 401:
                raise LLMError("Codex OAuth 未被接受，请在本机重新登录 ChatGPT / Codex。") from exc
            if exc.code == 429:
                raise LLMError(
                    "Codex 额度用尽（常见是 5 小时或每周窗口）。等重置，或把 AGENT_PROVIDER 改回 openai_compatible。"
                ) from exc
            hint = ""
            if exc.code == 400 and "not supported" in detail.lower() and self._codex_auth:
                try:
                    names = "、".join(self._codex_auth.list_models()[:12])
                    if names:
                        hint = f" 当前账号可用：{names}。"
                except CodexAuthError:
                    hint = " 本机 ChatGPT 登录下不要用已下线的 gpt-5.3-codex，改成 gpt-5.6-sol。"
            raise LLMError(f"Codex 接口返回 {exc.code}：{detail}{hint}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"Codex 接口连接失败：{exc.reason}") from exc
        try:
            completed = collect_sse_response(raw)
        except CodexAuthError as exc:
            raise LLMError(str(exc)) from exc
        parsed = parse_responses_output(completed.get("output") or [])
        parsed["finishReason"] = completed.get("status") or ""
        parsed["usage"] = completed.get("usage") or {}
        if not parsed["content"] and not parsed["tool_calls"]:
            raise LLMError(
                "Codex 返回了空回复。把 AGENT_MODEL 换成当前账号可用的模型（本机是 gpt-5.6-sol 这一档），"
                "或把 AGENT_PROVIDER 改回 openai_compatible。"
            )
        return parsed

    def _post(self, request, label: str) -> dict:
        last_error = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=ssl_context()) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                logger.warning("%s HTTP %s: %s", label, exc.code, detail)
                last_error = LLMError(f"{label}返回 {exc.code}")
                if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(0.8)
                    continue
                raise last_error from exc
            except urllib.error.URLError as exc:
                raise LLMError(f"{label}连接失败：{exc.reason}") from exc
            except json.JSONDecodeError as exc:
                raise LLMError(f"{label}返回的不是合法 JSON") from exc
        raise last_error or LLMError(f"{label}调用失败")
