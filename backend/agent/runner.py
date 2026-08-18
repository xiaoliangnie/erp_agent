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
from .context import identity_block, resolve_request_context
from .intents import INSOLE_PROCESS, INSOLE_QUERY, KIND_ASK, KIND_REFUSE, intent_calls
from .router import intent_review_hint, needs_llm_review, route_message
from .session_commands import parse_session_command
from .working_set import extract_working_set, format_working_set
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
- 员工说查询或处理「鞋垫订单」（含抖音/快手/视频号）时：固定原话由后端直接分派，不要再审一遍。
  查询用 locate_insole_orders；要处理用 process_insole_orders。未识别的说法才由你选工具。
  半码按码数舍去小数后映射，不得把 Delivering 加进执行清单。
  已有待确认动作时不要再次调用 process_insole_orders，让员工直接回「确认」。
  员工回复「确认」后由后端串行写 ERP，写完钉钉会再发【任务完成】。不要叫员工去换货页。
- 「异常订单」第一期只处理 SKU 替换：同款换规格、指定源→目标、已维护的白名单跨款。
  备注异常、超卖、地址错误等没有配置规则，说明做不了，不得自行定义、筛选或拿换货硬套。
  采购逾期走催办工具，不要和订单换货混用。
- 国标码是商品条码；执行标准是 GB/T 编号。问目录是否同步、有多少条，用 gb_catalog_status。
  问某商品或分类对应哪些国家标准，用 lookup_gb_standards。不要编造标准号；
  候选有多条（例如现行与即将实施并存）时列出要点，说明合同页按明细勾选，不要擅自指定一条。
- 问供应商未维护、近期采购 SKU 没图、票种缺价、分类未映射国标目录时，用 master_data_gaps，
  不要逐张单猜测或编造主数据。
- 员工要「代发订单」「今天的代发表」时，用 generate_dropship_workbook。确认后才抓 ERP，
  不改单据；不要声称已经导出完成。收货明文只进 Excel，不要在对话里复述姓名、手机或地址。
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
                 memories=None, users=None, summary_enabled: bool = False,
                 summary_trigger: int = 40, summary_keep: int = 20,
                 max_tool_calls: int = 5, busy_timeout: float = 45):
        self.registry = registry
        self.llm = llm
        self.sessions = sessions
        self.actions = actions
        self.audit = audit
        self.context = context
        self.max_steps = max(1, int(max_steps))
        self.enabled = bool(enabled)
        self.directory = directory
        self.users = users
        self.memories = memories
        self.summary_enabled = bool(summary_enabled)
        self.summary_trigger = max(1, int(summary_trigger))
        self.summary_keep = max(1, int(summary_keep))
        self.max_tool_calls = max(1, int(max_tool_calls))
        self.busy_timeout = max(0.0, float(busy_timeout))

    def _request_context(self, *, operator: str = "", channel: str = "web",
                         actor_id: str = "", session_key: str = "",
                         session_id: str = ""):
        return resolve_request_context(
            self.directory, users=self.users, operator=operator, channel=channel,
            actor_id=actor_id, session_key=session_key, session_id=session_id,
        )

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
        request = self._request_context(
            operator=operator, channel=channel, actor_id=actor_id, session_key=session_key,
        )
        operator = request.operator or operator
        session = self.sessions.ensure(
            channel, session_key, operator, user_id=request.user_id,
        )
        lock = self.sessions.lock_for(session["id"])
        if not lock.acquire(timeout=self.busy_timeout):
            return self._busy_reply(session, request)
        try:
            command = parse_session_command(message)
            if command is not None:
                return self._apply_session_command(command, message, session, request)
            if not self.available:
                raise AgentDisabled("Agent 未启用或尚未配置模型（AGENT_ENABLED / AGENT_MODEL / 对应供应商凭证）")
            return self._chat_locked(
                message=message, session=session, operator=operator,
                channel=channel, actor_id=actor_id, session_key=session_key,
                request=request,
            )
        finally:
            lock.release()

    def handle_session_command(self, message: str, *, session_key: str, operator: str = "",
                               channel: str = "web", actor_id: str = "") -> dict | None:
        """新话题 / 记住 / 忘记。未识别返回 None。不依赖模型是否启用。"""
        message = str(message or "").strip()
        command = parse_session_command(message)
        if command is None:
            return None
        request = self._request_context(
            operator=operator, channel=channel, actor_id=actor_id, session_key=session_key,
        )
        session = self.sessions.ensure(
            channel, session_key, request.operator or operator, user_id=request.user_id,
        )
        lock = self.sessions.lock_for(session["id"])
        if not lock.acquire(timeout=self.busy_timeout):
            return self._busy_reply(session, request)
        try:
            return self._apply_session_command(command, message, session, request)
        finally:
            lock.release()

    def _apply_session_command(self, command: dict, message: str, session: dict,
                               request) -> dict:
        session_id = session["id"]
        run_id = self.audit.start_run(
            session_id=session_id, channel=request.channel,
            operator=request.operator, user_id=request.user_id,
            request=message, model="session",
        )
        self.sessions.add_message(session_id, "user", message, run_id=run_id)
        try:
            reply = self._session_command_reply(command, request)
        except ValueError as exc:
            reply = str(exc)
        if command.get("name") == "new_topic":
            self.sessions.rotate(session_id)
        else:
            self.sessions.add_message(session_id, "assistant", reply, run_id=run_id)
        self.audit.finish_run(run_id, status="ok", reply=reply, steps=0, duration_ms=0)
        return self._command_payload(session_id, run_id, request, reply)

    def _session_command_reply(self, command: dict, request) -> str:
        name = command.get("name")
        if name == "new_topic":
            return "已开新话题，历史在网页端可查。"
        if name == "remember":
            if not self.memories or not self.memories.enabled:
                return "记忆未开启。请联系维护人打开 AGENT_MEMORY_ENABLED。"
            if not self._memory_allowed(request):
                return "请先绑定采购员姓名再记偏好。网页填绑定姓名，钉钉回复「绑定 利特」。"
            item = self.memories.remember(
                request.operator, command.get("content") or "",
                user_id=request.user_id,
            )
            return f"已记住：{item['content']}。可说「忘记 {item['content'][:20]}」删掉。"
        if name == "forget":
            if not self.memories or not self.memories.enabled:
                return "记忆未开启。"
            if not self._memory_allowed(request):
                return "请先绑定采购员姓名再改记忆。"
            removed = self.memories.forget(
                request.operator, command.get("keyword") or "",
                user_id=request.user_id,
            )
            if not removed:
                return "没有匹配的记忆。"
            return "已忘记：" + "、".join(item["content"] for item in removed)
        return "无法处理该会话指令。"

    def _memory_allowed(self, request) -> bool:
        if not request.operator and not request.user_id:
            return False
        if self.directory is None:
            return bool(request.operator)
        if request.actor_id and self.directory.get_by_dingtalk_user_id(request.actor_id):
            return True
        return bool(request.operator and self.directory.known_operator(request.operator))

    def _memory_prompt(self, request) -> str:
        if not (self.memories and self.memories.enabled):
            return ""
        if not self._memory_allowed(request):
            return ""
        return self.memories.prompt_block(request.operator, user_id=request.user_id)

    def _busy_reply(self, session: dict, request) -> dict:
        payload = self._command_payload(session["id"], "", request, "上一条还在处理，请稍后再发。")
        payload["ok"] = False
        payload["busy"] = True
        return payload

    def _command_payload(self, session_id: str, run_id: str, request, reply: str) -> dict:
        return {
            "ok": True,
            "sessionId": session_id,
            "runId": run_id,
            "operator": request.operator,
            "userId": request.user_id,
            "traceId": request.trace_id,
            "role": request.role,
            "buyerNames": list(request.buyer_names),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "reply": reply,
            "steps": [],
            "pendingActions": [],
        }

    def _chat_locked(self, *, message, session, operator, channel, actor_id, session_key,
                     request=None) -> dict:
        session_id = session["id"]
        operator = operator or session.get("operator") or ""
        request = request or self._request_context(
            operator=operator, channel=channel, actor_id=actor_id, session_key=session_key,
            session_id=session_id,
        )
        operator = request.operator or operator
        run_id = self.audit.start_run(
            session_id=session_id, channel=channel, operator=operator,
            user_id=request.user_id, request=message, model=self.llm.model,
        )
        snapshot = extract_working_set(
            self.sessions.history(session_id),
            self.actions.list(session_id=session_id, status="pending"),
        )
        self.sessions.add_message(session_id, "user", message, run_id=run_id)
        decision = route_message(message, working_set=snapshot)
        if decision.intent is not None and not needs_llm_review(decision.intent):
            return self._dispatch_intent(
                decision.intent, session, request, run_id, route=decision.route,
            )
        memory = self._memory_prompt(request)
        system = self.system_prompt(
            operator, channel, role=request.role, buyer_names=request.buyer_names,
        )
        if decision.intent is not None and needs_llm_review(decision.intent):
            system = system + "\n\n" + intent_review_hint(decision.intent)
        messages = self.sessions.context_messages(
            session_id,
            system=system,
            identity=identity_block(request),
            memory=memory,
            summary=self.sessions.latest_summary(session_id),
            working_set=format_working_set(snapshot),
        )
        steps: list[dict] = []
        pending: list[dict] = []
        started = time.monotonic()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        try:
            reply = self._loop(
                messages, steps, pending, session_id, run_id, request, usage,
                require_tool_until_pending=needs_llm_review(decision.intent),
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
        payload = {
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
        if decision.intent is not None:
            payload["intent"] = decision.intent.name
            payload["intentKind"] = decision.intent.kind
            payload["route"] = decision.route
        return payload

    def handle_intent(self, message: str, *, session_key: str, operator: str = "",
                      channel: str = "web", actor_id: str = "") -> dict | None:
        """固定意图不走 LLM。未识别返回 None，由调用方再走 chat。"""
        message = str(message or "").strip()
        first = route_message(message)
        if first.intent is None:
            return None
        request = self._request_context(
            operator=operator, channel=channel, actor_id=actor_id, session_key=session_key,
        )
        session = self.sessions.ensure(
            channel, session_key, request.operator or operator, user_id=request.user_id,
        )
        with self.sessions.lock_for(session["id"]):
            run_id = self.audit.start_run(
                session_id=session["id"], channel=channel,
                operator=request.operator or operator, user_id=request.user_id,
                request=message, model="intent",
            )
            snapshot = extract_working_set(
                self.sessions.history(session["id"]),
                self.actions.list(session_id=session["id"], status="pending"),
            )
            self.sessions.add_message(session["id"], "user", message, run_id=run_id)
            decision = route_message(message, working_set=snapshot)
            intent = decision.intent or first.intent
            return self._dispatch_intent(
                intent, session, request, run_id, route=decision.route or first.route,
            )

    def _dispatch_intent(self, intent, session, request, run_id, *, route="") -> dict:
        started = time.monotonic()
        if getattr(intent, "kind", "invoke") in {KIND_ASK, KIND_REFUSE}:
            reply = str(intent.reply or "需要补充信息后再办。")
            self.sessions.add_message(session["id"], "assistant", reply, run_id=run_id)
            self.audit.finish_run(
                run_id, status="ok", reply=reply, steps=0,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {
                "ok": True,
                "sessionId": session["id"],
                "runId": run_id,
                "operator": request.operator,
                "userId": request.user_id,
                "traceId": request.trace_id,
                "role": request.role,
                "buyerNames": list(request.buyer_names),
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "reply": reply,
                "steps": [],
                "pendingActions": [],
                "intent": intent.name,
                "intentKind": intent.kind,
                "route": route,
            }
        calls = intent_calls(intent)
        if not calls:
            fallback = (
                "locate_insole_orders" if intent.name == INSOLE_QUERY
                else "process_insole_orders" if intent.name == INSOLE_PROCESS
                else ""
            )
            if fallback:
                calls = [(fallback, dict(intent.arguments or {}))]
        if not calls:
            result, action = {"error": f"未实现的意图 {intent.name}"}, None
            calls = [("", {})]
        replies = []
        steps = []
        pending = []
        status = "ok"
        result, action = {}, None
        for tool_name, arguments in calls:
            if not tool_name:
                result = {"error": f"未实现的意图 {intent.name}"}
                action = None
            else:
                result, action = self._invoke(
                    {"function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments or {}, ensure_ascii=False),
                    }},
                    session["id"], run_id, request,
                )
            if isinstance(result, dict) and result.get("error"):
                replies.append(str(result["error"]))
                status = "error"
                steps.append({"tool": tool_name or intent.name, "status": "error"})
                break
            if action:
                preview = action.get("preview") or {}
                chunk = str(preview.get("markdown") or action.get("title") or "请核对订单后确认。")
                note = preview.get("note")
                if note:
                    chunk = f"{chunk}\n{note}"
                replies.append(chunk)
                pending.append(action)
                status = "awaiting_confirm"
                steps.append({"tool": tool_name or intent.name, "status": status})
                break
            data = result.get("data") if isinstance(result, dict) else None
            chunk = ""
            if isinstance(data, dict):
                chunk = str(data.get("markdown") or "")
            if not chunk and isinstance(result, dict):
                chunk = str(result.get("summary") or result.get("message") or "已按你的要求调用工具。")
            replies.append(chunk)
            steps.append({"tool": tool_name or intent.name, "status": "ok"})
        reply = "\n\n".join(item for item in replies if item)
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
            "intentKind": getattr(intent, "kind", "invoke"),
            "route": route,
        }

    def _loop(self, messages, steps, pending, session_id, run_id, request, usage,
              *, require_tool_until_pending: bool = False) -> str:
        for step in range(self.max_steps):
            tool_choice = "required" if require_tool_until_pending and not pending else "auto"
            answer = self.llm.chat(
                messages, tools=self.registry.schemas(), tool_choice=tool_choice,
            )
            self._add_usage(answer.get("usage"), usage)
            tool_calls = answer.get("tool_calls") or []
            if len(tool_calls) > self.max_tool_calls:
                tool_calls = tool_calls[:self.max_tool_calls]
            if not tool_calls:
                if require_tool_until_pending and not pending:
                    messages.append({
                        "role": "system",
                        "content": (
                            "还没有待确认动作。必须调用 process_insole_orders，"
                            "不要只口头让员工确认。"
                        ),
                    })
                    continue
                content = str(answer.get("content") or "").strip()
                return self._with_pending_preview(
                    content or "本轮模型没有给出可展示的回复，请换个说法再问一次。",
                    pending,
                )
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
        return self._with_pending_preview(
            "这轮需要的工具调用超过了步数上限，请把问题拆小一点再问（比如只问一个采购单或一个档位）。",
            pending,
        )

    @staticmethod
    def _with_pending_preview(reply: str, pending: list) -> str:
        """模型话术可以审核，清单数字必须用工具预览原文。"""
        chunks = []
        for action in pending or []:
            preview = action.get("preview") or {}
            markdown = str(preview.get("markdown") or "").strip()
            note = str(preview.get("note") or "").strip()
            if markdown:
                chunks.append(markdown)
            if note and note not in markdown:
                chunks.append(note)
        text = str(reply or "").strip()
        extra = "\n".join(chunks)
        if extra and extra not in text:
            return f"{extra}\n\n{text}" if text else extra
        return text

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
                        raise ToolError(
                            "还没绑定采购员姓名。请到群里发「绑定 利特」或「绑定 利特、李佳冬（利特）」，管理员同意后生效。"
                        )
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

        def pending_oids(item: dict) -> set[str]:
            preview_ids = (item.get("preview") or {}).get("oIds") or []
            argument_ids = (item.get("arguments") or {}).get("o_ids") or []
            return {str(oid) for oid in (preview_ids or argument_ids) if oid}

        def pending_size(item: dict) -> int:
            preview = item.get("preview") or {}
            return int(preview.get("processableCount") or len(pending_oids(item)))

        if not wanted:
            keep = max(opens, key=pending_size)
            self._cancel_extra_insole(opens, keep["id"], operator, actor_id)
            return keep
        keep = next((item for item in opens if pending_oids(item) == wanted), None)
        if keep is None:
            keep = next(
                (item for item in opens if wanted <= pending_oids(item)),
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
        request = self._request_context(
            operator=operator, channel=channel, actor_id=actor_id,
        )
        try:
            self._assert_tool_allowed(tool, request)
        except PermissionDenied as exc:
            raise ActionError(str(exc), 403) from exc
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
        request = self._request_context(
            operator=operator, channel=channel, actor_id=actor_id,
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

    def pending(self, *, session_id: str | None = None, limit: int = 20,
                operator: str = "", actor_id: str = "", user_id: str = "") -> list[dict]:
        return self.actions.list(
            session_id=session_id, limit=limit,
            operator=operator, actor_id=actor_id, user_id=user_id,
        )
