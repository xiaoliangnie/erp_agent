#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉通道调试：状态、绑定采购员、试发、立刻催办。

不经聊天页。机器人凭证仍从 `.env` 读，和 server.py 是同一套。

    .venv/bin/python scripts/run_dingtalk_cli.py status
    .venv/bin/python scripts/run_dingtalk_cli.py bind --buyer 张三 --mobile 13800000000
    .venv/bin/python scripts/run_dingtalk_cli.py send-test --text "连通性测试"
    .venv/bin/python scripts/run_dingtalk_cli.py remind-now
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import (  # noqa: E402
    DINGTALK_SENDER, DINGTALK_STREAM, DROPSHIP_SCHEDULER, JOB_WORKER, OUTBOX,
    QUALITY_SCHEDULER, REMINDER_NOTIFIER, REMINDER_SCHEDULER, STAFF_DIRECTORY,
)
from backend.dingtalk import DingTalkError, sdk_available  # noqa: E402


def dump(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="钉钉通道调试")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Stream / 发送通道 / 催办 / 品控 / 代发 / 绑定人数")
    sub.add_parser("list", help="列出 staff_bindings")

    bind = sub.add_parser("bind", help="登记采购员 ↔ 钉钉 userId / 手机号")
    bind.add_argument("--buyer", required=True, help="ERP 采购员姓名，可用顿号分开多个署名：利特、李佳冬（利特）")
    bind.add_argument("--user-id", default="", help="钉钉 userId；群内 @ 用")
    bind.add_argument("--mobile", default="", help="手机号；Webhook 机器人只能用这个 @")
    bind.add_argument("--note", default="")
    bind.add_argument("--role", default="", help="viewer / operator / admin；管理员走这条命令可跳过审批")

    resolve = sub.add_parser("resolve-mobile", help="用手机号向钉钉反查 userId（需要应用机器人）")
    resolve.add_argument("--mobile", required=True)

    send = sub.add_parser("send-test", help="往配置好的群发一条测试 markdown")
    send.add_argument("--text", default="采购助手连通性测试")
    send.add_argument("--title", default="连通性测试")

    sub.add_parser("remind-now", help="立刻按今天的四波口径推一次催办（幂等键含日期）")

    args = parser.parse_args()
    if args.command == "status":
        dump({
            "sdkInstalled": sdk_available(),
            "stream": DINGTALK_STREAM.status(),
            "sender": DINGTALK_SENDER.status(),
            "notifier": REMINDER_NOTIFIER.status(),
            "reminder": REMINDER_SCHEDULER.status(),
            "quality": QUALITY_SCHEDULER.status(),
            "dropship": DROPSHIP_SCHEDULER.status(),
            "jobs": JOB_WORKER.status(),
            "outbox": {"pending": OUTBOX.pending_count()},
            "bindings": len(STAFF_DIRECTORY.list()),
        })
        return
    if args.command == "list":
        dump({"bindings": STAFF_DIRECTORY.list()})
        return
    if args.command == "bind":
        dump(STAFF_DIRECTORY.upsert(
            args.buyer, dingtalk_user_id=args.user_id, mobile=args.mobile, note=args.note,
            role=args.role,
        ))
        return
    if args.command == "resolve-mobile":
        try:
            user_id = DINGTALK_SENDER.user_id_by_mobile(args.mobile)
        except DingTalkError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            sys.exit(1)
        dump({"mobile": args.mobile, "dingtalkUserId": user_id})
        return
    if args.command == "send-test":
        try:
            dump(DINGTALK_SENDER.send_markdown(args.title, args.text))
        except DingTalkError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            sys.exit(1)
        return
    if args.command == "remind-now":
        try:
            dump(REMINDER_SCHEDULER.run_once())
        except DingTalkError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
