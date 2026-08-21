# -*- coding: utf-8 -*-
"""ERP 店铺设置分组：shops.query 的 group_name。"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..business_time import BUSINESS_TIMEZONE, business_now
from ..paths import DATA_DIR, ROOT
from .channel import OFFLINE_GROUPS

logger = logging.getLogger(__name__)

CACHE_NAME = "shop-groups.json"
CACHE_MAX_AGE = timedelta(hours=6)
PAGE_SIZE = 100


@dataclass
class ShopGroups:
    """shop_id / 店名 → 店铺设置分组。"""

    by_id: dict[str, str] = field(default_factory=dict)
    by_name: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def group_name(self, shop_id: str = "", shop_name: str = "") -> str:
        sid = str(shop_id or "").strip()
        if sid and sid in self.by_id:
            return self.by_id[sid]
        name = str(shop_name or "").strip()
        if name and name in self.by_name:
            return self.by_name[name]
        return ""

    def shop_name(self, shop_id: str) -> str:
        return self.names.get(str(shop_id or "").strip(), "")


def shop_groups_from_records(records: list[dict]) -> ShopGroups:
    groups = ShopGroups()
    for record in records:
        sid = str(record.get("shop_id") or "").strip()
        name = str(record.get("shop_name") or "").strip()
        group = str(record.get("group_name") or "").strip()
        if sid and sid != "0":
            groups.by_id[sid] = group
            if name:
                groups.names[sid] = name
        if name:
            groups.by_name[name] = group
    return groups


def cache_path(root: Path | None = None) -> Path:
    if root is None:
        return DATA_DIR / CACHE_NAME
    return Path(root) / "files" / "data" / CACHE_NAME


def fetch_shop_records(client) -> list[dict]:
    from ..realtime_mirror import SHOPS_ROUTE, extract_page

    records: list[dict] = []
    page = 1
    while page <= 30:
        page_rows, more, _request_id = extract_page(
            client.post(SHOPS_ROUTE, {"page_index": page, "page_size": PAGE_SIZE}),
            page,
            PAGE_SIZE,
        )
        records.extend(page_rows)
        if not more or not page_rows:
            break
        page += 1
    return records


def _slim(record: dict) -> dict:
    return {
        "shop_id": record.get("shop_id"),
        "shop_name": record.get("shop_name"),
        "short_name": record.get("short_name"),
        "group_id": record.get("group_id"),
        "group_name": record.get("group_name"),
        "enabled": record.get("enabled"),
        "shop_site": record.get("shop_site"),
    }


def write_shop_cache(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": business_now().isoformat(timespec="seconds"),
        "shops": [_slim(record) for record in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_shop_cache(path: Path) -> tuple[list[dict], datetime | None]:
    if not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None
    shops = payload.get("shops") if isinstance(payload, dict) else payload
    if not isinstance(shops, list):
        return [], None
    fetched_at = None
    raw = payload.get("fetched_at") if isinstance(payload, dict) else None
    if raw:
        try:
            fetched_at = datetime.fromisoformat(str(raw))
        except ValueError:
            fetched_at = None
    return [row for row in shops if isinstance(row, dict)], fetched_at


def _cache_fresh(fetched_at: datetime | None, now: datetime) -> bool:
    if fetched_at is None:
        return False
    stamp = fetched_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=BUSINESS_TIMEZONE)
    current = now if now.tzinfo else now.replace(tzinfo=BUSINESS_TIMEZONE)
    return current - stamp <= CACHE_MAX_AGE


def _proxy_client(root: Path | None = None):
    from ..database import load_all_env
    from ..realtime_mirror import MirrorError, SupplyProxyClient

    base = Path(root) if root is not None else ROOT
    values: dict[str, str] = {}
    env_file = base / ".env"
    if env_file.exists():
        values.update(load_all_env(env_file))

    def get(key: str, default: str = "") -> str:
        return str(os.environ.get(key) or values.get(key, default) or "")

    secret = get("SUPPLY_API_CLIENT_SECRET")
    secret_file = get("SUPPLY_API_CLIENT_SECRET_FILE").strip()
    if secret_file:
        secret_path = Path(secret_file)
        if not secret_path.is_absolute():
            secret_path = base / secret_path
        try:
            secret = secret_path.read_text(encoding="utf-8").strip() or secret
        except OSError:
            pass
    try:
        return SupplyProxyClient(
            get("SUPPLY_API_BASE", "https://api.wjyfek.com"),
            get("SUPPLY_API_CLIENT_ID"),
            secret,
        )
    except MirrorError:
        return None


def load_shop_groups(
    *,
    client: Any = None,
    cache: Path | None = None,
    root: Path | None = None,
    refresh: bool = False,
    fetch: bool = True,
) -> ShopGroups:
    """优先 shops.query；失败或未过期则用本机缓存。"""
    path = cache if cache is not None else cache_path(root)
    cached, fetched_at = read_shop_cache(path)
    now = business_now()
    if cached and not refresh and _cache_fresh(fetched_at, now):
        groups = shop_groups_from_records(cached)
        logger.info("店铺分组用缓存 %s 店", len(groups.by_id))
        return groups

    proxy = None
    if fetch:
        proxy = client if client is not None else _proxy_client(root)
    if proxy is not None:
        try:
            records = fetch_shop_records(proxy)
            if records:
                write_shop_cache(records, path)
                groups = shop_groups_from_records(records)
                offline = sum(1 for name in groups.by_id.values() if name in OFFLINE_GROUPS)
                logger.info("店铺分组已刷新 %s 店，线下分组 %s", len(groups.by_id), offline)
                return groups
        except Exception as exc:
            logger.warning("店铺分组刷新失败，改用缓存：%s", exc)

    if cached:
        logger.info("店铺分组用缓存 %s 店", len(cached))
        return shop_groups_from_records(cached)
    logger.warning("没有店铺分组，线下只靠店名规则")
    return ShopGroups()
