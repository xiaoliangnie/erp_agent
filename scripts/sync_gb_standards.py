# -*- coding: utf-8 -*-
"""手工执行一次全国标准信息公共服务平台 → hanli.env 国标目录同步。"""
import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.business_time import business_today  # noqa: E402
from backend.database import load_all_env  # noqa: E402
from backend.gb_standards import (  # noqa: E402
    DEFAULT_CATEGORY_MAP,
    GbStandardsSyncer,
    SamrCatalogClient,
    build_queries,
    fetch_contract_gb_usages,
    fetch_product_categories,
    gb_change_markdown,
    load_category_map,
    match_contract_gb_alerts,
    resolve_product_scope,
)


def setting(values, name, default=""):
    return os.environ.get(name, values.get(name, default))


def main():
    parser = argparse.ArgumentParser(description="同步国家标准目录元数据到镜像库（不下载标准全文）")
    parser.add_argument("--env", default=str(ROOT / "hanli.env"), help="目标 MySQL env 文件")
    parser.add_argument("--config", default=str(ROOT / ".env"), help="同步范围等配置文件")
    parser.add_argument(
        "--scope",
        choices=["products", "filtered", "all"],
        help="products=按商品表分类；filtered=手工 CCS/ICS/关键字；all=全部国家标准目录",
    )
    parser.add_argument("--map", help="商品分类映射 JSON，默认 files/config/gb_category_map.json")
    parser.add_argument("--ccs", help="filtered 范围：中国标准分类号，逗号分隔")
    parser.add_argument("--ics", help="filtered 范围：国际标准分类号，逗号分隔")
    parser.add_argument("--keyword", help="filtered 范围：中文标准名称关键字，逗号分隔")
    parser.add_argument("--status", help="现行 / 即将实施 / 废止；默认不过滤")
    parser.add_argument("--page-size", type=int, help="每页条数，最大 100")
    parser.add_argument("--max-pages", type=int, help="每个检索条件最多拉几页，0 表示拉完")
    parser.add_argument("--dry-run", action="store_true", help="只打印商品分类展开的检索条件，不写库")
    args = parser.parse_args()
    values = load_all_env(args.config) if Path(args.config).exists() else {}
    scope = args.scope or setting(values, "GB_SYNC_SCOPE", "products")
    status = args.status if args.status is not None else setting(values, "GB_SYNC_STATUS", "")
    from backend.paths import resolve_repo_path
    raw_map = args.map or setting(values, "GB_SYNC_CATEGORY_MAP", "")
    map_path = resolve_repo_path(raw_map) if raw_map else Path(DEFAULT_CATEGORY_MAP)
    client = SamrCatalogClient(
        timeout=float(setting(values, "GB_SYNC_TIMEOUT_SECONDS", "30") or 30),
        page_size=int(args.page_size or setting(values, "GB_SYNC_PAGE_SIZE", "50") or 50),
        request_interval=float(setting(values, "GB_SYNC_REQUEST_INTERVAL", "1.2") or 1.2),
    )
    max_pages = int(args.max_pages if args.max_pages is not None else setting(values, "GB_SYNC_MAX_PAGES", "0") or 0)
    query_loader = None
    queries = []
    if scope == "products":
        def query_loader():
            mapping = load_category_map(map_path)
            categories = fetch_product_categories(args.env)
            return resolve_product_scope(categories, mapping, status=status)

        scope_info = query_loader()
        queries = scope_info["queries"]
        print("商品分类 → 国标目录族：")
        for family_id, cats in scope_info["families"].items():
            print(f"  {family_id}：{('、'.join(cats))}")
        if scope_info["unmapped"]:
            print("未映射分类（已跳过，请补 files/config/gb_category_map.json）：" + "、".join(scope_info["unmapped"]))
        if scope_info["ignored"]:
            print("忽略分类：" + "、".join(repr(item) for item in scope_info["ignored"]))
        query_loader = query_loader
    else:
        queries = build_queries(
            scope=scope,
            ccs=args.ccs if args.ccs is not None else setting(values, "GB_SYNC_CCS", ""),
            ics=args.ics if args.ics is not None else setting(values, "GB_SYNC_ICS", ""),
            keywords=args.keyword if args.keyword is not None else setting(values, "GB_SYNC_KEYWORDS", ""),
            status=status,
        )
    print("检索条件：" + "；".join(item["label"] for item in queries))
    if scope == "all":
        print("警告：全量国家标准目录约 8 万条，按默认分页和间隔大约需要半小时。")
    if args.dry_run:
        print(json.dumps({
            "queryCount": len(queries),
            "queries": [
                {"label": item["label"], "ccs": item.get("ccs"), "ics": item.get("ics"),
                 "keyword": item.get("keyword"), "families": item.get("family_ids") or []}
                for item in queries
            ],
        }, ensure_ascii=False, indent=2))
        return
    syncer = GbStandardsSyncer(
        args.env, client,
        queries=None if query_loader else queries,
        query_loader=query_loader,
        max_pages=max_pages,
    )
    result = syncer.sync()
    changes = result.pop("statusChanges", [])
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if changes:
        usages = fetch_contract_gb_usages(args.env, changes)
        alerts = match_contract_gb_alerts(changes, usages)
        print(f"目录状态跃迁 {len(changes)} 条，其中合同已选用 {len(alerts)} 条。")
        if alerts:
            print(gb_change_markdown(alerts, today=business_today().isoformat()))
            print("钉钉推送由运行中的 server.py 在每日同步成功后发出；本命令只打印清单。")
    else:
        print("本轮没有目录状态跃迁。")


if __name__ == "__main__":
    main()
