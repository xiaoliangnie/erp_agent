#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产计划表：从员工订货表读需求数，镜像供库存/在途/净销量，写当月计划表。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.spu_plan.alerts import format_plan_alert_markdown, push_plan_workbook  # noqa: E402
from backend.spu_plan.production_plan import build_production_plan  # noqa: E402

DEFAULT_SOURCE = str(ROOT / "files" / "config" / "重点产品订货表.xlsx")


def main() -> int:
    parser = argparse.ArgumentParser(description="写出生产计划表（订货量读员工订货表，不生成）")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="员工订货表工作簿路径")
    parser.add_argument("--env", default=str(ROOT / "hanli.env"), help="镜像库 env")
    parser.add_argument(
        "--xlsx", default="",
        help="输出路径，默认 files/outputs/spu/YYMM-生产计划表.xlsx",
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="生成后把生产计划表 xlsx 发到钉钉群",
    )
    args = parser.parse_args()

    if not Path(args.source).exists():
        raise SystemExit(f"找不到员工订货表：{args.source}")
    if not Path(args.env).exists():
        raise SystemExit(f"没有镜像库配置：{args.env}")

    result = build_production_plan(args.source, args.env, args.xlsx or None)
    print(f"重点产品 {result['styles']} 款，输出 {result['xlsx']}")
    alerts = result.get("alerts") or {}
    print(
        f"当月需补货 {len(alerts.get('replenish') or [])} 款，"
        f"及时入库 {len(alerts.get('inbound') or [])} 款"
    )
    print(format_plan_alert_markdown(alerts))
    if result["added"]:
        print(f"新打标进表 {len(result['added'])} 款：{'、'.join(result['added'][:8])}")
    if result["dropped"]:
        print(f"已摘标退出 {len(result['dropped'])} 款：{'、'.join(result['dropped'][:8])}")
    if result["missingDemand"]:
        preview = "、".join(result["missingDemand"][:8])
        more = "…" if len(result["missingDemand"]) > 8 else ""
        print(f"订货表里没有需求数的款 {len(result['missingDemand'])} 个：{preview}{more}")
    if args.notify:
        from backend.database import load_all_env
        from backend.dingtalk.sender import DingTalkSender

        values = load_all_env(str(ROOT / ".env")) if (ROOT / ".env").exists() else {}

        def val(name, default=""):
            return os.environ.get(name, values.get(name, default))

        sender = DingTalkSender(
            webhook_url=val("DINGTALK_WEBHOOK_URL"),
            webhook_secret=val("DINGTALK_WEBHOOK_SECRET"),
            client_id=val("DINGTALK_CLIENT_ID"),
            client_secret=val("DINGTALK_CLIENT_SECRET"),
            robot_code=val("DINGTALK_ROBOT_CODE"),
            group_conversation_id=val("DINGTALK_GROUP_CONVERSATION_ID"),
        )
        pushed = push_plan_workbook(
            result["xlsx"], sender=sender, force=True, operator="cli",
            today=result.get("today") or "",
        )
        if pushed.get("sent"):
            print("已推送到钉钉群")
        else:
            print(f"未推送：{pushed.get('reason') or '未知原因'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
