# -*- coding: utf-8 -*-
"""ChatGPT / Codex OAuth：走本机订阅额度，不走 API 按量计费。

和 OpenClaw 同一条路：读 Codex CLI 留下的 `~/.codex/auth.json`，用里面的
access/refresh token 打 `https://chatgpt.com/backend-api/codex/responses`。
这不是 `api.openai.com` 的 API Key；ChatGPT OAuth token 打官方 API 会 401。

刷新写回同一个文件（文件锁），避免和 Cursor / Codex CLI 各刷一次把对方踢下线。
额度是 ChatGPT 计划里的 Codex 窗口（常见是 5 小时 + 每周），用尽只能等重置或改回
`openai_compatible`。
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_ORIGINATOR = "codex_cli_rs"
# Codex CLI 公开的 ChatGPT OAuth client_id；JWT 里有就优先用 JWT 的。
FALLBACK_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def ssl_context():
    """macOS 官方 Python 经常缺系统 CA；仓库已经依赖 certifi。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class CodexAuthError(RuntimeError):
    """OAuth 文件或刷新失败；消息可以回给员工，不含 token。"""


def default_auth_path() -> Path:
    override = str(os.environ.get("CODEX_HOME") or "").strip()
    root = Path(override).expanduser() if override else Path.home() / ".codex"
    return root / "auth.json"


def _b64url_json(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def jwt_claims(token: str) -> dict:
    token = str(token or "").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = _b64url_json(parts[1])
    except (ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _account_id_from_token(access_token: str, stored: str = "") -> str:
    if stored:
        return stored
    claims = jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth") or {}
    if isinstance(auth, dict):
        for key in ("chatgpt_account_id", "account_id"):
            value = str(auth.get(key) or "").strip()
            if value:
                return value
    return str(claims.get("chatgpt_account_id") or "").strip()


def _client_id_from_token(access_token: str) -> str:
    claims = jwt_claims(access_token)
    return str(claims.get("client_id") or "").strip() or FALLBACK_CLIENT_ID


def _token_expires_at(access_token: str, fallback: float = 0.0) -> float:
    claims = jwt_claims(access_token)
    try:
        return float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return fallback


class CodexAuth:
    """读写 Codex CLI 的 auth.json，过期就 refresh 并写回。"""

    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path else default_auth_path()

    def status(self) -> dict:
        try:
            payload = self._read()
        except CodexAuthError as exc:
            return {"configured": False, "path": str(self.path), "error": str(exc)}
        tokens = payload.get("tokens") or {}
        access = str(tokens.get("access_token") or "")
        expires = _token_expires_at(access)
        return {
            "configured": bool(access and tokens.get("refresh_token")),
            "path": str(self.path),
            "authMode": payload.get("auth_mode") or "",
            "expiresAt": datetime.fromtimestamp(expires, timezone.utc).isoformat() if expires else "",
            "expired": bool(expires and expires <= time.time() + 60),
        }

    def credentials(self) -> tuple[str, str]:
        """返回 (access_token, account_id)；过期则刷新。"""
        with self._lock() as lock:
            payload = lock.payload
            tokens = payload.get("tokens") or {}
            access = str(tokens.get("access_token") or "").strip()
            refresh = str(tokens.get("refresh_token") or "").strip()
            account_id = _account_id_from_token(access, str(tokens.get("account_id") or "").strip())
            if not access or not refresh:
                raise CodexAuthError(f"未找到 Codex OAuth token（{self.path}）。请先在本机登录 ChatGPT / Codex。")
            expires = _token_expires_at(access)
            if expires and expires > time.time() + 60:
                return access, account_id
            refreshed = self._refresh(refresh, _client_id_from_token(access))
            new_access = str(refreshed.get("access_token") or "").strip()
            if not new_access:
                raise CodexAuthError("Codex OAuth 刷新成功但没有 access_token")
            new_refresh = str(refreshed.get("refresh_token") or refresh).strip()
            new_account = _account_id_from_token(new_access, account_id)
            payload["tokens"] = {
                **tokens,
                "access_token": new_access,
                "refresh_token": new_refresh,
                "id_token": refreshed.get("id_token") or tokens.get("id_token") or "",
                "account_id": new_account,
            }
            payload["last_refresh"] = datetime.now(timezone.utc).isoformat()
            lock.write(payload)
            return new_access, new_account

    def list_models(self) -> list[str]:
        """当前 ChatGPT 账号在 Codex 后端能用的模型 id。"""
        access, account_id = self.credentials()
        request = urllib.request.Request(
            DEFAULT_BASE_URL.rstrip("/") + "/models?client_version=1.0.0",
            headers={
                "Authorization": f"Bearer {access}",
                "Accept": "application/json",
                "originator": DEFAULT_ORIGINATOR,
                **({"chatgpt-account-id": account_id} if account_id else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise CodexAuthError(f"拉 Codex 模型列表失败：{exc}") from exc
        models = body.get("models") if isinstance(body, dict) else body
        ids = []
        for item in models or []:
            if isinstance(item, str) and item not in ids:
                ids.append(item)
            elif isinstance(item, dict):
                value = str(item.get("id") or item.get("slug") or "").strip()
                if value and value not in ids:
                    ids.append(value)
        return ids

    def _read(self) -> dict:
        if not self.path.is_file():
            raise CodexAuthError(f"没有 Codex 登录文件：{self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexAuthError(f"读不了 Codex 登录文件：{exc}") from exc
        if not isinstance(payload, dict):
            raise CodexAuthError("Codex 登录文件格式不对")
        return payload

    def _write_unlocked(self, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _lock(self):
        return _AuthFileLock(self)

    def _refresh(self, refresh_token: str, client_id: str) -> dict:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }).encode("utf-8")
        request = urllib.request.Request(
            TOKEN_URL, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:240]
            raise CodexAuthError(
                f"Codex OAuth 刷新失败（{exc.code}）。请在本机重新登录 ChatGPT / Codex。"
            ) from exc
        except urllib.error.URLError as exc:
            raise CodexAuthError(f"Codex OAuth 刷新连不上：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise CodexAuthError("Codex OAuth 刷新返回的不是 JSON") from exc
        if not isinstance(payload, dict):
            raise CodexAuthError("Codex OAuth 刷新返回格式不对")
        return payload


class _AuthFileLock:
    def __init__(self, auth: CodexAuth):
        self.auth = auth
        self.payload = {}
        self._fh = None

    def __enter__(self):
        self.auth.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.auth.path.exists():
            raise CodexAuthError(f"没有 Codex 登录文件：{self.auth.path}")
        self._fh = open(self.auth.path, "r+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        self._fh.seek(0)
        try:
            payload = json.loads(self._fh.read() or "{}")
        except json.JSONDecodeError as exc:
            raise CodexAuthError("Codex 登录文件格式不对") from exc
        if not isinstance(payload, dict):
            raise CodexAuthError("Codex 登录文件格式不对")
        self.payload = payload
        return self

    def write(self, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self._fh.seek(0)
        self._fh.write(text)
        self._fh.truncate()
        self._fh.flush()
        os.fsync(self._fh.fileno())
        try:
            os.chmod(self.auth.path, 0o600)
        except OSError:
            pass

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
            self._fh.close()


def chat_messages_to_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """把 runner 的 OpenAI chat 消息拆成 Responses 的 instructions + input。"""
    instructions = []
    items = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "system":
            text = str(message.get("content") or "").strip()
            if text:
                instructions.append(text)
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or message.get("id") or ""),
                "output": str(message.get("content") or ""),
            })
            continue
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(function.get("name") or call.get("name") or ""),
                    "arguments": function.get("arguments")
                    if isinstance(function.get("arguments"), str)
                    else json.dumps(function.get("arguments") or {}, ensure_ascii=False),
                })
            text = str(message.get("content") or "")
            if text:
                items.append({"role": "assistant", "content": text})
            continue
        items.append({"role": "user" if role == "user" else role,
                      "content": str(message.get("content") or "")})
    return "\n\n".join(instructions), items


def chat_tools_to_responses(tools: list[dict] | None) -> list[dict]:
    converted = []
    for tool in tools or []:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool["function"]
            converted.append({
                "type": "function",
                "name": function.get("name") or "",
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            })
        elif tool.get("type") == "function":
            converted.append(tool)
    return converted


def parse_responses_output(output) -> dict:
    """把 Responses `output` 数组收成 runner 认识的 assistant 消息。"""
    texts = []
    tool_calls = []
    for item in output or []:
        kind = str(item.get("type") or "")
        if kind == "message":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    texts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    texts.append(block)
        elif kind == "function_call":
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {}, ensure_ascii=False)
            tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or ""),
                "type": "function",
                "function": {"name": str(item.get("name") or ""), "arguments": arguments},
            })
        elif kind == "reasoning":
            for block in item.get("summary") or []:
                if isinstance(block, dict) and block.get("text"):
                    texts.append(str(block.get("text")))
        elif kind in ("output_text", "text") and item.get("text"):
            texts.append(str(item.get("text")))
    return {
        "role": "assistant",
        "content": "".join(texts),
        "tool_calls": tool_calls,
    }


def collect_sse_response(raw: bytes) -> dict:
    """从 Responses SSE 里取出最终 response 对象。"""
    text = raw.decode("utf-8", "replace")
    completed = None
    failed = None
    for chunk in text.split("\n\n"):
        data_lines = []
        event_name = ""
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        payload_text = "\n".join(data_lines)
        if payload_text == "[DONE]":
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type") or event_name
        if kind == "response.completed":
            completed = payload.get("response") or payload
        elif kind in ("response.failed", "error"):
            failed = payload
        elif payload.get("status") == "completed" and payload.get("output") is not None:
            completed = payload
    if failed:
        error = failed.get("error") or failed.get("response") or failed
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or "Codex 返回失败"
        else:
            message = str(error)
        raise CodexAuthError(str(message))
    if not completed:
        raise CodexAuthError("Codex 没有返回完整回复（SSE 里没有 response.completed）")
    return completed
