#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行对话调试入口，不经 HTTP。

复用 `backend.app` 里那一个 Agent 实例，所以工具注册表、缓存、确认流和网页完全一致。

    .venv/bin/python scripts/run_agent_cli.py --operator 张三
    .venv/bin/python scripts/run_agent_cli.py --operator 张三 --ask "今年逾期多少单"
    .venv/bin/python scripts/run_agent_cli.py --status

会话里的命令：`/确认 <id>`、`/取消 <id>`、`/待确认`、`/工具`、`/退出`。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.agent import ActionError, AgentDisabled, LLMError, ToolError  # noqa: E402
from backend.app import AGENT, FORECAST  # noqa: E402


def show(answer):
    print(f"\n助手：{answer['reply']}")
    if answer["steps"]:
        print("  工具：" + "、".join(
            f"{step['tool']}{'(待确认)' if step['actionId'] else ''}{'(失败)' if step['status'] == 'error' else ''}"
            for step in answer["steps"]
        ))
    for action in answer["pendingActions"]:
        print(f"\n  待确认 [{action['id']}] {action['title']}（{action['risk']}，{action['expiresAt']} 前有效）")
        print("  " + json.dumps(action["preview"], ensure_ascii=False, indent=2).replace("\n", "\n  "))
        print(f"  执行：/确认 {action['id']}    放弃：/取消 {action['id']}")


def main():
    parser = argparse.ArgumentParser(description="Agent 命令行调试")
    parser.add_argument("--operator", default="cli", help="操作人姓名，确认动作要用同一个名字")
    parser.add_argument("--session", default="", help="会话键，缺省按操作人")
    parser.add_argument("--ask", default="", help="只问一句就退出")
    parser.add_argument("--status", action="store_true", help="只打印子系统状态")
    args = parser.parse_args()

    if args.status:
        from backend.app import (
            DINGTALK_STREAM, DROPSHIP_SCHEDULER, JOB_WORKER, OUTBOX,
            QUALITY_SCHEDULER, REMINDER_NOTIFIER, REMINDER_SCHEDULER, STAFF_DIRECTORY,
        )
        print(json.dumps({
            "agent": AGENT.status(),
            "forecast": FORECAST.status(),
            "dingtalk": {
                "stream": DINGTALK_STREAM.status(),
                "notifier": REMINDER_NOTIFIER.status(),
                "reminder": REMINDER_SCHEDULER.status(),
            },
            "quality": QUALITY_SCHEDULER.status(),
            "dropship": DROPSHIP_SCHEDULER.status(),
            "jobs": JOB_WORKER.status(),
            "outbox": {"pending": OUTBOX.pending_count()},
            "bindings": len(STAFF_DIRECTORY.list()),
        }, ensure_ascii=False, indent=2))
        return

    session_key = args.session or f"cli-{args.operator}"
    if not AGENT.available:
        print("Agent 未启用：请在 .env 设置 AGENT_ENABLED=true，并配好模型（openai_compatible 要 API Key，codex_oauth 要本机 ChatGPT 登录）")
        return

    def ask(text):
        try:
            show(AGENT.chat(message=text, session_key=session_key, operator=args.operator, channel="cli"))
        except (AgentDisabled, LLMError, ToolError, ActionError, ValueError) as exc:
            print(f"\n失败：{exc}")

    if args.ask:
        ask(args.ask)
        return

    print(f"Agent 已就绪：{AGENT.llm.model} · {len(AGENT.registry.names())} 个工具。/退出 结束。")
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text in ("/退出", "/quit", "/exit"):
            return
        if text == "/工具":
            for tool in AGENT.registry.catalog():
                print(f"  {tool['risk']} {tool['name']}：{tool['description']}")
            continue
        if text == "/待确认":
            for action in AGENT.pending():
                print(f"  [{action['id']}] {action['title']}（{action['status']}）")
            continue
        if text.startswith(("/确认", "/取消")):
            parts = text.split()
            if len(parts) < 2:
                print("  用法：/确认 <动作 id>")
                continue
            try:
                action = (AGENT.confirm(parts[1], args.operator, channel="cli")
                          if parts[0] == "/确认" else AGENT.cancel(parts[1], args.operator))
                print(f"  {action['title']} → {action['status']}")
                if action.get("result"):
                    print("  " + json.dumps(action["result"], ensure_ascii=False, indent=2).replace("\n", "\n  "))
            except (ActionError, ToolError, ValueError, RuntimeError) as exc:
                print(f"  失败：{exc}")
            continue
        ask(text)


if __name__ == "__main__":
    main()
