# -*- coding: utf-8 -*-
"""AI 专用 ERP 账号配置。凭证只从 .env 读，明文不进库、不进日志。"""
from __future__ import annotations

from pathlib import Path

from ..paths import resolve_repo_path

DEFAULT_BASE_URL = "https://www.erp321.com/epaas"
DEFAULT_ORDER_LIST_URL = "https://www.erp321.com/app/order/order/list.aspx"
DEFAULT_STORAGE_STATE = "files/data/secrets/erp-ai-state.json"
WORKER_ID = "erp-ai-procurement"
ALLOWED_COMMANDS = ("erp.exchange_items",)


def _flag(value, default=False) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on", "enabled")


def load_digital_worker(setting, *, root: Path | None = None) -> dict:
    """返回可公开的 Digital Worker 状态；密码和 TOTP 只暴露是否已配置。"""
    password = str(setting("ERP_AI_PASSWORD", "") or "")
    totp = str(setting("ERP_AI_TOTP_SECRET", "") or "")
    base = str(setting("ERP_AI_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip()
    order_list = str(
        setting("ERP_AI_ORDER_LIST_URL", DEFAULT_ORDER_LIST_URL) or DEFAULT_ORDER_LIST_URL
    ).strip()
    storage = str(
        setting("ERP_AI_STORAGE_STATE_PATH", DEFAULT_STORAGE_STATE) or DEFAULT_STORAGE_STATE
    ).strip()
    return {
        "enabled": _flag(setting("ERP_AI_ENABLED", "false")),
        "workerId": WORKER_ID,
        "baseUrl": base or DEFAULT_BASE_URL,
        "orderListUrl": order_list or DEFAULT_ORDER_LIST_URL,
        "username": str(setting("ERP_AI_USERNAME", "") or "").strip(),
        "hasPassword": bool(password),
        "hasTotp": bool(totp),
        "headless": _flag(setting("ERP_AI_HEADLESS", "true"), default=True),
        "writeDelayMs": max(50, int(setting("ERP_AI_WRITE_DELAY_MS", "250") or 250)),
        "storageStatePath": str(resolve_repo_path(storage, root=root)),
        "allowedCommands": list(ALLOWED_COMMANDS),
        "loginFields": {
            "account": "#login_id",
            "password": "#password",
            "submit": "立即登录",
            "note": "登录账号填邮箱或手机号，不是采购员花名",
        },
    }


def load_worker_secrets(setting) -> dict:
    """仅给会话层用，禁止写入日志或接口。"""
    return {
        "password": str(setting("ERP_AI_PASSWORD", "") or ""),
        "totp": str(setting("ERP_AI_TOTP_SECRET", "") or ""),
    }
