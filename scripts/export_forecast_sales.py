#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从供应链代理拉销售出库，落到 FORECAST_EXPORT_DIR（默认 D:\\Predict_DATA）。

不写镜像库。默认只探一页；全量用 --days。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import load_all_env  # noqa: E402
from backend.realtime_mirror import (  # noqa: E402
    ProxyAPIError,
    SupplyProxyClient,
    extract_page,
    _format_api_time,
)

SALES_OUT_ROUTE = "/api/proxy/v1/jushuitan/sales/out/query"
DEFAULT_EXPORT_DIR = Path(r"D:\Predict_DATA")
SAFE_TOP_KEYS = (
    "io_id", "o_id", "so_id", "io_date", "status", "wms_co_id", "wms_co_name",
    "warehouse", "modified", "items", "datas", "sku_id", "qty", "qty_type",
    "i_id", "name", "type", "io_id",
)


def setting(values, name, default=""):
    return os.environ.get(name, values.get(name, default))


def log(dest: Path, message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    handle = dest / "export.log"
    handle.parent.mkdir(parents=True, exist_ok=True)
    with handle.open("a", encoding="utf-8") as out:
        out.write(line + "\n")


def make_client(values) -> SupplyProxyClient:
    client_secret = setting(values, "SUPPLY_API_CLIENT_SECRET")
    secret_file = str(setting(values, "SUPPLY_API_CLIENT_SECRET_FILE") or "").strip()
    if secret_file:
        secret_path = Path(secret_file)
        if not secret_path.is_absolute():
            secret_path = ROOT / secret_path
        client_secret = secret_path.read_text(encoding="utf-8").strip() or client_secret
    return SupplyProxyClient(
        setting(values, "SUPPLY_API_BASE", "https://api.wjyfek.com"),
        setting(values, "SUPPLY_API_CLIENT_ID"),
        client_secret,
        timeout=int(setting(values, "REALTIME_SYNC_TIMEOUT_SECONDS", "45") or 45),
    )


def _items(record: dict) -> list[dict]:
    for key in ("items", "datas", "sku_items", "order_items"):
        value = record.get(key)
        if isinstance(value, list) and value:
            return [row for row in value if isinstance(row, dict)]
    if record.get("sku_id"):
        return [record]
    return []


def flatten_rows(records: list[dict]) -> list[dict]:
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        io_date = record.get("io_date") or record.get("ioDate") or record.get("created")
        status = str(record.get("status") or "")
        warehouse = (
            record.get("wms_co_id") or record.get("wms_co_name")
            or record.get("warehouse") or ""
        )
        io_id = record.get("io_id") or record.get("ioId") or ""
        items = _items(record)
        if not items:
            continue
        for item in items:
            sku = str(item.get("sku_id") or item.get("skuId") or "").strip()
            if not sku:
                continue
            qty = item.get("qty")
            if qty in (None, ""):
                qty = item.get("qty_type") or item.get("sale_qty") or 0
            try:
                amount = float(qty)
            except (TypeError, ValueError):
                amount = 0.0
            rows.append({
                "sku_id": sku,
                "io_date": str(io_date or "")[:19],
                "qty": amount,
                "status": status,
                "warehouse": str(warehouse),
                "io_id": str(io_id),
            })
    return rows


def redact_probe(value) -> dict:
    """只留字段名和第一条的安全键，避免把收货人写进仓库。"""
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    sample = None
    records = []
    try:
        records, _, _ = extract_page(value, 1, 50)
    except Exception as exc:  # noqa: BLE001 — 探页失败也要留下结构
        return {"parseError": str(exc), "topKeys": sorted(value.keys())}
    if records:
        first = records[0]
        sample = {key: first.get(key) for key in SAFE_TOP_KEYS if key in first}
        sample["_allKeys"] = sorted(first.keys())
        items = _items(first)
        if items:
            sample["_itemKeys"] = sorted(items[0].keys())
            sample["_itemSafe"] = {
                key: items[0].get(key)
                for key in ("sku_id", "i_id", "qty", "name", "properties_value")
                if key in items[0]
            }
    return {
        "topKeys": sorted(value.keys()),
        "recordCount": len(records),
        "sample": sample,
    }


def probe(client: SupplyProxyClient, dest: Path, days: int = 7) -> dict:
    end = datetime.now()
    begin = end - timedelta(days=max(1, days))
    body = {
        "page_index": 1,
        "page_size": 50,
        "modified_begin": _format_api_time(begin),
        "modified_end": _format_api_time(end),
    }
    raw = client.post(SALES_OUT_ROUTE, body)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "probe-sales-out.json").write_text(
        json.dumps(redact_probe(raw), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    records, more, request_id = extract_page(raw, 1, 50)
    rows = flatten_rows(records)
    return {
        "ok": True,
        "requestId": request_id,
        "records": len(records),
        "lines": len(rows),
        "more": more,
        "window": [body["modified_begin"], body["modified_end"]],
        "probeFile": str(dest / "probe-sales-out.json"),
    }


FIELDNAMES = ["sku_id", "io_date", "qty", "status", "warehouse", "io_id"]


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_daily(path: Path, rows: list[dict]) -> dict:
    daily: dict[tuple[str, str], float] = {}
    confirmed = 0
    for row in rows:
        if str(row.get("status") or "").lower() not in ("confirmed", "archive"):
            continue
        confirmed += 1
        day = str(row.get("io_date") or "")[:10]
        sku = str(row.get("sku_id") or "").strip()
        if len(day) < 10 or not sku:
            continue
        key = (sku, day)
        daily[key] = daily.get(key, 0.0) + float(row.get("qty") or 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sku_id", "日期", "数量"])
        writer.writeheader()
        for (sku, day), qty in sorted(daily.items()):
            writer.writerow({"sku_id": sku, "日期": day, "数量": qty})
    return {"confirmedLines": confirmed, "dailyRows": len(daily)}


def export_days(client: SupplyProxyClient, dest: Path, *, days: int,
                page_size: int, chunk_days: int, interval: float,
                status: str = "Confirmed") -> dict:
    csv_dir = dest / "csv"
    win_dir = csv_dir / "windows"
    win_dir.mkdir(parents=True, exist_ok=True)
    end = datetime.now().replace(microsecond=0)
    begin = end - timedelta(days=max(1, days))
    pages = 0
    windows = 0
    skipped = 0
    window_start = begin
    while window_start < end:
        window_end = min(window_start + timedelta(days=chunk_days), end)
        stamp = f"{window_start:%Y%m%d}-{window_end:%Y%m%d}"
        part_path = win_dir / f"{stamp}.csv"
        done_path = win_dir / f"{stamp}.done"
        windows += 1
        if done_path.exists() and part_path.exists():
            skipped += 1
            log(dest, f"{stamp} 已存在，跳过")
            window_start = window_end
            continue
        rows: list[dict] = []
        page = 1
        while True:
            body = {
                "page_index": page,
                "page_size": page_size,
                "modified_begin": _format_api_time(window_start),
                "modified_end": _format_api_time(window_end),
            }
            if status:
                body["status"] = status
            value = client.post(SALES_OUT_ROUTE, body)
            records, more, _request_id = extract_page(value, page, page_size)
            chunk_rows = flatten_rows(records)
            rows.extend(chunk_rows)
            pages += 1
            log(
                dest,
                f"{body['modified_begin'][:10]}~{body['modified_end'][:10]} "
                f"p{page} 单={len(records)} 行={len(chunk_rows)} 窗内行={len(rows)}",
            )
            if not more:
                break
            page += 1
            if page > 2000:
                raise RuntimeError(f"{stamp} 超过 2000 页，把 --chunk-days 调到 1 再跑")
            if interval:
                time.sleep(interval)
        _write_rows(part_path, rows)
        done_path.write_text(str(len(rows)), encoding="utf-8")
        window_start = window_end
        if interval:
            time.sleep(interval)

    all_rows: list[dict] = []
    for path in sorted(win_dir.glob("*.csv")):
        all_rows.extend(_read_rows(path))
    csv_path = csv_dir / "销售出库明细.csv"
    daily_path = csv_dir / "销售出库日汇总.csv"
    _write_rows(csv_path, all_rows)
    daily_stats = _write_daily(daily_path, all_rows)
    statuses = Counter(row.get("status") or "" for row in all_rows)
    summary = {
        "ok": True,
        "days": days,
        "windows": windows,
        "skippedWindows": skipped,
        "pages": pages,
        "lines": len(all_rows),
        "skus": len({row.get("sku_id") for row in all_rows if row.get("sku_id")}),
        "statuses": dict(statuses),
        "window": [_format_api_time(begin), _format_api_time(end)],
        "csv": str(csv_path),
        "dailyCsv": str(daily_path),
        **daily_stats,
    }
    (dest / "export-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return summary


def _train_baseline(dest: Path, csv_path: str) -> None:
    from backend.forecast import BaselineForecaster, ForecastStore, load_from_csv

    dataset = load_from_csv(csv_path)
    model = BaselineForecaster()
    model.fit(dataset)
    store_dir = dest / "models"
    metadata = ForecastStore(store_dir).save(
        model, mark_latest=True,
        metadata={
            "dataset": dataset.summary(),
            "trainWindow": [dataset.start, dataset.end],
            "trainedBy": "scripts/export_forecast_sales.py --train",
        },
    )
    log(dest, "训练完成 " + json.dumps(metadata, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="从供应链代理导出销售出库到本地训练目录")
    parser.add_argument("--config", default=str(ROOT / ".env"))
    parser.add_argument("--dir", default="", help="覆盖 FORECAST_EXPORT_DIR")
    parser.add_argument("--probe", action="store_true", help="只打最近窗口的第一页")
    parser.add_argument("--days", type=int, default=0, help="拉最近 N 天；0 且非 probe 则默认 365")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--chunk-days", type=int, default=3)
    parser.add_argument("--status", default="Confirmed", help="出库状态，空表示不过滤")
    parser.add_argument("--train", action="store_true", help="导完后用日汇总训 Baseline")
    args = parser.parse_args()
    values = load_all_env(args.config) if Path(args.config).exists() else {}
    dest = Path(args.dir or setting(values, "FORECAST_EXPORT_DIR", str(DEFAULT_EXPORT_DIR)))
    dest.mkdir(parents=True, exist_ok=True)
    client = make_client(values)
    try:
        if args.probe or args.days < 0:
            result = probe(client, dest / "raw")
        else:
            days = args.days or 365
            log(dest, f"开始导出 days={days} page_size={args.page_size} chunk={args.chunk_days}")
            result = export_days(
                client, dest, days=days, page_size=max(1, min(args.page_size, 100)),
                chunk_days=max(1, min(args.chunk_days, 7)),
                interval=float(setting(values, "REALTIME_SYNC_REQUEST_INTERVAL", "1.05") or 1.05),
                status=str(args.status or "").strip(),
            )
            log(dest, "导出结束 " + json.dumps(result, ensure_ascii=False))
            if args.train:
                _train_baseline(dest, result["dailyCsv"])
    except ProxyAPIError as exc:
        raise SystemExit(f"代理接口失败：{exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
