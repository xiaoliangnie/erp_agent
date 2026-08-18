#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成代发订单 Excel。默认写空白模板；--live 用 Digital Worker 填今日未安排。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.dropship.workbook import write_blank_dropship_template  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成代发订单 Excel")
    parser.add_argument("--date", help="覆盖业务日，格式 YYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--live", action="store_true", help="登录 ERP 揭开收货并写入当日文件")
    parser.add_argument("--dated", action="store_true", help="同时写一份当日空白表（会覆盖未打开的当日文件）")
    args = parser.parse_args()
    today = None
    if args.date:
        text = args.date.strip()
        from datetime import datetime
        if len(text) == 6 and text.isdigit():
            today = datetime.strptime(text, "%y%m%d").date()
        else:
            today = datetime.strptime(text, "%Y-%m-%d").date()
    if args.live:
        from backend.app import setting
        from backend.dropship.export import export_today_dropship
        from backend.erp import DigitalRuntime, ErpError
        from backend.paths import ROOT
        write_blank_dropship_template(today=today, include_dated=False)
        runtime = DigitalRuntime.from_settings(setting, root=ROOT)
        try:
            result = export_today_dropship(runtime)
        except ErpError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 1
        finally:
            runtime.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = write_blank_dropship_template(today=today, include_dated=args.dated)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
