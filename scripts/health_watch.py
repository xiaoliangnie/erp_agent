#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每 5 分钟拉 `/api/health`，异常时发钉钉。不引入监控系统。

不 import `backend.app`，避免把 HTTP 服务和催办调度装配进巡检进程。

    .venv/bin/python scripts/health_watch.py
    .venv/bin/python scripts/health_watch.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import load_all_env  # noqa: E402
from backend.dingtalk.sender import DingTalkSender  # noqa: E402
from backend.health_watch import (  # noqa: E402
    ALERT_TITLE,
    DEFAULT_LAG_MINUTES,
    DEFAULT_REPEAT_MINUTES,
    DEFAULT_TIMEOUT_SECONDS,
    evaluate_health,
    fetch_health,
    load_state,
    render_alert,
    save_state,
)
from backend.logging_setup import configure_logging  # noqa: E402


ENV_PATH = ROOT / ".env"
ENV = load_all_env(ENV_PATH) if ENV_PATH.exists() else {}
logger = logging.getLogger("health_watch")


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, ENV.get(name, default))


def _int_setting(name: str, default: int) -> int:
    text = setting(name, str(default)).strip()
    return int(text) if text else int(default)


def build_sender() -> DingTalkSender:
    return DingTalkSender(
        webhook_url=setting("DINGTALK_WEBHOOK_URL", ""),
        webhook_secret=setting("DINGTALK_WEBHOOK_SECRET", ""),
        client_id=setting("DINGTALK_CLIENT_ID", ""),
        client_secret=setting("DINGTALK_CLIENT_SECRET", ""),
        robot_code=setting("DINGTALK_ROBOT_CODE", ""),
        group_conversation_id=setting("DINGTALK_GROUP_CONVERSATION_ID", ""),
    )


def resolve_path(value: str, default: str) -> Path:
    text = str(value or default).strip() or default
    path = Path(text)
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description="拉 /api/health，异常时发钉钉告警")
    parser.add_argument("--url", default="", help="健康检查 URL，默认 APP_BASE_URL/api/health")
    parser.add_argument("--state", default="", help="巡检状态文件，默认 data/health_watch_state.json")
    parser.add_argument("--lag-minutes", type=int, default=None, help="镜像滞后阈值（分钟）")
    parser.add_argument("--repeat-minutes", type=int, default=None, help="同一问题重复告警间隔")
    parser.add_argument("--timeout", type=int, default=None, help="HTTP 超时秒数")
    parser.add_argument("--dry-run", action="store_true", help="只评估，不写状态、不发钉钉")
    args = parser.parse_args()

    configure_logging(level=setting("LOG_LEVEL", "INFO"), log_file="", stream=True)

    base = setting("APP_BASE_URL", "http://127.0.0.1:8777").rstrip("/")
    url = args.url or setting("HEALTH_WATCH_URL", "") or f"{base}/api/health"
    state_path = resolve_path(args.state or setting("HEALTH_WATCH_STATE_PATH", ""),
                              "data/health_watch_state.json")
    lag_minutes = args.lag_minutes if args.lag_minutes is not None else _int_setting(
        "HEALTH_WATCH_LAG_MINUTES", DEFAULT_LAG_MINUTES,
    )
    repeat_minutes = args.repeat_minutes if args.repeat_minutes is not None else _int_setting(
        "HEALTH_WATCH_REPEAT_MINUTES", DEFAULT_REPEAT_MINUTES,
    )
    timeout = args.timeout if args.timeout is not None else _int_setting(
        "HEALTH_WATCH_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS,
    )

    previous = load_state(state_path)
    payload, fetch_error = fetch_health(url, timeout=timeout)
    evaluation = evaluate_health(
        payload,
        fetch_error=fetch_error,
        previous=previous,
        lag_minutes=lag_minutes,
        repeat_minutes=repeat_minutes,
    )

    if not evaluation.issues:
        logger.info("健康检查正常：%s", url)
        if not args.dry_run:
            save_state(state_path, evaluation.state)
        return 0

    markdown = render_alert(evaluation.issues, url=url)
    if args.dry_run:
        print(markdown)
        return 0
    if not evaluation.should_alert:
        logger.info("健康问题仍在，冷却期内不重复告警：%s", evaluation.fingerprint)
        save_state(state_path, evaluation.state)
        return 0

    sender = build_sender()
    if not sender.configured:
        logger.error("需要告警但钉钉发送通道未配置\n%s", markdown)
        print(markdown, file=sys.stderr)
        save_state(state_path, evaluation.state)
        return 1
    try:
        sender.send_markdown(ALERT_TITLE, markdown)
    except Exception as exc:
        logger.error("健康告警发送失败：%s: %s\n%s", type(exc).__name__, exc, markdown)
        print(markdown, file=sys.stderr)
        return 1
    logger.warning("已发送健康告警：%s", evaluation.fingerprint)
    save_state(state_path, evaluation.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
