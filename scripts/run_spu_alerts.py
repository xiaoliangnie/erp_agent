#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鞋服 SPU 缺货 / 断码 / 缺码。默认只读镜像；--sync 才打代理。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import load_all_env  # noqa: E402
from backend.paths import resolve_repo_path  # noqa: E402
from backend.realtime_mirror import RealtimeMirror, SupplyProxyClient  # noqa: E402
from backend.spu_plan import (  # noqa: E402
    BOARDS,
    DataMissing,
    build_style_alerts,
    format_alert_text,
    normalize_board,
    save_style_snapshot,
    scoped_style_ids,
    style_workbook_path,
    write_style_workbook,
)


def setting(values, name, default=""):
    return os.environ.get(name, values.get(name, default))


def make_mirror(env_path: str, values: dict) -> RealtimeMirror:
    client_secret = setting(values, "SUPPLY_API_CLIENT_SECRET")
    secret_file = str(setting(values, "SUPPLY_API_CLIENT_SECRET_FILE") or "").strip()
    if secret_file:
        secret_path = Path(secret_file)
        if not secret_path.is_absolute():
            secret_path = ROOT / secret_path
        try:
            client_secret = secret_path.read_text(encoding="utf-8").strip() or client_secret
        except OSError as exc:
            if not client_secret:
                raise SystemExit(f"无法读取供应链 API Secret 文件：{secret_path}") from exc
    client = SupplyProxyClient(
        setting(values, "SUPPLY_API_BASE", "https://api.wjyfek.com"),
        setting(values, "SUPPLY_API_CLIENT_ID"),
        client_secret,
        timeout=int(setting(values, "REALTIME_SYNC_TIMEOUT_SECONDS", "45") or 45),
    )
    image_dir = resolve_repo_path(setting(values, "PRODUCT_IMAGE_CACHE_DIR", "files/data/product-images"))
    return RealtimeMirror(
        env_path, client,
        page_size=int(setting(values, "REALTIME_SYNC_PAGE_SIZE", "50") or 50),
        initial_days=int(setting(values, "REALTIME_SYNC_INITIAL_DAYS", "30") or 30),
        overlap_minutes=int(setting(values, "REALTIME_SYNC_OVERLAP_MINUTES", "5") or 5),
        chunk_days=int(setting(values, "REALTIME_SYNC_CHUNK_DAYS", "7") or 7),
        request_interval=float(setting(values, "REALTIME_SYNC_REQUEST_INTERVAL", "1.05") or 1.05),
        image_dir=image_dir,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="写出鞋服 / 自营百货 SPU 总表（不上看板、不推钉钉）")
    parser.add_argument("--env", default=str(ROOT / "hanli.env"), help="镜像库 env")
    parser.add_argument("--config", default=str(ROOT / ".env"), help="供应链代理配置")
    parser.add_argument("--sync", action="store_true", help="按圈定款拉库存 + 近 N 天出库")
    parser.add_argument(
        "--inventory-only", action="store_true",
        help="配合 --sync：只按款补库存，不重拉出库（60 秒同步已在捞出库时用）",
    )
    parser.add_argument(
        "--inventory-history", type=int, default=0, metavar="天数",
        help="按 modified 窗口往历史回填库存（每窗 7 天），补不动销款；代理不转发 i_id 时用这条",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="出库 modified 窗口天数；默认 61（百货看板只回看 30 天）",
    )
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个款式（调试）")
    parser.add_argument(
        "--board", default="apparel",
        help="apparel=鞋服（默认），baihuo=自营百货，all=两张都写",
    )
    parser.add_argument(
        "--xlsx", default="",
        help="总表路径；默认 files/outputs/spu/YYMMDD-鞋服SPU总表.xlsx 或 …-自营百货总表.xlsx",
    )
    parser.add_argument(
        "--json",
        default=str(ROOT / "files" / "data" / "spu-alerts.json"),
        help="预警 JSON 路径",
    )
    parser.add_argument("--text-limit", type=int, default=40)
    args = parser.parse_args()
    raw_board = str(args.board or "apparel").strip().lower()
    if args.days is None:
        args.days = 61

    env_path = args.env
    if not Path(env_path).exists():
        raise SystemExit(f"没有镜像库配置：{env_path}")

    sync_result = None
    if args.inventory_history > 0:
        values = load_all_env(args.config) if Path(args.config).exists() else {}
        mirror = make_mirror(env_path, values)
        print(f"回填近 {args.inventory_history} 天库存历史窗口…", flush=True)
        history = mirror.sync_inventory_history(days_back=args.inventory_history)
        sync_result = {"inventoryHistory": history}
        print(
            f"历史回填 窗口={history['windows']} 页={history['pages']} 行={history['records']}",
            flush=True,
        )
    if args.sync:
        values = load_all_env(args.config) if Path(args.config).exists() else {}
        style_ids = scoped_style_ids(env_path)
        if args.limit > 0:
            style_ids = style_ids[: args.limit]
        what = f"{len(style_ids)} 款库存"
        if not args.inventory_only:
            what += f" + 近 {args.days} 天出库"
        print(f"同步 {what}…", flush=True)
        mirror = make_mirror(env_path, values)
        inventory = mirror.sync_inventory_styles(style_ids)
        sync_result = {**(sync_result or {}), "inventory": inventory}
        extra = ""
        if not args.inventory_only:
            salesout = mirror.sync_salesout(days=args.days)
            sync_result["salesout"] = salesout
            extra = (
                f"；出库 单={salesout['orders']} 行={salesout['items']} "
                f"页={salesout['pages']}"
            )
        print(
            f"库存 款={inventory['styles']} 行={inventory['records']} "
            f"缺行={inventory['missing']}{extra}",
            flush=True,
        )

    raw_board = str(args.board or "apparel").strip().lower()
    boards = list(BOARDS) if raw_board == "all" else [normalize_board(raw_board)]
    last_result = None
    try:
        for board in boards:
            result = build_style_alerts(env_path, board=board)
            last_result = result
            text = format_alert_text(result, limit=max(1, args.text_limit))
            print(text)
            written = save_style_snapshot(env_path, result, board=board)
            print(f"结果表 {board} {written} 款")
            if args.xlsx and len(boards) == 1:
                xlsx = Path(args.xlsx)
            else:
                xlsx = style_workbook_path(board=board)
            write_style_workbook(result, xlsx)
            print(f"总表 {xlsx}")
    except DataMissing as exc:
        raise SystemExit(str(exc) + "。加上 --sync 从代理拉库存和出库。") from exc

    dest = Path(args.json)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(last_result or {})
    if sync_result is not None:
        payload["sync"] = sync_result
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
