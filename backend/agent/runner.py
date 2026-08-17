# -*- coding: utf-8 -*-
"""Agent Core：工具循环 + 确认状态机的编排。

模型只负责理解意图、补齐参数、选工具和组织话术。查库、算数、生成文件、外发消息
全在确定性代码里；L1/L2 工具在这里被拦成 pending_action，等人工确认。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date

from ..business_time import business_today
from .actions import ActionError, PendingActions
from .audit import AuditLog, summarize
from .llm import LLMClient, LLMError
from .sessions import SessionStore, encode_tool_result
from ..staff_names import VIEWER_WRITE_DENIED, WEB_OPERATOR_UNBOUND
from .context import resolve_request_context
from .intents import INSOLE_PROCESS, INSOLE_QUERY, classify_intent
from .permissions import CAPABILITY_INSOLE_PROCESS, check_capability
from .tools import (
    RISK_LEVELS, PermissionDenied, ToolContext, ToolError, ToolRegistry,
    as_tool_envelope, declared_arguments,
)
from .validate import validate_arguments


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是「蜀黍家」采购供应链助手，服务对象是公司内部的采购员。

工作方式：
- 只能通过工具获取数据。不要凭记忆或推测回答采购单、交期、库存、预测相关的任何数字。
- 工具返回的数字直接引用，不要自己再算一遍、不要四舍五入成"大概"。
- 缺少必要参数（采购单号、票种、SKU、预测周期等）时先追问，不要替员工猜。
- 数据缺失、供应商未维护、模型未接入时，如实说明缺什么、该补什么，不要编造占位结果。
- 标注「需要员工确认」的工具，你调用后只会生成一条待确认动作；请把要点讲清楚并提示员工确认，
  不要声称已经完成。
- 员工说“把/将 A 换成 B”，或说某批「异常订单」要改 SKU 才能发货时，这是订单换货，不是采购催办。
  必须落到明确 ERP 内部订单号 o_id、源 SKU 和目标 SKU。单号不明确时先查订单镜像（默认待发货、
  含源 SKU，可加店铺/日期）和订单明细；结果不唯一就列出候选并追问，不允许猜测。
  参数明确后调用 submit_exchange_dry_run，仍需员工确认登记，dry-run 完成后还要在换货页
  二次确认，才会实际修改 ERP。
- 员工说查询或处理「抖音鞋垫订单」时：先 locate_insole_orders 列出内部单号、平台单号、
  状态、店铺、鞋码和目标 SKU；要处理再调用 process_insole_orders。确认前必须把订单信息
  讲清楚。半码按码数舍去小数后映射，不得把 Delivering 加进执行清单。
  已有待确认动作时不要再次调用 process_insole_orders。员工回复「确认」后由后端串行写 ERP，
  写完钉钉会再发一条【任务完成】结果日志。不要叫员工去换货页，不要说你没有确认入口或
  不能主动通知，也不要让员工靠反复查询来猜是否写完。
- 「异常订单」第一期只处理 SKU 替换：同款换规格、指定源→目标、已维护的白名单跨款。
  备注异常、超卖、地址错误等没有配置规则，说明做不了，不得自行定义、筛选或拿换货硬套。
  采购逾期走催办工具，不要和订单换货混用。
- 国标码是商品条码；执行标准是 GB/T 编号。问目录是否同步、有多少条，用 gb_catalog_status。
  问某商品或分类对应哪些国家标准，用 lookup_gb_standards。不要编造标准号；
  候选有多条（例如现行与即将实施并存）时列出要点，说明合同页按明细勾选，不要擅自指定一条。
- 问供应商未维护、近期采购 SKU 没图、票种缺价、分类未映射国标目录时，用 master_data_gaps，
  不要逐张单猜测或编造主数据。
- 回答用中文，简洁、可执行；多条结果按紧急程度组织，必要时给出下一步动作。

业务口径：
- 待入库 = 数量 − 已入库，按明细行取正数。
- 交期取该行 item_delivery_date，为空退到最早预计到货日期，都没有算未排期。
- 四波催办：逾期（剩余 < 0）/ T-1（0~1 天）/ T-10（2~10 天）/ T-20（11~20 天）。

今天是 {today}。当前员工：{operator}。角色：{role}。当前渠道：{channel}。
绑定采购员（「我名下」）：{buyers}。
员工说「我名下」时，buyer 填「我名下」或绑定姓名，不要填别人的名字。
viewer 只能查询，不要调用需要确认的工具。
可用工具（风险级 / 说明）：
{tools}"""


class AgentDisabled(RuntimeError):
    """Agent 未启用或未配置模型，调用方应返回 503。"""


class AgentRunner:
    def __init__(self, *, registry: ToolRegistry, llm: LLMClient, sessions: SessionStore,
                 actions: PendingActions, audit: AuditLog, context: ToolContext,
                 max_steps: int = 8, enabled: bool = True, directory=None,
                 memories=None, summary_enabled: bool = False,
                 summary_trigger: int = 40, summary_keep: int = 20,
                 max_tool_calls: int = 5):
        self.registry = registry
        self.llm = llm
        self.sessions = sessions
        self.actions = actions
        self.audit = audit
        self.context = context
        self.max_steps = max(1, int(max_steps))
        self.enabled = bool(enabled)
        self.directory = directory
        self.memories = memories
        self.summary_enabled = bool(summary_enabled)
        self.summary_trigger = max(1, int(summary_trigger))
        self.summary_keep = max(1, int(summary_keep))
        self.max_tool_calls = max(1, int(max_tool_calls))

    def _web_staff_allowed(self, operator: str, channel: str) -> bool:
        """网页 L1/L2 必须对上员工绑定表；钉钉渠道和未注入目录时不拦（离线测试仍可用）。"""
        if channel != "web" or self.directory is None:
            return True
        return bool(self.directory.known_operator(operator))

    def _assert_tool_allowed(self, tool, request) -> None:
        if tool.channels and request.channel not in tool.channels:
            raise PermissionDenied(
                f"工具 {tool.name} 不能在 {request.channel} 渠道使用",
                role=request.role, tool=tool.name, permission=tool.permission,
                channel=request.channel,
            )
        if request.role == "viewer" and (tool.needs_confirm or tool.permission != "read"):
            raise PermissionDenied(
                VIEWER_WRITE_DENIED,
                role=request.role, tool=tool.name, permission=tool.permission,
                channel=request.channel,
            )
        if tool.side_effect == "erp":
            check_capability(
                self.directory,
                operator=request.operator, actor_id=request.actor_id,
                channel=request.channel, role=request.role,
                capability=CAPABILITY_INSOLE_PROCESS,
            )

    @property
    def available(self) -> bool:
        return self.enabled and self.llm.configured

    @property
    def store(self):
        return self.sessions.store

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "maxToolSteps": self.max_steps,
            "llm": self.llm.status(),
            "tools": self.registry.catalog(),
        }

    def system_prompt(self, operator: str, channel: str, *, role: str = "operator",
                      buyer_names=()) -> str:
        tools = "\n".join(
            f"- {item['name']}（{item['risk']} {item['riskLabel']}）：{item['description']}"
            for item in self.registry.catalog()
        )
        buyers = "、".join(buyer_names) if buyer_names else "未绑定"
        return SYSTEM_PROMPT.format(
            today=business_today().isoformat(),
            operator=operator or "未署名",
            role=role or "operator",
            channel=channel,
            buyers=buyers,
            tools=tools,
        )

    def chat(self, *, message: str, session_key: str, operator: str = "", channel: str = "web",
             actor_id: str = "") -> dict:
        message = str(message or "").strip()
        if not message:
            raise ValueError("消息内容不能为空")
        if len(message) > 4000:
            raise ValueError("单条消息不能超过 4000 字")
        if not self.available:
            raise AgentDisabled("Agent 未启用或尚未配置模型（AGENT_ENABLED / AGENT_MODEL / 对应供应商凭证）")

        session = self.sessions.ensure(channel, session_key, operator)
        session_id = session["id"]
        with self.sessions.lock_for(session_id):
            return self._chat_locked(
                message=message, session=session, operator=operator,
                channel=channel, actor_id=actor_id, session_key=session_key,
            )

    def _chat_locked(self, *, message, session, operator, channel, actor_id, session_key) -> dict:
        session_id = session["id"]
        operator = operator or session.get("operator") or ""
        request = resolve_request_context(
            self.directory, operator=operator, channel=channel, actor_id=actor_id,
            session_key=session_key,
        )
        operator = request.operator or operator
        run_id = self.audit.start_run(
            session_id=session_id, channel=channel, operator=operator,
            user_id=request.user_id, request=message, model=self.llm.model,
        )
        self.sessions.add_message(session_id, "user", message, run_id=run_id)
        intent = classify_intent(message)
        if intent is not None:
            return self._dispatch_intent(intent, session, request, run_id)
        memory = ""
        if self.memories and self.memories.enabled and self.directory and operator:
            if self.directory.known_operator(operator):
                memory = self.memories.prompt_block(operator)
        messages = self.sessions.context_messages(
            session_id,
            system=self.system_prompt(
                operator, channel, role=request.role, buyer_names=request.buyer_names,
            ),
            memory=memory,
            summary=self.sessions.latest_summary(session_id),
        )
        steps: list[dict] = []
        pending: list[dict] = []
        started = time.monotonic()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        try:
            reply = self._loop(
                messages, steps, pending, session_id, run_id, request, usage,
            )
        except (LLMError, ToolError, ValueError) as exc:
            self.audit.finish_run(run_id, status="failed", steps=len(steps),
                                  duration_ms=int((time.monotonic() - started) * 1000), error=str(exc),
                                  prompt_tokens=usage["prompt_tokens"],
                                  completion_tokens=usage["completion_tokens"])
            raise
        except Exception as exc:
            self.audit.finish_run(run_id, status="failed", steps=len(steps),
                                  duration_ms=int((time.monotonic() - started) * 1000),
                                  error=f"{type(exc).__name__}: {exc}",
                                  prompt_tokens=usage["prompt_tokens"],
                                  completion_tokens=usage["completion_tokens"])
            raise
        self.sessions.add_message(session_id, "assistant", reply, run_id=run_id)
        self.audit.finish_run(run_id, status="ok", reply=reply, steps=len(steps),
                              duration_ms=int((time.monotonic() - started) * 1000),
                              prompt_tokens=usage["prompt_tokens"],
                              completion_tokens=usage["completion_tokens"])
        self._maybe_summarize(session_id)
        return {
            "ok": True,
            "sessionId": session_id,
            "runId": run_id,
            "operator": operator,
            "userId": request.user_id,
            "traceId": request.trace_id,
            "role": request.role,
            "buyerNames": list(request.buyer_names),
            "usage": dict(usage),
            "reply": reply,
            "steps": steps,
            "pendingActions": pending,
        }

    def handle_intent(self, message: str, *, session_key: str, operator: str = "",
                      channel: str = "web", actor_id: str = "") -> dict | None:
        """固定意图不走 LLM。未识别返回 None，由调用方再走 chat。"""
        message = str(message or "").strip()
        intent = classify_intent(message)
        if intent is None:
            return None
        session = self.sessions.ensure(channel, session_key, operator)
        with self.sessions.lock_for(session["id"]):
            request = resolve_request_context(
                self.directory, operator=operator or session.get("operator") or "",
                channel=channel, actor_id=actor_id, session_key=session_key,
            )
            run_id = self.audit.start_run(
                session_id=session["id"], channel=channel,
                operator=request.operator or operator, user_id=request.user_id,
                request=message, model="intent",
            )
            self.sessions.add_message(session["id"], "user", message, run_id=run_id)
            return self._dispatch_intent(intent, session, request, run_id)

    def _dispatch_intent(self, intent, session, request, run_id) -> dict:
        started = time.monotonic()
        tool_name = "locate_insole_orders" if intent.name == INSOLE_QUERY else "process_insole_orders"
        if intent.name not in {INSOLE_QUERY, INSOLE_PROCESS}:
            tool_name = ""
        result, action = ({"error": f"未实现的意图 {intent.name}"}, None)
        if tool_name:
            result, action = self._invoke(
                {"function": {
                    "name": tool_name,
                    "arguments": json.dumps(intent.arguments or {}, ensure_ascii=False),
                }},
                session["id"], run_id, request,
            )
        if isinstance(result, dict) and result.get("error"):
            reply = str(result["error"])
            status = "error"
            pending = []
        elif action:
            preview = action.get("preview") or {}
            reply = str(preview.get("markdown") or action.get("title") or "请核对订单后确认。")
            note = preview.get("note")
            if note:
                reply = f"{reply}\n{note}"
            status = "awaiting_confirm"
            pending = [action]
        else:
            data = result.get("data") if isinstance(result, dict) else None
            reply = ""
            if isinstance(data, dict):
                reply = str(data.get("markdown") or "")
            if not reply and isinstance(result, dict):
                reply = str(result.get("summary") or "没有待处理的鞋垫订单。")
            status = "ok"
            pending = []
        steps = [{"tool": tool_name or intent.name, "status": status}]
        self.sessions.add_message(session["id"], "assistant", reply, run_id=run_id)
        self.audit.finish_run(
            run_id, status="ok" if status != "error" else "failed",
            reply=reply, steps=len(steps),
            duration_ms=int((time.monotonic() - started) * 1000),
            error=reply if status == "error" else "",
        )
        return {
            "ok": status != "error",
            "sessionId": session["id"],
            "runId": run_id,
            "operator": request.operator,
            "userId": request.user_id,
            "traceId": request.trace_id,
            "role": request.role,
            "buyerNames": list(request.buyer_names),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "reply": reply,
            "steps": steps,
            "pendingActions": pending,
            "intent": intent.name,
        }

    def _loop(self, messages, steps, pending, session_id, run_id, request, usage) -> str:
        for step in range(self.max_steps):
            answer = self.llm.chat(messages, tools=self.registry.schemas())
            self._add_usage(answer.get("usage"), usage)
            tool_calls = answer.get("tool_calls") or []
            if len(tool_calls) > self.max_tool_calls:
                tool_calls = tool_calls[:self.max_tool_calls]
            if not tool_calls:
                content = str(answer.get("content") or "").strip()
                return content or "本轮模型没有给出可展示的回复，请换个说法再问一次。"
            assistant_message = {"role": "assistant", "content": answer.get("content") or "",
                                 "tool_calls": tool_calls}
            messages.append(assistant_message)
            self.sessions.add_message(session_id, "assistant", answer.get("content") or "",
                                      run_id=run_id, tool_calls=tool_calls)
            for call in tool_calls:
                result, action = self._invoke(call, session_id, run_id, request)
                steps.append({
                    "step": step + 1,
                    "tool": (call.get("function") or {}).get("name") or "",
                    "status": "error" if isinstance(result, dict) and result.get("error") else "ok",
                    "actionId": action["id"] if action else None,
                })
                if action:
                    pending.append({
                        "id": action["id"], "tool": action["tool"], "risk": action["risk"],
                        "title": action["title"], "preview": action["preview"],
                        "expiresAt": action["expiresAt"],
                    })
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "name": (call.get("function") or {}).get("name") or "",
                    "content": encode_tool_result(result, limit=self.sessions.tool_result_limit),
                }
                messages.append(tool_message)
                self.sessions.add_message(session_id, "tool", tool_message["content"], run_id=run_id,
                                          name=tool_message["name"], tool_call_id=tool_message["tool_call_id"])
        return "这轮需要的工具调用超过了步数上限，请把问题拆小一点再问（比如只问一个采购单或一个档位）。"

    def _invoke(self, call, session_id, run_id, request):
        """执行一次工具调用；L1/L2 只登记待确认动作。"""
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw_arguments = function.get("arguments") or "{}"
        started = time.monotonic()
        operator = request.operator
        channel = request.channel
        actor_id = request.actor_id
        user_id = request.user_id
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
            if not isinstance(arguments, dict):
                raise ToolError("工具入参必须是对象")
            tool = self.registry.get(name)
            arguments = declared_arguments(tool, arguments)
            arguments = validate_arguments(tool, arguments)
            ctx = self.context.for_caller(
                operator=operator, user_id=user_id, channel=channel,
                session_id=session_id, run_id=run_id,
                role=request.role, buyer_names=request.buyer_names,
            )
            self._assert_tool_allowed(tool, request)
            if tool.needs_confirm:
                if not self._web_staff_allowed(operator, channel):
                    raise ToolError(WEB_OPERATOR_UNBOUND)
                if channel == "dingtalk" and self.directory is not None:
                    if not actor_id or not self.directory.get_by_dingtalk_user_id(actor_id):
                        raise ToolError("还没绑定采购员姓名。回复「绑定 利特」或「绑定 利特、李佳冬（利特）」。")
                preview = tool.preview(arguments, ctx) if tool.preview else {}
                if not isinstance(preview, dict):
                    preview = {"value": preview}
                preview = {**preview, "arguments": arguments}
                if tool.name == "process_insole_orders" and session_id:
                    existing = self._reuse_insole_pending(
                        session_id, arguments, operator, actor_id,
                    )
                    if existing:
                        return {
                            "status": "awaiting_confirm",
                            "actionId": existing["id"],
                            "risk": f"{tool.risk} {RISK_LEVELS[tool.risk]}",
                            "expiresAt": existing["expiresAt"],
                            "preview": existing.get("preview") or preview,
                            "message": "已有待确认动作。请让员工直接回复「确认」，由后端写入 ERP，不要去换货页，也不要再次调用本工具。",
                        }, existing
                action = self.actions.create(
                    tool=tool.name, risk=tool.risk, arguments=arguments,
                    title=tool.title(arguments) if tool.title else tool.name,
                    preview=preview, operator=operator, user_id=user_id, channel=channel,
                    session_id=session_id, run_id=run_id, actor_id=actor_id,
                )
                self.audit.record_tool(
                    tool=name, risk=tool.risk, status="awaiting_confirm", arguments=arguments,
                    result={"actionId": action["id"]}, run_id=run_id, session_id=session_id,
                    pending_action_id=action["id"], operator=operator, user_id=user_id,
                    channel=channel, duration_ms=int((time.monotonic() - started) * 1000),
                )
                return {
                    "status": "awaiting_confirm",
                    "actionId": action["id"],
                    "risk": f"{tool.risk} {RISK_LEVELS[tool.risk]}",
                    "expiresAt": action["expiresAt"],
                    "preview": preview,
                    "message": (
                        "已生成待确认动作，尚未执行。鞋垫换货请让员工直接回复「确认」，"
                        "由后端串行写入 ERP，不要去换货页，也不要再次调用本工具。"
                        if tool.name == "process_insole_orders"
                        else "已生成待确认动作，尚未执行。请向员工说明要点并等待确认。"
                    ),
                }, action
            result = as_tool_envelope(tool.handler(arguments, ctx))
            self.audit.record_tool(
                tool=name, risk=tool.risk, status="ok", arguments=arguments, result=result,
                run_id=run_id, session_id=session_id, operator=operator, user_id=user_id,
                channel=channel, duration_ms=int((time.monotonic() - started) * 1000),
            )
            return result, None
        except (ToolError, ActionError, ValueError, RuntimeError) as exc:
            self.audit.record_tool(
                tool=name, status="error", arguments={"raw": summarize(raw_arguments)},
                error=str(exc), run_id=run_id, session_id=session_id, operator=operator,
                user_id=user_id, channel=channel,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            payload = {"error": str(exc)}
            if isinstance(exc, PermissionDenied):
                payload["permission"] = exc.decision
            return payload, None
        except Exception as exc:
            logger.exception("Agent tool error [%s]", name)
            self.audit.record_tool(
                tool=name, status="error", arguments={"raw": summarize(raw_arguments)},
                error=f"{type(exc).__name__}: {exc}", run_id=run_id, session_id=session_id,
                operator=operator, user_id=user_id, channel=channel,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {"error": f"工具 {name} 执行失败，请稍后重试或联系维护人"}, None

    def _open_insole_actions(self, session_id: str) -> list[dict]:
        return [
            item for item in self.actions.list(session_id=session_id, status="pending")
            if item.get("tool") == "process_insole_orders"
        ]

    def _cancel_extra_insole(self, actions: list[dict], keep_id: str,
                             operator: str, actor_id: str) -> None:
        for item in actions:
            if item["id"] == keep_id:
                continue
            try:
                self.actions.cancel(item["id"], operator, actor_id=actor_id)
            except ActionError:
                pass

    def _reuse_insole_pending(self, session_id: str, arguments: dict,
                              operator: str, actor_id: str) -> dict | None:
        """同一会话只保留一条鞋垫待确认，避免重复调用再生成新动作。"""
        opens = self._open_insole_actions(session_id)
        if not opens:
            return None
        wanted = set(arguments.get("o_ids") or [])
        keep = next(
            (item for item in opens
             if set((item.get("arguments") or {}).get("o_ids") or []) == wanted),
            None,
        )
        if keep:
            self._cancel_extra_insole(opens, keep["id"], operator, actor_id)
            return keep
        self._cancel_extra_insole(opens, "", operator, actor_id)
        return None

    def confirm_latest(self, operator: str = "", *, channel: str = "web",
                       actor_id: str = "", session_key: str = "") -> dict:
        """钉钉只回「确认」时，执行当前会话最近一条待确认动作。"""
        session = self.sessions.ensure(channel, session_key, operator)
        action = self.actions.latest_open(session_id=session["id"])
        if action is None and actor_id:
            action = self.actions.latest_open(actor_id=actor_id)
        if action is None:
            raise ActionError("当前没有待确认的动作。请先查询并处理鞋垫订单。", 404)
        if action.get("tool") == "process_insole_orders" and action.get("sessionId"):
            self._cancel_extra_insole(
                self._open_insole_actions(action["sessionId"]),
                action["id"], operator, actor_id,
            )
        return self.confirm(action["id"], operator, channel=channel, actor_id=actor_id)

    def cancel_latest(self, operator: str = "", *, channel: str = "web",
                      actor_id: str = "", session_key: str = "") -> dict:
        session = self.sessions.ensure(channel, session_key, operator)
        action = self.actions.latest_open(session_id=session["id"])
        if action is None and actor_id:
            action = self.actions.latest_open(actor_id=actor_id)
        if action is None:
            raise ActionError("当前没有待取消的动作。", 404)
        return self.cancel(action["id"], operator, channel=channel, actor_id=actor_id)

    def confirm(self, action_id: str, operator: str = "", channel: str = "web",
                actor_id: str = "") -> dict:
        """确认并执行一条待确认动作；同一动作只会真正执行一次。"""
        if not self._web_staff_allowed(operator, channel):
            raise ActionError(WEB_OPERATOR_UNBOUND, 403)
        if channel == "dingtalk" and self.directory is not None:
            if not actor_id or not self.directory.get_by_dingtalk_user_id(actor_id):
                raise ActionError("还没绑定采购员姓名，不能确认。请先回复「绑定 姓名」。", 403)
        action = self.actions.get(action_id)
        tool = self.registry.get(action["tool"])
        request = resolve_request_context(
            self.directory, operator=operator, channel=channel, actor_id=actor_id,
        )
        if request.role == "viewer":
            raise ActionError(VIEWER_WRITE_DENIED, 403)
        session_id = action.get("sessionId") or ""
        if session_id:
            with self.sessions.lock_for(session_id):
                return self._confirm_execute(action_id, operator, actor_id, request, tool)
        return self._confirm_execute(action_id, operator, actor_id, request, tool)

    def _confirm_execute(self, action_id, operator, actor_id, request, tool) -> dict:
        def executor(name, arguments, current):
            ctx = self.context.for_caller(
                operator=current["operator"] or operator,
                user_id=current.get("userId") or request.user_id,
                channel=current["channel"] or request.channel,
                session_id=current["sessionId"], run_id=current["runId"], action_id=current["id"],
                role=request.role, buyer_names=request.buyer_names,
            )
            started = time.monotonic()
            try:
                result = tool.handler(arguments, ctx)
            except Exception as exc:
                self.audit.record_tool(
                    tool=name, risk=tool.risk, status="error", arguments=arguments,
                    error=f"{type(exc).__name__}: {exc}", run_id=current["runId"],
                    session_id=current["sessionId"], pending_action_id=current["id"],
                    operator=ctx.operator, user_id=ctx.user_id, channel=ctx.channel,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            self.audit.record_tool(
                tool=name, risk=tool.risk, status="executed", arguments=arguments, result=result,
                run_id=current["runId"], session_id=current["sessionId"],
                pending_action_id=current["id"], operator=ctx.operator, user_id=ctx.user_id,
                channel=ctx.channel, duration_ms=int((time.monotonic() - started) * 1000),
            )
            return result

        executed = self.actions.execute(action_id, operator, executor, actor_id=actor_id)
        if executed["sessionId"]:
            self.sessions.add_message(
                executed["sessionId"], "assistant",
                f"【已执行】{executed['title']}\n{summarize(executed['result'])}",
                run_id=executed["runId"],
            )
        return executed

    def cancel(self, action_id: str, operator: str = "", channel: str = "web",
               actor_id: str = "") -> dict:
        if not self._web_staff_allowed(operator, channel):
            raise ActionError(WEB_OPERATOR_UNBOUND, 403)
        if channel == "dingtalk" and self.directory is not None:
            if not actor_id or not self.directory.get_by_dingtalk_user_id(actor_id):
                raise ActionError("还没绑定采购员姓名，不能取消。请先回复「绑定 姓名」。", 403)
        request = resolve_request_context(
            self.directory, operator=operator, channel=channel, actor_id=actor_id,
        )
        if request.role == "viewer":
            raise ActionError(VIEWER_WRITE_DENIED, 403)
        return self.actions.cancel(action_id, operator, actor_id=actor_id)

    def _maybe_summarize(self, session_id: str) -> None:
        if not self.summary_enabled:
            return
        if self.sessions.message_count(session_id) < self.summary_trigger:
            return
        chunk, upto = self.sessions.oldest_unsummarized(session_id, keep=self.summary_keep)
        if not chunk:
            return
        try:
            prompt = (
                "把下面的采购助手对话压成不超过 800 字的中文摘要。"
                "只记单号、SKU、供应商、已确认的结论、未完成事项。"
                "禁止记录金额、数量、日期等易过期数字。\n\n"
                + "\n".join(
                    f"{item.get('role')}: {str(item.get('content') or '')[:400]}"
                    for item in chunk
                )
            )
            answer = self.llm.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
                tool_choice="none",
            )
            content = str(answer.get("content") or "").strip()
            if content:
                self.sessions.save_summary(session_id, content[:800], upto)
        except Exception:
            logger.exception("Session summary failed")

    def _add_usage(self, incoming, bucket) -> None:
        if not isinstance(incoming, dict) or not isinstance(bucket, dict):
            return
        bucket["prompt_tokens"] += int(incoming.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(incoming.get("completion_tokens") or 0)

    def pending(self, *, session_id: str | None = None, limit: int = 20) -> list[dict]:
        return self.actions.list(session_id=session_id, limit=limit)
