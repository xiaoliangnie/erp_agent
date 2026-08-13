# -*- coding: utf-8 -*-
"""钉钉 Stream 模式客户端。

Stream 模式由服务进程主动长连，不需要公网 IP 和回调地址，内网台式机部署即可用。
SDK（`dingtalk-stream`）按阶段引入，没装或没开关时整个线程不启动，网页链路不受影响。

会话隔离用 `conversationId + senderId`；入口层用钉钉消息 ID 去重，断线重连不会
把同一条指令跑两遍——执行层的第二道幂等在 `pending_actions` 里。
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re
import ssl
import threading

from ..agent.actions import ActionError
from ..agent.runner import AgentDisabled
from ..staff_names import parse_buyer_names
from .sender import DingTalkSender


CONFIRM_PATTERN = re.compile(r"^\s*(确认|执行|同意)\s*[:：#]?\s*([a-f0-9]{6,24})\s*$")
CANCEL_PATTERN = re.compile(r"^\s*(取消|作废|不执行)\s*[:：#]?\s*([a-f0-9]{6,24})\s*$")
BIND_PATTERN = re.compile(r"^\s*(?:绑定|我是)\s+(.+?)\s*$")
HELP_TEXT = (
    "可以直接问我：查采购单、看交期催办、生成采购合同、订货建议、异常订单换货。\n"
    "第一次先发「绑定 你的采购员姓名」，之后确认动作才对得上网页上的同一个人。\n"
    "ERP 里同一人有花名和「真名（花名）」时，绑其中一个即可，也可以「绑定 利特、李佳冬（利特）」。\n"
    "需要确认的动作我会给出编号，回复「确认 编号」执行，「取消 编号」放弃。"
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
                 audit, enabled: bool = False, directory=None):
        self.runner = runner
        self.sender = sender
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.audit = audit
        self.enabled = bool(enabled)
        self.directory = directory
        self._thread: threading.Thread | None = None
        self.last_error = ""

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
        }

    def start(self) -> dict:
        if not self.configured:
            return self.status()
        if not sdk_available():
            self.last_error = "缺少 dingtalk-stream，请先 pip install dingtalk-stream"
            print(f"DingTalk Stream 未启动：{self.last_error}")
            return self.status()
        if self._thread and self._thread.is_alive():
            return self.status()
        self._thread = threading.Thread(target=self._serve, name="dingtalk-stream", daemon=True)
        self._thread.start()
        return self.status()

    def _serve(self) -> None:
        stream = importlib.import_module("dingtalk_stream")
        channel = self

        class Handler(stream.ChatbotHandler):
            async def process(self, callback):
                message = stream.ChatbotMessage.from_dict(callback.data)
                reply = channel.handle(
                    text=getattr(getattr(message, "text", None), "content", "") or "",
                    message_id=getattr(message, "message_id", "") or "",
                    conversation_id=getattr(message, "conversation_id", "") or "",
                    sender_id=getattr(message, "sender_staff_id", "")
                    or getattr(message, "sender_id", "") or "",
                    sender_name=getattr(message, "sender_nick", "") or "",
                )
                if reply:
                    self.reply_text(reply, message)
                return stream.AckMessage.STATUS_OK, "OK"

        try:
            patch_stream_ssl()
            credential = stream.Credential(self.client_id, self.client_secret)
            client = stream.DingTalkStreamClient(credential)
            client.register_callback_handler(stream.ChatbotMessage.TOPIC, Handler())
            print("钉钉 Stream 客户端已启动")
            client.start_forever()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"DingTalk Stream 线程退出：{self.last_error}")

    def handle(self, *, text: str, message_id: str, conversation_id: str, sender_id: str,
               sender_name: str = "") -> str:
        """处理一条钉钉消息并返回要回复的文本。同一 message_id 只处理一次。"""
        text = re.sub(r"@[^\s]+\s*", "", str(text or "")).strip()
        if not text:
            return HELP_TEXT
        if message_id and not self.audit.record_delivery(
            channel="dingtalk", target=conversation_id, kind="inbound", status="received",
            detail={"senderId": sender_id, "senderName": sender_name, "text": text[:500]},
            idempotency_key=f"dingtalk-msg-{message_id}",
        ):
            return ""
        operator = self._operator(sender_id, sender_name)
        session_key = f"{conversation_id}:{sender_id}"
        try:
            bind = BIND_PATTERN.match(text)
            if bind:
                return self._bind(bind.group(1), sender_id, sender_name)
            confirm = CONFIRM_PATTERN.match(text)
            if confirm:
                action = self.runner.confirm(confirm.group(2), operator, channel="dingtalk")
                return f"已执行：{action['title']}\n{_brief(action.get('result'))}"
            cancel = CANCEL_PATTERN.match(text)
            if cancel:
                action = self.runner.cancel(cancel.group(2), operator)
                return f"已取消：{action['title']}"
            if text in ("帮助", "help", "?", "？"):
                return HELP_TEXT
            answer = self.runner.chat(
                message=text, session_key=session_key, operator=operator, channel="dingtalk",
            )
            reply = answer["reply"]
            for action in answer["pendingActions"]:
                reply += (
                    f"\n\n待确认：{action['title']}\n"
                    f"回复「确认 {action['id']}」执行，「取消 {action['id']}」放弃"
                    f"（{action['expiresAt']} 前有效）"
                )
            if self.directory and sender_id and not self.directory.get_by_dingtalk_user_id(sender_id):
                reply += "\n\n还没绑定采购员姓名。回复「绑定 利特」或「绑定 利特、李佳冬（利特）」。"
            return reply
        except AgentDisabled as exc:
            return f"助手暂未启用：{exc}"
        except (ActionError, ValueError) as exc:
            return f"处理失败：{exc}"
        except Exception as exc:
            print(f"DingTalk handle error: {type(exc).__name__}: {exc}")
            return "处理失败，请稍后再试或联系维护人。"

    def _operator(self, sender_id: str, sender_name: str) -> str:
        if self.directory:
            bound = self.directory.get_by_dingtalk_user_id(sender_id)
            if bound.get("buyerName"):
                return bound["buyerName"]
        return (sender_name or sender_id or "钉钉用户")[:120]

    def _bind(self, buyer_name: str, sender_id: str, sender_name: str) -> str:
        names = parse_buyer_names(buyer_name)
        if not names:
            return "绑定姓名不能为空。用法：绑定 利特，或 绑定 利特、李佳冬（利特）"
        if not self.directory:
            return "身份目录未就绪，请用命令行 scripts/run_dingtalk_cli.py bind 登记。"
        if not sender_id:
            return "这条消息没有钉钉 userId，无法绑定。请用企业内部应用机器人（Stream），不要用自定义 Webhook。"
        bound = []
        for name in names:
            existing = self.directory.get(name)
            binding = self.directory.upsert(
                name,
                dingtalk_user_id=sender_id,
                mobile=existing.get("mobile") or "",
                note=existing.get("note") or sender_name,
            )
            bound.append(binding["buyerName"])
        joined = "、".join(bound)
        return (
            f"已绑定：钉钉账号 {sender_name or sender_id} → 采购员「{joined}」。"
            "花名和「真名（花名）」会视为同一个人，催办 @ 和确认都能对上。"
        )


def _brief(result, limit: int = 600) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        parts = [f"{key}：{value}" for key, value in result.items()
                 if isinstance(value, (str, int, float, bool))]
        return "\n".join(parts)[:limit]
    return str(result)[:limit]
