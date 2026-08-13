# -*- coding: utf-8 -*-
"""Agent Core 的装配入口。

HTTP 服务、命令行调试和钉钉线程都用 `build_agent` 得到同一个 `AgentRunner`，
共用同一份工具注册表和同一套确认流。
"""
from pathlib import Path

from .actions import ActionError, PendingActions
from .audit import AuditLog
from .llm import LLMClient, LLMError
from .runner import AgentDisabled, AgentRunner
from .sessions import SessionStore
from .store import AgentStore
from .tools import RESERVED_TOOLS, RISK_LEVELS, Tool, ToolContext, ToolError, ToolRegistry, build_registry


__all__ = [
    "ActionError", "AgentDisabled", "AgentRunner", "AgentStore", "AuditLog", "LLMClient",
    "LLMError", "PendingActions", "RESERVED_TOOLS", "RISK_LEVELS", "SessionStore", "Tool",
    "ToolContext", "ToolError", "ToolRegistry", "agent_database_path", "build_agent",
    "build_registry", "flag",
]


def flag(value, default=False):
    """把 .env 里的字符串开关转成布尔值。"""
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on", "enabled")


def agent_database_path(setting, root):
    """Agent 业务库位置；相对路径按仓库根目录解析。"""
    path = Path(setting("AGENT_DATABASE_PATH", "data/agent.sqlite3"))
    return path if path.is_absolute() else Path(root) / path


def build_agent(*, setting, root, env_path, fetch_rows, exchange=None, forecast=None,
                notifier=None, store=None, audit=None):
    """按 `.env` 配置装配 Agent。

    `setting(name, default)` 由调用方提供（服务端用 `backend.app.setting`），
    这样进程环境变量优先于 `.env` 的规则只有一处实现。`store` / `audit` 可以由调用方
    先建好共享给钉钉通道，避免同一个库被打开两遍。
    """
    root = Path(root)
    store = store or AgentStore(agent_database_path(setting, root))
    llm = LLMClient(
        api_base=setting("AGENT_API_BASE", ""),
        api_key=setting("AGENT_API_KEY", ""),
        model=setting("AGENT_MODEL", ""),
        temperature=float(setting("AGENT_TEMPERATURE", "0.1") or 0.1),
        timeout=int(setting("AGENT_TIMEOUT_SECONDS", "90") or 90),
        provider=setting("AGENT_PROVIDER", "openai_compatible"),
        auth_file=setting("AGENT_CODEX_AUTH_FILE", ""),
        originator=setting("AGENT_CODEX_ORIGINATOR", ""),
    )
    registry = build_registry(
        with_forecast=forecast is not None,
        with_exchange=exchange is not None,
        with_notifier=notifier is not None,
    )
    context = ToolContext(
        env_path=env_path, root=root, fetch_rows=fetch_rows,
        exchange=exchange, forecast=forecast, notifier=notifier,
        setting=setting,
    )
    audit = audit or AuditLog(store)
    context.audit = audit
    return AgentRunner(
        registry=registry,
        llm=llm,
        sessions=SessionStore(store, history_limit=int(setting("AGENT_HISTORY_LIMIT", "20") or 20)),
        actions=PendingActions(store, ttl_seconds=int(setting("AGENT_ACTION_TTL_SECONDS", "1800") or 1800)),
        audit=audit,
        context=context,
        max_steps=int(setting("AGENT_MAX_TOOL_STEPS", "8") or 8),
        enabled=flag(setting("AGENT_ENABLED", "false")),
    )
