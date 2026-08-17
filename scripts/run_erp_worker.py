#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ERP Digital Worker 调试：看状态、登录、探测订单页。不经 HTTP。

    .venv/bin/python scripts/run_erp_worker.py status
    .venv/bin/python scripts/run_erp_worker.py login
    .venv/bin/python scripts/run_erp_worker.py ping

login 会打开有头浏览器。密码只从本机 .env 读，不要写在命令行。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import setting  # noqa: E402
from backend.erp import DigitalRuntime, ErpError  # noqa: E402
from backend.paths import ROOT  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="ERP Digital Worker")
    parser.add_argument("command", choices=("status", "login", "ping"))
    args = parser.parse_args()
    runtime = DigitalRuntime.from_settings(setting, root=ROOT)
    if args.command == "status":
        print(json.dumps(runtime.status(), ensure_ascii=False, indent=2))
        return
    try:
        if args.command == "login":
            result = runtime.login(headed=True)
        else:
            result = runtime.ping()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ErpError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
