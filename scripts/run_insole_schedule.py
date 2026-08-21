#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动跑一轮抖音换鞋垫定时任务，或只看调度状态。

    .venv/bin/python scripts/run_insole_schedule.py --status
    .venv/bin/python scripts/run_insole_schedule.py
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import INSOLE_SCHEDULER  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="抖音换鞋垫定时任务")
    parser.add_argument("--status", action="store_true", help="只打印调度状态")
    parser.add_argument("--operator", default="cli", help="操作人，写入审计")
    parser.add_argument("--no-notify", action="store_true", help="不发钉钉，只跑写入")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(INSOLE_SCHEDULER.status(), ensure_ascii=False, indent=2))
        return 0
    result = INSOLE_SCHEDULER.run_once(
        trigger="manual", operator=args.operator, notify=not args.no_notify,
    )
    print(result.get("reply") or json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") or result.get("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
