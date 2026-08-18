# -*- coding: utf-8 -*-
"""Intent Router。

先走确定性命令和现有 `classify_intent`，未识别再交给 Agent Loop。
只判断「他想干什么」，不判断权限。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .intents import (
    KIND_ASK, KIND_INVOKE, KIND_REFUSE, Intent, classify_intent, clarify_working_set,
    intent_calls,
)
from .session_commands import parse_session_command


ROUTE_EXACT_QUERY = "exact_query"
ROUTE_WORKFLOW = "workflow"
ROUTE_AGENT = "agent"
ROUTE_KNOWLEDGE = "knowledge"
ROUTE_CLARIFY = "clarify"
ROUTE_DENY = "deny"
ROUTE_COMMAND = "command"

QUERY_TOOLS = {
    "get_purchase_order", "search_purchase_orders", "get_sales_order_items",
    "search_sales_orders", "delivery_reminders", "dashboard_summary",
    "search_products", "gb_catalog_status", "lookup_gb_standards",
    "master_data_gaps", "locate_insole_orders", "list_quality_issues",
    "forecast_demand", "order_suggestion",
}
WORKFLOW_TOOLS = {
    "generate_purchase_contract", "generate_dropship_workbook",
    "submit_exchange_dry_run", "process_insole_orders",
    "send_delivery_reminder", "record_quality_issue", "push_quality_report",
    "resolve_quality_issue", "cancel_quality_issue",
}
KNOWLEDGE_TOOLS = {"lookup_gb_standards", "gb_catalog_status"}
# 固定原话已经能点名的工具不要再丢给模型审核。钩子留着，避免以后又把 L2 塞进 LLM。
REVIEW_TOOLS = frozenset()


@dataclass(frozen=True)
class RouteDecision:
    route: str
    domain: str = ""
    operation: str = ""
    entities: dict = field(default_factory=dict)
    missing_slots: list = field(default_factory=list)
    risk_level: str = "L0"
    confidence: float = 0.0
    tool: str = ""
    intent: Intent | None = None

    def as_public(self) -> dict:
        calls = []
        if self.intent is not None:
            calls = [{"name": name, "arguments": arguments} for name, arguments in intent_calls(self.intent)]
        return {
            "route": self.route,
            "domain": self.domain,
            "operation": self.operation,
            "entities": dict(self.entities),
            "missing_slots": list(self.missing_slots),
            "risk_level": self.risk_level,
            "confidence": self.confidence,
            "tool": self.tool,
            "calls": calls,
            "review": needs_llm_review(self.intent),
        }


def needs_llm_review(intent: Intent | None) -> bool:
    """固定意图一律直分派。鞋垫处理也是固定任务，人确认才写 ERP。"""
    if intent is None or intent.kind != KIND_INVOKE:
        return False
    tool = str(intent.tool or "")
    return tool in REVIEW_TOOLS


def intent_review_hint(intent: Intent) -> str:
    """给模型的审核说明。当前鞋垫处理不再走这条路。"""
    calls = intent_calls(intent)
    suggested = "、".join(name for name, _ in calls) or (intent.tool or intent.name)
    arguments = dict(intent.arguments or {})
    extras = ""
    if arguments:
        extras = "抽出参数：" + json.dumps(arguments, ensure_ascii=False)
    lines = [
        "【意图已识别，请审核后调用工具，不要改工具返回的数字】",
        f"任务：{intent.name}。建议工具：{suggested}。",
    ]
    if extras:
        lines.append(extras)
    return "\n".join(lines)


def route_message(text: str, *, working_set: dict | None = None) -> RouteDecision:
    """第一版路由：会话命令 → 固定意图 → 开放 Agent。不接收角色或权限。

    ``working_set`` 只用于补当前话题已抽出的唯一编号，不猜新单号。
    """
    command = parse_session_command(text)
    if command is not None:
        return RouteDecision(
            route=ROUTE_COMMAND,
            domain="session",
            operation=str(command.get("name") or "session_command"),
            entities={"content": command.get("content") or command.get("keyword") or ""},
            confidence=0.99,
        )
    intent = classify_intent(text)
    if intent is not None and intent.kind == KIND_ASK and working_set:
        filled = classify_intent(text, working_set=working_set)
        if filled is not None:
            intent = filled
        if intent.kind == KIND_ASK:
            intent = clarify_working_set(intent, working_set)
    if intent is None:
        return RouteDecision(route=ROUTE_AGENT, domain="procurement", confidence=0.2)
    if intent.kind == KIND_REFUSE:
        return RouteDecision(
            route=ROUTE_DENY,
            domain=_domain_for(intent.tool, intent.name),
            operation=intent.name,
            entities=dict(intent.arguments or {}),
            risk_level="L0",
            confidence=0.99,
            intent=intent,
        )
    if intent.kind == KIND_ASK:
        return RouteDecision(
            route=ROUTE_CLARIFY,
            domain=_domain_for(intent.tool, intent.name),
            operation=intent.name,
            entities=dict(intent.arguments or {}),
            missing_slots=_missing_slots(intent),
            confidence=0.9,
            intent=intent,
        )
    tool = intent.tool or ""
    if tool in KNOWLEDGE_TOOLS:
        route = ROUTE_KNOWLEDGE
    elif tool in WORKFLOW_TOOLS:
        route = ROUTE_WORKFLOW
    elif tool in QUERY_TOOLS:
        route = ROUTE_EXACT_QUERY
    else:
        route = ROUTE_EXACT_QUERY if intent.kind == KIND_INVOKE else ROUTE_AGENT
    return RouteDecision(
        route=route,
        domain=_domain_for(tool, intent.name),
        operation=intent.name or tool,
        entities=dict(intent.arguments or {}),
        risk_level=_risk_for(tool),
        confidence=0.98,
        tool=tool,
        intent=intent,
    )


def _domain_for(tool: str, name: str) -> str:
    text = f"{tool} {name}"
    if "insole" in text or "exchange" in text:
        return "order_exception"
    if "contract" in text:
        return "contract"
    if "dropship" in text:
        return "dropship"
    if "gb" in text or "standard" in text:
        return "standards"
    if "quality" in text:
        return "quality"
    if "forecast" in text or "suggestion" in text:
        return "forecast"
    if "product" in text:
        return "product"
    if "dashboard" in text:
        return "dashboard"
    if "reminder" in text or "delivery" in text:
        return "followup"
    if "refuse" in text or "ask_" in text:
        return "procurement"
    return "procurement"


def _risk_for(tool: str) -> str:
    if tool in {"process_insole_orders", "send_delivery_reminder", "push_quality_report"}:
        return "L2"
    if tool in WORKFLOW_TOOLS:
        return "L1"
    return "L0"


def _missing_slots(intent: Intent) -> list[str]:
    if intent.name == "ask_contract":
        if intent.arguments.get("po_id"):
            return ["invoice_type"]
        return ["po_id", "invoice_type"]
    if intent.name == "ask_exchange":
        return ["source_sku", "target_sku", "o_ids"]
    if intent.name == "ask_purchase_order":
        return ["po_id"]
    if intent.name == "ask_sales_order":
        return ["o_ids"]
    if intent.name == "ask_product":
        return ["query"]
    if intent.name == "ask_gb":
        return ["query"]
    if intent.name == "ask_forecast":
        return ["keys"]
    if intent.name == "ask_which_id":
        return ["id_type"]
    if intent.name in {"ask_quality", "ask_quality_command"}:
        return ["description"] if intent.name == "ask_quality" else ["issue_id"]
    return []
