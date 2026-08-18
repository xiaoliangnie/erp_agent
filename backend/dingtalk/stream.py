# -*- coding: utf-8 -*-
"""钉钉 Stream 模式客户端。

Stream 模式由服务进程主动长连，不需要公网 IP 和回调地址，内网台式机部署即可用。
SDK（`dingtalk-stream`）按阶段引入，没装或没开关时整个线程不启动，网页链路不受影响。

会话隔离用 `conversationId + senderId`；入口层用钉钉消息 ID 去重，断线重连不会
把同一条指令跑两遍——执行层的第二道幂等在 `pending_actions` 里。
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import re
import ssl
import threading
import time

from ..agent.actions import ActionError
from ..agent.router import needs_llm_review, route_message
from ..agent.runner import AgentDisabled
from ..staff_names import parse_buyer_names
from ..agent.users import is_confirmed_admin_name
from ..agent.web_auth import WebAuth, WebAuthError
from .bindings import (
    BindRequests,
    already_bound,
    admin_user_ids,
    apply_binding,
    conflict_note,
    format_pending_binds,
    is_admin,
    is_private_conversation,
    is_super_admin,
    parse_bind_tokens,
)
from .sender import DingTalkError, DingTalkSender


logger = logging.getLogger(__name__)


CONFIRM_PATTERN = re.compile(r"^\s*(确认|执行|同意)\s*[:：#]?\s*([a-f0-9]{6,24})\s*$")
CANCEL_PATTERN = re.compile(r"^\s*(取消|作废|不执行)\s*[:：#]?\s*([a-f0-9]{6,24})\s*$")
BARE_CONFIRM_PATTERN = re.compile(
    r"^\s*(确认|执行|同意|确认执行)(?:\s*[，,。；;：:].*)?\s*$"
)
BARE_CANCEL_PATTERN = re.compile(r"^\s*(取消|作废|不执行)(?:\s*[，,。；;：:].*)?\s*$")
BIND_PATTERN = re.compile(r"^\s*(?:绑定|我是)\s+(.+?)\s*$")
WEB_BIND_PATTERN = re.compile(r"^\s*(绑定网页|绑定网站|绑定\s*web)\s*$")
ADMIN_SET_ROLE = re.compile(r"^\s*设置管理员\s+(.+?)\s*$")
ADMIN_UNSET_ROLE = re.compile(r"^\s*取消管理员\s+(.+?)\s*$")
ADMIN_LIST_BINDS = re.compile(r"^\s*(待绑定|绑定申请|查看绑定)\s*$")
ADMIN_APPROVE_ALL = re.compile(r"^\s*(同意绑定全部|确认绑定全部|同意全部绑定|全部同意绑定)\s*$")
ADMIN_APPROVE = re.compile(r"^\s*(?:同意绑定|确认绑定)(?:\s+(.+))?\s*$")
ADMIN_REJECT_ALL = re.compile(r"^\s*(拒绝绑定全部|拒绝全部绑定|全部拒绝绑定)\s*$")
ADMIN_REJECT = re.compile(r"^\s*(?:拒绝绑定|驳回绑定)(?:\s+(.+))?\s*$")
INTRO_TEXT = (
    "我是采购助手。群里 @我 就能说话，可以帮你查采购和交期、生成合同、更换抖音鞋垫、登记品控。"
    "会改 ERP 或生成文件的动作，要你回「确认」才会执行。"
)
USAGE_TEXT = (
    "功能使用说明：\n"
    "1. 绑定身份：第一次到群里发「绑定 你的采购员姓名」，管理员同意后才能确认写操作。"
    "花名和「真名（花名）」绑一个即可，也可以「绑定 利特、李佳冬（利特）」。"
    "网页再发「绑定网页」，私信里的 20 位码填到对话页，姓名必须对得上。\n"
    "2. 查询采购 / 交期：直接问，例如「604264 到货了吗」「今年逾期多少」。\n"
    "3. 抖音鞋垫更换操作：先说「查询一下现在抖音需要更换的鞋垫订单，进行处理」，核对清单后回「确认」。完成后会发【任务完成】。\n"
    "4. 品控登记：品控 佰特 604264 鞋垫开胶 3 双；品控查询 今天；品控关闭 编号；撤销品控 编号。\n"
    "5. 确认与取消：回复「确认」执行当前待办，「取消」放弃；也可以带编号。\n"
    "6. 换话题 / 记忆：换话题发「新话题」。记住偏好发「记住 …」，删除发「忘记 …」。\n"
    "7. 私聊：员工私聊只收催办和任务确认，查询请到群里 @我。"
)
HELP_TEXT = INTRO_TEXT + "\n\n" + USAGE_TEXT
PRIVATE_HELP_TEXT = (
    "我是采购助手。私聊只接收催办和任务确认，不开放对话。\n\n"
    "功能使用说明：\n"
    "1. 确认任务：回复「确认」执行待办，「取消」放弃；也可以「确认 编号」。\n"
    "2. 其他事情：查询、换鞋垫、品控请到群里 @我。\n"
    "3. 绑定身份：还没绑定请到群里发「绑定 你的采购员姓名」，等管理员同意。"
)
PRIVATE_REFUSE_TEXT = (
    "我是采购助手。私聊只接收催办和任务确认。查询、换鞋垫、品控请到群里 @我。"
    "绑定也请到群里发「绑定 姓名」；网页再发「绑定网页」。"
)
ADMIN_HELP_TEXT = (
    "我是采购助手。你是管理员，私聊可以使用全部能力。\n\n"
    "绑定审批：\n"
    "1. 查看申请：待绑定\n"
    "2. 同意：回复「同意绑定」或「确认绑定」（有多条一次全过）。"
    "如果同时有鞋垫等待办，光回「确认」会先执行待办，不会误批绑定。\n"
    "3. 拒绝：拒绝绑定 1；拒绝绑定全部\n"
    "角色：韩立私信「设置管理员 姓名」/「取消管理员 姓名」。普通管理员权限与韩立相同，只是不能改角色。\n"
    "「绑定 姓名」会把你自己绑到该采购员名。\n\n"
    + USAGE_TEXT
)


def sdk_available() -> bool:
    try:
        return importlib.util.find_spec("dingtalk_stream") is not None
    except (ImportError, ValueError):
        return False


def certifi_ssl_context():
    """macOS 官方 Python 经常缺系统 CA；WSS 握手必须显式带上 certifi。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def patch_stream_ssl() -> str:
    """给 dingtalk-stream 的 websockets.connect 注入 certifi 根证书。

    HTTP 开连接用的是 requests，本机往往已经能通；WSS 走 asyncio SSL，
    在 python.org 安装的 3.11 上会 CERTIFICATE_VERIFY_FAILED。返回 CA 路径
    方便自检；SDK 未安装时只设置环境变量。
    """
    cafile = ""
    try:
        import certifi
        cafile = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", cafile)
    except ImportError:
        pass
    try:
        sdk_stream = importlib.import_module("dingtalk_stream.stream")
    except ImportError:
        return cafile
    websockets = getattr(sdk_stream, "websockets", None)
    if websockets is None or getattr(websockets, "_hanli_certifi_patched", False):
        return cafile
    original = websockets.connect
    ctx = certifi_ssl_context()

    def connect(uri, *args, **kwargs):
        kwargs.setdefault("ssl", ctx)
        return original(uri, *args, **kwargs)

    websockets.connect = connect
    websockets._hanli_certifi_patched = True
    return cafile


class DingTalkStreamChannel:
    """把钉钉消息接到同一个 Agent Core、同一份工具注册表、同一套确认流。"""

    def __init__(self, *, runner, sender: DingTalkSender, client_id: str, client_secret: str,
                 audit, enabled: bool = False, directory=None, quality=None, memories=None,
                 admin_user_ids=(),
                 initial_backoff_seconds: float = 30, max_backoff_seconds: float = 600):
        self.runner = runner
        self.sender = sender
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.audit = audit
        self.enabled = bool(enabled)
        self.directory = directory
        self.quality = quality
        self.memories = memories
        self.admin_user_ids = tuple(
            str(item).strip() for item in admin_user_ids or () if str(item or "").strip()
        )
        self.bind_requests = BindRequests(directory.store) if directory is not None else None
        self.initial_backoff_seconds = max(0.05, float(initial_backoff_seconds))
        self.max_backoff_seconds = max(self.initial_backoff_seconds, float(max_backoff_seconds))
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._client = None
        self._event_loop = None
        self._session_started = False
        self.last_error = ""
        self.restart_count = 0
        self._in_flight_confirms: set[str] = set()
        self._confirm_threads: list[threading.Thread] = []

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.client_id and self.client_secret)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "sdkInstalled": sdk_available(),
            "running": bool(self._thread and self._thread.is_alive()),
            "lastError": self.last_error,
            "restartCount": self.restart_count,
        }

    def start(self) -> dict:
        if not self.configured:
            return self.status()
        if not sdk_available():
            self.last_error = "缺少 dingtalk-stream，请先 pip install dingtalk-stream"
            logger.error("DingTalk Stream 未启动：%s", self.last_error)
            return self.status()
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="dingtalk-stream", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        self._interrupt_client()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _supervise(self) -> None:
        backoff = self.initial_backoff_seconds
        while not self._stop.is_set():
            self._session_started = False
            self._worker = threading.Thread(
                target=self._serve_guarded, name="dingtalk-stream-client", daemon=True,
            )
            self._worker.start()
            while self._worker.is_alive() and not self._stop.is_set():
                self._worker.join(timeout=0.5)
            if self._stop.is_set():
                self._interrupt_client()
                break
            self.restart_count += 1
            if self._session_started:
                backoff = self.initial_backoff_seconds
            logger.warning(
                "DingTalk Stream 将在 %.0fs 后重连（第 %s 次）",
                backoff, self.restart_count,
            )
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.max_backoff_seconds)

    def _serve_guarded(self) -> None:
        try:
            self._serve()
            if not self._stop.is_set():
                self.last_error = "Stream 客户端已退出"
                logger.warning("DingTalk Stream 线程退出：%s", self.last_error)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("DingTalk Stream 线程退出：%s", self.last_error)

    def _interrupt_client(self) -> None:
        websocket = getattr(self._client, "websocket", None)
        loop = self._event_loop
        if websocket is None or loop is None:
            return
        close = getattr(websocket, "close", None)
        if close is None:
            return
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(close(), loop)
        except Exception:
            pass

    def _serve(self) -> None:
        stream = importlib.import_module("dingtalk_stream")
        channel = self

        class Handler(stream.ChatbotHandler):
            async def process(self, callback):
                message = stream.ChatbotMessage.from_dict(callback.data)
                channel.remember_incoming_webhook(message)
                # handle 里会走 Playwright Sync API，不能停在 Stream 的 asyncio 循环上。
                reply = await channel.handle_async(
                    text=getattr(getattr(message, "text", None), "content", "") or "",
                    message_id=getattr(message, "message_id", "") or "",
                    conversation_id=getattr(message, "conversation_id", "") or "",
                    sender_id=getattr(message, "sender_staff_id", "")
                    or getattr(message, "sender_id", "") or "",
                    sender_name=getattr(message, "sender_nick", "") or "",
                    conversation_type=getattr(message, "conversation_type", "") or "",
                )
                if reply:
                    self.reply_text(reply, message)
                return stream.AckMessage.STATUS_OK, "OK"

        patch_stream_ssl()
        credential = stream.Credential(self.client_id, self.client_secret)
        client = stream.DingTalkStreamClient(credential)
        client.register_callback_handler(stream.ChatbotMessage.TOPIC, Handler())
        original_start = client.start

        async def start_and_track():
            self._event_loop = asyncio.get_running_loop()
            try:
                await original_start()
            finally:
                self._event_loop = None

        client.start = start_and_track
        self._client = client
        logger.info("钉钉 Stream 客户端已启动")
        self.last_error = ""
        self._session_started = True
        try:
            client.start_forever()
        finally:
            if self._client is client:
                self._client = None

    async def handle_async(self, **kwargs) -> str:
        """把同步 handle 挪出 Stream 的 asyncio 循环，避免 Playwright Sync API 报错。"""
        return await asyncio.to_thread(self.handle, **kwargs)

    def remember_incoming_webhook(self, message) -> None:
        """把入站 sessionWebhook 缓存下来，催办主动群发才能真正 @ 到人。"""
        sender = self.sender
        remember = getattr(sender, "remember_session_webhook", None)
        if remember is None:
            return
        remember(
            getattr(message, "conversation_id", "") or "",
            getattr(message, "session_webhook", "") or "",
            getattr(message, "session_webhook_expired_time", None),
        )

    def handle(self, *, text: str, message_id: str, conversation_id: str, sender_id: str,
               sender_name: str = "", conversation_type: str = "") -> str:
        """处理一条钉钉消息并返回要回复的文本。同一 message_id 只处理一次。"""
        text = re.sub(r"@[^\s]+\s*", "", str(text or "")).strip()
        admin = self._is_admin(sender_id, sender_name)
        private = is_private_conversation(conversation_type)
        if not text:
            return self._help_text(admin=admin, private=private)
        if message_id and not self.audit.record_delivery(
            channel="dingtalk", target=conversation_id, kind="inbound", status="received",
            detail={"senderId": sender_id, "senderName": sender_name, "text": text[:500],
                    "conversationType": conversation_type, "admin": admin},
            idempotency_key=f"dingtalk-msg-{message_id}",
        ):
            return ""
        operator = self._operator(sender_id, sender_name)
        session_key = f"{conversation_id}:{sender_id}"
        try:
            if private and self._is_super_admin(sender_id):
                role_reply = self._handle_super_admin_role(text)
                if role_reply is not None:
                    return role_reply
            if WEB_BIND_PATTERN.match(text):
                return self._issue_web_bind(sender_id, sender_name, private=private, admin=admin)
            if admin:
                admin_reply = self._handle_admin_bind_command(
                    text, sender_id, sender_name,
                    session_key=session_key, operator=operator,
                )
                if admin_reply is not None:
                    return admin_reply
            bind = BIND_PATTERN.match(text)
            if bind:
                if private and not admin:
                    return "绑定请到群里发「绑定 你的采购员姓名」，管理员同意后生效。私聊只收任务确认。"
                if admin:
                    return self._bind_immediate(bind.group(1), sender_id, sender_name)
                return self._request_bind(
                    bind.group(1), sender_id, sender_name,
                    conversation_id=conversation_id,
                )
            confirm = CONFIRM_PATTERN.match(text)
            if confirm:
                return self._handle_confirm(
                    lambda: self.runner.confirm(
                        confirm.group(2), operator, channel="dingtalk", actor_id=sender_id,
                    ),
                    conversation_id=conversation_id, sender_id=sender_id,
                    operator=operator, session_key=session_key,
                )
            if BARE_CONFIRM_PATTERN.match(text):
                return self._handle_confirm(
                    lambda: self.runner.confirm_latest(
                        operator, channel="dingtalk", actor_id=sender_id, session_key=session_key,
                    ),
                    conversation_id=conversation_id, sender_id=sender_id,
                    operator=operator, session_key=session_key,
                )
            cancel = CANCEL_PATTERN.match(text)
            if cancel:
                action = self.runner.cancel(
                    cancel.group(2), operator, channel="dingtalk", actor_id=sender_id,
                )
                return f"已取消：{action['title']}"
            if BARE_CANCEL_PATTERN.match(text):
                action = self.runner.cancel_latest(
                    operator, channel="dingtalk", actor_id=sender_id, session_key=session_key,
                )
                return f"已取消：{action['title']}"
            if text in ("帮助", "help", "?", "？"):
                return self._help_text(admin=admin, private=private)
            if private and not admin:
                return PRIVATE_REFUSE_TEXT
            session_cmd = getattr(self.runner, "handle_session_command", None)
            if session_cmd is not None:
                handled = session_cmd(
                    text, session_key=session_key, operator=operator,
                    channel="dingtalk", actor_id=sender_id,
                )
                if handled is not None:
                    return handled.get("reply") or ""
            if self.quality is not None:
                from ..quality.service import QualityError
                try:
                    handled = self.quality.handle_text(
                        text, reporter=operator, reporter_user_id=sender_id,
                        channel="dingtalk", conversation_id=conversation_id,
                        message_id=message_id or None,
                    )
                except QualityError as exc:
                    return str(exc)
                if handled is not None:
                    return handled
            intent_handler = getattr(self.runner, "handle_intent", None)
            routed = route_message(text)
            if intent_handler is not None and not needs_llm_review(routed.intent):
                intent_answer = intent_handler(
                    text, session_key=session_key, operator=operator,
                    channel="dingtalk", actor_id=sender_id,
                )
                if intent_answer is not None:
                    return self._format_chat_reply(intent_answer, sender_id)
            answer = self.runner.chat(
                message=text, session_key=session_key, operator=operator, channel="dingtalk",
                actor_id=sender_id,
            )
            return self._format_chat_reply(answer, sender_id)
        except AgentDisabled as exc:
            return f"助手暂未启用：{exc}"
        except (ActionError, ValueError) as exc:
            return f"处理失败：{exc}"
        except Exception as exc:
            logger.exception("DingTalk handle error")
            return "处理失败，请稍后再试或联系维护人。"

    def _can_notify(self) -> bool:
        sender = self.sender
        return bool(sender and (getattr(sender, "app_ready", False) or getattr(sender, "webhook_ready", False)))

    def _peek_open_action(self, session_key: str, operator: str, sender_id: str) -> dict | None:
        sessions = getattr(self.runner, "sessions", None)
        actions = getattr(self.runner, "actions", None)
        if sessions is None or actions is None:
            return None
        session = sessions.ensure("dingtalk", session_key, operator)
        found = actions.latest_open(session_id=session["id"])
        if found is None and sender_id:
            found = actions.latest_open(actor_id=sender_id)
        return found

    def _started_message(self, action: dict) -> str:
        preview = action.get("preview") or {}
        count = preview.get("processableCount") or len(preview.get("oIds") or [])
        return (
            f"已开始写入 {count} 单，完成后会再发一条【任务完成】结果日志。"
            f"请稍等，不要重复确认。"
        )

    def _handle_confirm(self, execute, *, conversation_id: str, sender_id: str,
                        operator: str, session_key: str) -> str:
        action = self._peek_open_action(session_key, operator, sender_id)
        if action and action.get("id") in self._in_flight_confirms:
            return "上一批还在写入，完成后会再发【任务完成】，请不要重复确认。"
        if action and action.get("tool") == "process_insole_orders" and self._can_notify():
            self._run_confirm_later(execute, action["id"], conversation_id, sender_id)
            return self._started_message(action)
        return self._format_executed(execute())

    def _run_confirm_later(self, execute, action_id: str, conversation_id: str,
                           sender_id: str) -> None:
        self._in_flight_confirms.add(action_id)

        def worker():
            started = time.monotonic()
            try:
                try:
                    text = self._format_executed(
                        execute(),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as exc:
                    logger.exception("钉钉确认后写入失败")
                    from ..exchange.insole import format_elapsed
                    pretty = format_elapsed(int((time.monotonic() - started) * 1000))
                    extra = f"，用时 {pretty}" if pretty else ""
                    text = f"【任务失败】鞋垫换货执行失败：{exc}{extra}"
                self._notify_done(conversation_id, sender_id, text)
            finally:
                self._in_flight_confirms.discard(action_id)

        thread = threading.Thread(target=worker, name="dingtalk-insole-confirm", daemon=True)
        self._confirm_threads.append(thread)
        thread.start()

    def _notify_done(self, conversation_id: str, sender_id: str, text: str) -> None:
        sender = self.sender
        if sender is None:
            return
        at_ids = [sender_id] if sender_id else []
        try:
            if getattr(sender, "app_ready", False) and conversation_id:
                sender.reply_text(conversation_id=conversation_id, text=text, at_user_ids=at_ids)
            elif getattr(sender, "send_markdown", None):
                sender.send_markdown("鞋垫换货任务完成", text, at_user_ids=at_ids)
            if self.audit is not None:
                self.audit.record_delivery(
                    channel="dingtalk", target=conversation_id or "group",
                    kind="insole_done", status="sent",
                    detail={"senderId": sender_id, "text": text[:500]},
                    idempotency_key=f"insole-done-{conversation_id}-{sender_id}-{hash(text) & 0xFFFFFFFF:x}",
                )
        except DingTalkError as exc:
            logger.exception("任务完成通知发送失败：%s", exc)
        except Exception:
            logger.exception("任务完成通知发送失败")

    def _format_executed(self, action: dict, elapsed_ms: int | None = None) -> str:
        result = action.get("result")
        if action.get("tool") == "process_insole_orders":
            from ..exchange.insole import format_insole_result
            return format_insole_result(
                result if isinstance(result, dict) else {},
                elapsed_ms=elapsed_ms,
            )
        return f"已执行：{action.get('title') or ''}\n{_brief(result)}"

    def _format_chat_reply(self, answer: dict, sender_id: str) -> str:
        reply = str(answer.get("reply") or "")
        for action in answer.get("pendingActions") or []:
            reply += (
                f"\n\n待确认：{action['title']}\n"
                f"回复「确认」执行，「取消」放弃；鞋垫写完会再通知"
                f"（{action['expiresAt']} 前有效）"
            )
        if (self.directory and sender_id and not self.directory.get_by_dingtalk_user_id(sender_id)
                and "绑定" not in reply):
            reply += "\n\n还没绑定采购员姓名。请到群里发「绑定 利特」或「绑定 利特、李佳冬（利特）」，管理员同意后生效。"
        return reply

    def _help_text(self, *, admin: bool, private: bool) -> str:
        if admin:
            return ADMIN_HELP_TEXT
        if private:
            return PRIVATE_HELP_TEXT
        return HELP_TEXT

    def _is_admin(self, sender_id: str, sender_name: str) -> bool:
        return is_admin(
            self.directory, sender_id,
            extra_ids=self.admin_user_ids, sender_name=sender_name,
        )

    def _is_super_admin(self, sender_id: str) -> bool:
        return is_super_admin(self.directory, sender_id)

    def _handle_super_admin_role(self, text: str):
        if self.directory is None:
            return None
        promote = ADMIN_SET_ROLE.match(text)
        demote = ADMIN_UNSET_ROLE.match(text)
        if promote is None and demote is None:
            return None
        name = ((promote or demote).group(1) or "").strip()
        if not name:
            return "请写明姓名，例如「设置管理员 利特」。"
        if demote is not None and is_confirmed_admin_name(name):
            return "韩立是最高管理员，不能取消。"
        try:
            updated = self.directory.set_role(name, "admin" if promote else "operator")
        except ValueError as exc:
            return str(exc)
        label = updated.get("buyerName") or name
        if promote:
            return f"已将「{label}」设为管理员，可审批绑定。"
        return f"已取消「{label}」的管理员角色，仍保留钉钉绑定。"

    def _issue_web_bind(self, sender_id: str, sender_name: str, *, private: bool, admin: bool) -> str:
        if private and not admin:
            return "绑定网页请到群里 @我 发「绑定网页」，身份码会私信给你。"
        if self.directory is None:
            return "员工目录未就绪，暂时不能发网页身份码。"
        bound = self.directory.get_by_dingtalk_user_id(sender_id)
        if not bound:
            return "请先到群里发「绑定 你的采购员姓名」，管理员同意后再发「绑定网页」。"
        user_id = str(bound.get("userId") or "")
        users = getattr(self.runner, "users", None)
        if not user_id and users is not None:
            hit = users.resolve_by_dingtalk(sender_id)
            if getattr(hit, "matched", False):
                user_id = hit.user_id
        try:
            issued = WebAuth(self.directory.store).issue_code(
                sender_id=sender_id,
                buyer_name=bound.get("buyerName") or sender_name,
                user_id=user_id,
            )
        except WebAuthError as exc:
            return str(exc)
        code_text = (
            f"网页身份码：{issued['code']}\n"
            f"对应采购员：{issued['buyerName']}\n"
            "10 分钟内有效，只能用一次。在网页填写与钉钉绑定一致的姓名和这串码。"
            "不要把码发到群里或转给别人。"
        )
        sent = self._send_oto(sender_id, "网页身份码", code_text)
        if private:
            return code_text
        if sent:
            return "身份码已私信给你，10 分钟内在网页填写姓名和 20 位码。不要把码发到群里。"
        return "身份码已生成，但私信发送失败。请再发一次「绑定网页」，或检查机器人是否已开通单聊。"

    def _operator(self, sender_id: str, sender_name: str) -> str:
        if self.directory:
            bound = self.directory.get_by_dingtalk_user_id(sender_id)
            if bound.get("buyerName"):
                return bound["buyerName"]
        return (sender_name or sender_id or "钉钉用户")[:120]

    def _handle_admin_bind_command(self, text: str, sender_id: str, sender_name: str,
                                   *, session_key: str = "", operator: str = ""):
        if self.bind_requests is None:
            return None
        pending = self.bind_requests.list_pending()
        if ADMIN_LIST_BINDS.match(text):
            return format_pending_binds(pending)
        if pending and BARE_CONFIRM_PATTERN.match(text):
            if session_key and self._peek_open_action(session_key, operator, sender_id):
                return None
            return self._decide_binds("", "approved", sender_id, sender_name)
        numbered = CONFIRM_PATTERN.match(text)
        if numbered:
            item = self.bind_requests.get(numbered.group(2))
            if item and item.get("status") == "pending":
                return self._decide_binds(item["id"], "approved", sender_id, sender_name)
        if ADMIN_APPROVE_ALL.match(text):
            return self._decide_binds("", "approved", sender_id, sender_name)
        if ADMIN_REJECT_ALL.match(text):
            return self._decide_binds("", "rejected", sender_id, sender_name)
        approve = ADMIN_APPROVE.match(text)
        if approve:
            return self._decide_binds(approve.group(1) or "", "approved", sender_id, sender_name)
        reject = ADMIN_REJECT.match(text)
        if reject:
            raw = (reject.group(1) or "").strip()
            if not raw:
                return "请写明要拒绝的编号，例如「拒绝绑定 1」或「拒绝绑定全部」。"
            return self._decide_binds(raw, "rejected", sender_id, sender_name)
        return None

    def _decide_binds(self, raw: str, status: str, sender_id: str, sender_name: str) -> str:
        pending = self.bind_requests.list_pending()
        if not pending:
            return "没有待审批的绑定申请。"
        try:
            ids = parse_bind_tokens(raw, pending)
        except ValueError as exc:
            return str(exc)
        if not ids:
            return "没有待审批的绑定申请。"
        decided_by = self._operator(sender_id, sender_name)
        done = []
        for request_id in ids:
            item = self.bind_requests.get(request_id)
            if not item:
                done.append(f"{request_id}：找不到申请")
                continue
            if item["status"] != "pending":
                done.append(f"{request_id}：已是 {item['status']}（{item.get('decidedBy') or '他人'}）")
                continue
            if status == "approved":
                conflict = conflict_note(
                    self.directory,
                    names=item.get("names") or [],
                    sender_id=item.get("senderId") or "",
                )
                if conflict:
                    done.append(f"{request_id}：冲突未同意，{conflict}")
                    continue
            decided = self.bind_requests.decide(
                request_id, status=status, decided_by=decided_by,
            )
            names = "、".join(decided.get("names") or [])
            who = decided.get("senderName") or decided.get("senderId")
            if decided.get("decidedBy") and decided["decidedBy"] != decided_by:
                done.append(f"{request_id}：已被 {decided['decidedBy']} 处理")
                continue
            if status == "approved":
                try:
                    apply_binding(
                        self.directory,
                        names=decided.get("names") or [],
                        sender_id=decided["senderId"],
                        sender_name=decided.get("senderName") or "",
                        users=getattr(self.runner, "users", None),
                    )
                except ValueError as exc:
                    self.bind_requests.reopen(request_id)
                    done.append(f"{request_id}：{exc}")
                    continue
                self._notify_employee(
                    decided["senderId"],
                    f"管理员已同意绑定「{names}」。之后催办和确认都会对上这个身份。",
                )
                self._notify_group_bind_success(decided)
                done.append(f"已同意 {who} → {names}")
            else:
                self._notify_employee(
                    decided["senderId"],
                    f"管理员已拒绝绑定「{names}」。如需再申请，请到群里重新发「绑定 姓名」。",
                )
                done.append(f"已拒绝 {who} → {names}")
        leftover = self.bind_requests.list_pending()
        text = "\n".join(done)
        if leftover:
            text += "\n\n" + format_pending_binds(leftover)
        return text

    def _request_bind(self, buyer_name: str, sender_id: str, sender_name: str,
                      conversation_id: str = "") -> str:
        names = parse_buyer_names(buyer_name)
        if not names:
            return "绑定姓名不能为空。用法：绑定 利特，或 绑定 利特、李佳冬（利特）"
        if not self.directory or self.bind_requests is None:
            return "身份目录未就绪，请用命令行 scripts/run_dingtalk_cli.py bind 登记。"
        if not sender_id:
            return "这条消息没有钉钉 userId，无法绑定。请用企业内部应用机器人（Stream），不要用自定义 Webhook。"
        if already_bound(self.directory, names=names, sender_id=sender_id):
            joined = "、".join(names)
            return f"已经绑定：钉钉账号 {sender_name or sender_id} → 采购员「{joined}」，无需再申请。"
        note = conflict_note(self.directory, names=names, sender_id=sender_id)
        request = self.bind_requests.create(
            sender_id=sender_id, sender_name=sender_name, names=names, note=note,
            conversation_id=conversation_id,
        )
        joined = "、".join(request.get("names") or names)
        notified = self._notify_admins(self._admin_bind_notice(request))
        extra = f"；注意：{note}" if note else ""
        if notified:
            return (
                f"已提交绑定申请 {request['id']}：钉钉账号 {sender_name or sender_id} → 「{joined}」。"
                f"等管理员同意后生效{extra}。"
            )
        return (
            f"已提交绑定申请 {request['id']}：钉钉账号 {sender_name or sender_id} → 「{joined}」。"
            "还没有可通知的管理员（请先绑定韩立）。"
            f"{extra}"
        )

    def _bind_immediate(self, buyer_name: str, sender_id: str, sender_name: str) -> str:
        names = parse_buyer_names(buyer_name)
        if not names:
            return "绑定姓名不能为空。用法：绑定 利特，或 绑定 利特、李佳冬（利特）"
        if not self.directory:
            return "身份目录未就绪，请用命令行 scripts/run_dingtalk_cli.py bind 登记。"
        if not sender_id:
            return "这条消息没有钉钉 userId，无法绑定。请用企业内部应用机器人（Stream），不要用自定义 Webhook。"
        try:
            bound = apply_binding(
                self.directory, names=names, sender_id=sender_id, sender_name=sender_name,
                users=getattr(self.runner, "users", None),
            )
        except ValueError as exc:
            return str(exc)
        joined = "、".join(bound)
        return (
            f"已绑定：钉钉账号 {sender_name or sender_id} → 采购员「{joined}」。"
            "花名和「真名（花名）」会视为同一个人，催办 @ 和确认都能对上。"
        )

    def _admin_bind_notice(self, request: dict) -> str:
        names = "、".join(request.get("names") or [])
        who = request.get("senderName") or request.get("senderId")
        extra = f"\n注意：{request['note']}" if request.get("note") else ""
        return (
            f"【绑定申请】{request['id']}\n"
            f"钉钉「{who}」申请绑定采购员「{names}」。{extra}\n"
            "回复「同意绑定」或「确认绑定」同意。拒绝用「拒绝绑定 "
            f"{request['id']}」。"
        )

    def _notify_admins(self, text: str) -> int:
        ids = admin_user_ids(self.directory, extra_ids=self.admin_user_ids)
        if not ids:
            return 0
        sent = 0
        for user_id in ids:
            if self._send_oto(user_id, "绑定申请", text):
                sent += 1
        return sent

    def _notify_employee(self, user_id: str, text: str) -> None:
        self._send_oto(user_id, "绑定结果", text)

    def _notify_group_bind_success(self, decided: dict) -> None:
        sender = self.sender
        if sender is None:
            return
        who = decided.get("senderName") or decided.get("senderId") or ""
        names = "、".join(decided.get("names") or [])
        user_id = str(decided.get("senderId") or "").strip()
        conversation_id = str(
            decided.get("conversationId")
            or getattr(sender, "group_conversation_id", "")
            or ""
        ).strip()
        if not conversation_id:
            return
        text = f"绑定成功：钉钉「{who}」已绑定采购员「{names}」。"
        at_ids = [user_id] if user_id else []
        try:
            if getattr(sender, "reply_text", None):
                sender.reply_text(
                    conversation_id=conversation_id, text=text, at_user_ids=at_ids,
                )
            elif getattr(sender, "send_markdown", None):
                sender.send_markdown("绑定成功", text, at_user_ids=at_ids)
        except DingTalkError as exc:
            logger.warning("绑定成功群通知失败：%s", exc)
        except Exception:
            logger.exception("绑定成功群通知失败")

    def _send_oto(self, user_id: str, title: str, text: str) -> bool:
        sender = self.sender
        send = getattr(sender, "send_oto_markdown", None) if sender is not None else None
        if send is None or not user_id:
            return False
        try:
            send(title, text, user_ids=[user_id])
            return True
        except DingTalkError as exc:
            logger.warning("钉钉私信失败：%s", exc)
            return False
        except Exception:
            logger.exception("钉钉私信失败")
            return False


def _brief(result, limit: int = 600) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        parts = [f"{key}：{value}" for key, value in result.items()
                 if isinstance(value, (str, int, float, bool))]
        return "\n".join(parts)[:limit]
    return str(result)[:limit]
