# -*- coding: utf-8 -*-
"""钉钉消息发送。

两条通道，按 `.env` 配了哪个就用哪个：

- 群自定义机器人 Webhook（`DINGTALK_WEBHOOK_URL` + `DINGTALK_WEBHOOK_SECRET`）：
  最简单，@ 人只能用手机号；
- 企业内部应用机器人（`DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET`）：
  可以按 userId @ 人，也是 Stream 模式下回消息用的通道。

全部走标准库 HTTP，超时固定，不新增运行期依赖。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


OPEN_API_BASE = "https://api.dingtalk.com"
OAPI_BASE = "https://oapi.dingtalk.com"
TIMEOUT_SECONDS = 15


def encode_multipart(fields, files) -> tuple[bytes, str]:
    """拼 multipart/form-data。fields=(name,value)；files=(name,filename,bytes,content_type)。"""
    import secrets
    boundary = secrets.token_hex(16)
    body = bytearray()
    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, filename, content, content_type in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class DingTalkError(RuntimeError):
    """钉钉调用失败；消息可回给员工，但不含密钥。"""


def _ssl_context():
    """macOS 官方 Python 缺系统 CA；与 Stream / Codex 一样走 certifi。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=_ssl_context(),
        ) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DingTalkError(f"钉钉接口返回 {exc.code}：{exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise DingTalkError(f"钉钉接口连接失败：{exc.reason}") from exc
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise DingTalkError("钉钉接口返回的不是合法 JSON") from exc


class DingTalkSender:
    def __init__(self, *, webhook_url: str = "", webhook_secret: str = "", client_id: str = "",
                 client_secret: str = "", robot_code: str = "", group_conversation_id: str = ""):
        self.webhook_url = str(webhook_url or "").strip()
        self.webhook_secret = str(webhook_secret or "").strip()
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.robot_code = str(robot_code or "").strip() or self.client_id
        self.group_conversation_id = str(group_conversation_id or "").strip()
        self._token = ""
        self._token_expires = 0.0

    @property
    def webhook_ready(self) -> bool:
        return bool(self.webhook_url)

    @property
    def app_ready(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def configured(self) -> bool:
        return self.webhook_ready or self.app_ready

    def status(self) -> dict:
        return {
            "webhook": self.webhook_ready,
            "app": self.app_ready,
            "robotCode": bool(self.robot_code),
            "groupConversationId": bool(self.group_conversation_id),
        }

    def access_token(self) -> str:
        """取企业内部应用 access_token，带过期缓存。"""
        if not self.app_ready:
            raise DingTalkError("未配置 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET")
        if self._token and time.time() < self._token_expires - 120:
            return self._token
        body = _post_json(f"{OPEN_API_BASE}/v1.0/oauth2/accessToken", {
            "appKey": self.client_id, "appSecret": self.client_secret,
        })
        token = str(body.get("accessToken") or "")
        if not token:
            raise DingTalkError("钉钉没有返回 accessToken")
        self._token = token
        self._token_expires = time.time() + float(body.get("expireIn") or 7200)
        return token

    def _signed_webhook(self) -> str:
        if not self.webhook_secret:
            return self.webhook_url
        timestamp = str(round(time.time() * 1000))
        digest = hmac.new(
            self.webhook_secret.encode("utf-8"),
            f"{timestamp}\n{self.webhook_secret}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        joiner = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{joiner}timestamp={timestamp}&sign={sign}"

    def send_markdown(self, title: str, text: str, *, at_user_ids=(), at_mobiles=(),
                      at_all: bool = False) -> dict:
        """向配置好的群发 markdown 消息，优先用应用机器人（可按 userId @ 人）。"""
        if self.app_ready and self.group_conversation_id:
            return self._send_group_message(title, text, at_user_ids=at_user_ids, at_mobiles=at_mobiles)
        if not self.webhook_ready:
            raise DingTalkError("未配置钉钉发送通道：需要 DINGTALK_WEBHOOK_URL，或应用机器人 + 群会话 ID")
        mentions = "".join(f"@{mobile}" for mobile in at_mobiles)
        body = _post_json(self._signed_webhook(), {
            "msgtype": "markdown",
            "markdown": {"title": title[:60], "text": f"{text}\n\n{mentions}".strip()},
            "at": {"atMobiles": list(at_mobiles), "isAtAll": bool(at_all)},
        })
        if int(body.get("errcode", 0) or 0) != 0:
            raise DingTalkError(f"钉钉群机器人拒绝：{body.get('errmsg')}")
        return {"channel": "webhook", "atMobiles": list(at_mobiles), "response": body}

    def _send_group_message(self, title: str, text: str, *, at_user_ids=(), at_mobiles=()) -> dict:
        mentions = "".join(f"@{mobile}" for mobile in at_mobiles)
        body = _post_json(
            f"{OPEN_API_BASE}/v1.0/robot/groupMessages/send",
            {
                "robotCode": self.robot_code,
                "openConversationId": self.group_conversation_id,
                "atUserIds": list(at_user_ids),
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps(
                    {"title": title[:60], "text": f"{text}\n\n{mentions}".strip()},
                    ensure_ascii=False,
                ),
            },
            headers={"x-acs-dingtalk-access-token": self.access_token()},
        )
        return {"channel": "app", "atUserIds": list(at_user_ids), "response": body}

    def reply_text(self, *, conversation_id: str, text: str, at_user_ids=()) -> dict:
        """在 Stream 会话里回一条文本消息。"""
        return _post_json(
            f"{OPEN_API_BASE}/v1.0/robot/groupMessages/send",
            {
                "robotCode": self.robot_code,
                "openConversationId": conversation_id,
                "atUserIds": list(at_user_ids),
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text}, ensure_ascii=False),
            },
            headers={"x-acs-dingtalk-access-token": self.access_token()},
        )

    def send_action_card(self, *, conversation_id: str, title: str, text: str,
                         buttons: list[dict]) -> dict:
        """ActionCard 用于 L1/L2 动作的群内确认。"""
        return _post_json(
            f"{OPEN_API_BASE}/v1.0/robot/groupMessages/send",
            {
                "robotCode": self.robot_code,
                "openConversationId": conversation_id,
                "msgKey": "sampleActionCard6",
                "msgParam": json.dumps({
                    "title": title[:60], "text": text,
                    **{f"button_title_{index + 1}": button.get("title", "")
                       for index, button in enumerate(buttons[:6])},
                    **{f"button_url_{index + 1}": button.get("url", "")
                       for index, button in enumerate(buttons[:6])},
                }, ensure_ascii=False),
            },
            headers={"x-acs-dingtalk-access-token": self.access_token()},
        )

    def upload_media(self, path, filetype: str = "file") -> dict:
        """上传文件到钉钉，返回 mediaId。标准库手拼 multipart。"""
        from pathlib import Path
        path = Path(path)
        payload, content_type = encode_multipart(
            [("type", filetype)],
            [("media", path.name, path.read_bytes(), "application/octet-stream")],
        )
        token = urllib.parse.quote(self.access_token())
        url = f"{OAPI_BASE}/media/upload?access_token={token}&type={urllib.parse.quote(filetype)}"
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": content_type}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise DingTalkError(f"钉钉上传返回 {exc.code}：{exc.read().decode('utf-8', 'replace')[:300]}") from exc
        except urllib.error.URLError as exc:
            raise DingTalkError(f"钉钉上传连接失败：{exc.reason}") from exc
        if int(body.get("errcode", 0) or 0) != 0:
            raise DingTalkError(f"钉钉拒绝上传：{body.get('errmsg')}")
        media_id = str(body.get("media_id") or body.get("mediaId") or "")
        if not media_id:
            raise DingTalkError("钉钉没有返回 media_id")
        return {"mediaId": media_id, "type": body.get("type") or filetype}

    def send_file(self, conversation_id: str, media_id: str, file_name: str,
                  file_type: str = "xlsx") -> dict:
        return _post_json(
            f"{OPEN_API_BASE}/v1.0/robot/groupMessages/send",
            {
                "robotCode": self.robot_code,
                "openConversationId": conversation_id,
                "msgKey": "sampleFile",
                "msgParam": json.dumps({
                    "mediaId": media_id, "fileName": file_name, "fileType": file_type,
                }, ensure_ascii=False),
            },
            headers={"x-acs-dingtalk-access-token": self.access_token()},
        )

    def user_id_by_mobile(self, mobile: str) -> str:
        """用手机号反查 userId，便于把 `staff_bindings` 补全。"""
        body = _post_json(
            f"{OAPI_BASE}/topapi/v2/user/getbymobile?access_token={urllib.parse.quote(self.access_token())}",
            {"mobile": str(mobile or "").strip()},
        )
        if int(body.get("errcode", 0) or 0) != 0:
            raise DingTalkError(f"手机号查 userId 失败：{body.get('errmsg')}")
        return str((body.get("result") or {}).get("userid") or "")
