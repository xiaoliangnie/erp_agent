# -*- coding: utf-8 -*-
"""Agent Core 的装配入口。

HTTP 服务、命令行调试和钉钉线程都用 `build_agent` 得到同一个 `AgentRunner`，
共用同一份工具注册表和同一套确认流。
"""
from pathlib import Path

from .actions import ActionError, PendingActions
from .audit import AuditLog
from .context import RequestContext, resolve_request_context, resolve_user_id
from .llm import LLMClient, LLMError
from .maintenance import MaintenanceScheduler
from .memories import OperatorMemories
from .runner import AgentDisabled, AgentRunner
from .sessions import SessionStore
from .store import AgentStore
from .intents import classify_intent
from .tools import (
    RESERVED_TOOLS, RISK_LEVELS, PermissionDenied, Tool, ToolContext, ToolError,
    ToolRegistry, build_registry, declared_arguments, scoped_buyers,
)


__all__ = [
    "ActionError", "AgentDisabled", "AgentRunner", "AgentStore", "AuditLog", "LLMClient",
    "RequestContext", "resolve_request_context", "resolve_user_id",
    "LLMError", "PendingActions", "RESERVED_TOOLS", "RISK_LEVELS", "SessionStore", "Tool",
    "PermissionDenied", "ToolContext", "ToolError", "ToolRegistry",
    "agent_database_path", "build_agent",
    "build_registry", "declared_arguments", "flag", "MaintenanceScheduler",
    "scoped_buyers",
    "OperatorMemories",
]


def flag(value, default=False):
    """把 .env 里的字符串开关转成布尔值。"""
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on", "enabled")


def agent_database_path(setting, root):
    """Agent 业务库位置；相对路径按仓库根目录解析。"""
    from ..paths import resolve_repo_path
    return resolve_repo_path(setting("AGENT_DATABASE_PATH", "files/data/agent.sqlite3"), root=root)


def build_agent(*, setting, root, env_path, fetch_rows, exchange=None, erp=None,
                forecast=None, notifier=None, store=None, audit=None, directory=None,
                quality=None, memories=None, mirror=None):
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
        with_quality=quality is not None,
    )
    context = ToolContext(
        env_path=env_path, root=root, fetch_rows=fetch_rows,
        exchange=exchange, erp=erp, forecast=forecast, notifier=notifier,
        setting=setting, quality=quality, mirror=mirror,
    )
    audit = audit or AuditLog(store)
    context.audit = audit
    memories = memories or OperatorMemories(
        store, enabled=flag(setting("AGENT_MEMORY_ENABLED", "false")),
    )
    return AgentRunner(
        registry=registry,
        llm=llm,
        sessions=SessionStore(
            store,
            history_limit=int(setting("AGENT_HISTORY_LIMIT", "20") or 20),
            idle_minutes=int(setting("AGENT_SESSION_IDLE_MINUTES", "120") or 0),
            char_budget=int(setting("AGENT_CONTEXT_CHAR_BUDGET", "60000") or 60000),
            tool_result_limit=int(setting("AGENT_TOOL_RESULT_LIMIT", "8000") or 8000),
        ),
        actions=PendingActions(store, ttl_seconds=int(setting("AGENT_ACTION_TTL_SECONDS", "1800") or 1800)),
        audit=audit,
        context=context,
        max_steps=int(setting("AGENT_MAX_TOOL_STEPS", "8") or 8),
        enabled=flag(setting("AGENT_ENABLED", "false")),
        directory=directory,
        memories=memories,
        summary_enabled=flag(setting("AGENT_SUMMARY_ENABLED", "false")),
        summary_trigger=int(setting("AGENT_SUMMARY_TRIGGER_MESSAGES", "40") or 40),
    )
