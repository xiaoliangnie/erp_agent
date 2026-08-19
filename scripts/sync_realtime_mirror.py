# -*- coding: utf-8 -*-
"""手工执行一次供应链代理 API → hanli.env 增量同步。"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import load_all_env  # noqa: E402
from backend.realtime_mirror import RealtimeMirror, SupplyProxyClient  # noqa: E402


def setting(values, name, default=""):
    return os.environ.get(name, values.get(name, default))


def parsed_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("时间格式应为 ISO 8601，例如 2026-08-01T00:00:00+08:00") from exc


def main():
    parser = argparse.ArgumentParser(description="同步订单、采购单、入库单、商品和供应商到 MySQL 实时镜像")
    parser.add_argument("--env", default=str(ROOT / "hanli.env"), help="目标 MySQL env 文件")
    parser.add_argument("--config", default=str(ROOT / ".env"), help="API 配置文件")
    parser.add_argument(
        "--source", choices=["all", "purchase", "orders", "products", "suppliers", "purchasein"],
        default="all",
    )
    parser.add_argument("--since", help="覆盖同步开始时间（ISO 8601）")
    parser.add_argument("--until", help="覆盖同步结束时间（ISO 8601）")
    args = parser.parse_args()
    values = load_all_env(args.config) if Path(args.config).exists() else {}
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
    from backend.paths import resolve_repo_path
    image_dir = resolve_repo_path(setting(values, "PRODUCT_IMAGE_CACHE_DIR", "files/data/product-images"))
    mirror = RealtimeMirror(
        args.env, client,
        page_size=int(setting(values, "REALTIME_SYNC_PAGE_SIZE", "50") or 50),
        initial_days=int(setting(values, "REALTIME_SYNC_INITIAL_DAYS", "30") or 30),
        purchasein_initial_days=int(setting(values, "REALTIME_PURCHASEIN_INITIAL_DAYS", "2000") or 2000),
        overlap_minutes=int(setting(values, "REALTIME_SYNC_OVERLAP_MINUTES", "5") or 5),
        chunk_days=int(setting(values, "REALTIME_SYNC_CHUNK_DAYS", "7") or 7),
        request_interval=float(setting(values, "REALTIME_SYNC_REQUEST_INTERVAL", "1.05") or 1.05),
        image_dir=image_dir,
    )
    since, until = parsed_time(args.since), parsed_time(args.until)
    result = (
        mirror.sync_all(since=since, until=until)
        if args.source == "all"
        else mirror.sync_source(args.source, since=since, until=until)
    )
    print(result)


if __name__ == "__main__":
    main()
