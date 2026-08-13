# -*- coding: utf-8 -*-
"""从全国标准信息公共服务平台同步国标目录到镜像库。

只拉取目录元数据（标准号、名称、状态、ICS/CCS、发布/实施日期），不下载标准全文 PDF。
权威接口是站点高级检索用的 JSON：`/gb/search/gbAdvancedSearchPage`。
同一条标准以 SAMR 的 `id` 为主键幂等写入；`content_hash` 不变则只刷新 last_seen。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .business_time import BUSINESS_TIMEZONE, business_now, business_today
from .database import (
    REALTIME_PRODUCT_TABLE,
    connect,
    is_transient_mysql_error,
    read_query,
)

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where() if certifi else None)

GB_STANDARDS_TABLE = "gb_standards"
GB_FAMILY_TABLE = "gb_standard_families"
SYNC_STATE_TABLE = "realtime_sync_state"
SYNC_SOURCE_NAME = "gb_standards"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATEGORY_MAP = ROOT / "config" / "gb_category_map.json"

SAMR_ORIGIN = "https://std.samr.gov.cn"
SEARCH_PATH = "/gb/search/gbAdvancedSearchPage"
DETAIL_PATH = "/gb/search/gbDetailed"
SEARCH_REFERER = f"{SAMR_ORIGIN}/gb/search/gbAdvancedSearch"

USER_AGENT = (
    "Mozilla/5.0 (compatible; HanliGBCatalogSync/1.0; "
    "+https://std.samr.gov.cn/gb/search/gbAdvancedSearch)"
)

CONTRACT_GB_STATUSES = ("现行", "即将实施")
_CATEGORY_CODE_RE = re.compile(r"[（(]\d+[）)]")
_TOKEN_SPLIT_RE = re.compile(r"[\s/;，,、;]+")

PAYLOAD_KEYS = (
    "id", "C_STD_CODE", "STD_CODE2", "STD_CODE3", "C_C_NAME", "C_NAME",
    "C_E_NAME", "E_NAME", "STATE", "STATE2", "STD_NATURE", "STD_TYPE",
    "ICS", "CCS", "ICS_NAME1_FULL", "ISSUE_DATE", "ACT_DATE", "ISSUE_ANN_NO",
    "TOTAL_REPE", "TA_CODE", "TA_NAME", "C_PLAN_CODE", "OPEN_HASH_CODE",
    "SYS_INPUTIME", "STD_DOMAIN", "TABLE_NAME",
)

COLUMNS = [
    "samr_id", "standard_no", "standard_no_compact", "name_cn", "name_en",
    "status", "nature", "std_type", "ics", "ics_name", "ccs", "ccs_norm",
    "issue_date", "implement_date", "issue_ann_no", "replaced_standards",
    "committee_code", "committee_name", "plan_code", "open_hash_code",
    "content_hash", "detail_url", "source_payload", "first_seen_at",
    "last_seen_at", "last_changed_at", "api_synced_at",
]

SCHEMA_SQL = [
    f"""
    CREATE TABLE IF NOT EXISTS `{GB_STANDARDS_TABLE}` (
        samr_id VARCHAR(64) NOT NULL PRIMARY KEY,
        standard_no VARCHAR(128) NOT NULL,
        standard_no_compact VARCHAR(128) NOT NULL DEFAULT '',
        name_cn VARCHAR(512) NOT NULL DEFAULT '',
        name_en VARCHAR(512) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL DEFAULT '',
        nature VARCHAR(32) NOT NULL DEFAULT '',
        std_type VARCHAR(32) NOT NULL DEFAULT '',
        ics VARCHAR(128) NOT NULL DEFAULT '',
        ics_name VARCHAR(512) NOT NULL DEFAULT '',
        ccs VARCHAR(128) NOT NULL DEFAULT '',
        ccs_norm VARCHAR(64) NOT NULL DEFAULT '',
        issue_date DATE NULL,
        implement_date DATE NULL,
        issue_ann_no VARCHAR(64) NOT NULL DEFAULT '',
        replaced_standards VARCHAR(512) NOT NULL DEFAULT '',
        committee_code VARCHAR(64) NOT NULL DEFAULT '',
        committee_name VARCHAR(255) NOT NULL DEFAULT '',
        plan_code VARCHAR(64) NOT NULL DEFAULT '',
        open_hash_code VARCHAR(64) NOT NULL DEFAULT '',
        content_hash CHAR(64) NOT NULL,
        detail_url VARCHAR(512) NOT NULL DEFAULT '',
        source_payload JSON NULL,
        first_seen_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        last_changed_at DATETIME NOT NULL,
        api_synced_at DATETIME NOT NULL,
        KEY idx_gb_standard_no (standard_no),
        KEY idx_gb_status (status),
        KEY idx_gb_ccs_norm (ccs_norm),
        KEY idx_gb_ics (ics),
        KEY idx_gb_changed (last_changed_at),
        KEY idx_gb_name (name_cn(64))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{GB_FAMILY_TABLE}` (
        samr_id VARCHAR(64) NOT NULL,
        family_id VARCHAR(64) NOT NULL,
        api_synced_at DATETIME NOT NULL,
        PRIMARY KEY (samr_id, family_id),
        KEY idx_gb_family (family_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{SYNC_STATE_TABLE}` (
        source_name VARCHAR(64) NOT NULL PRIMARY KEY,
        status VARCHAR(32) NOT NULL DEFAULT 'never',
        watermark_modified DATETIME NULL,
        last_started_at DATETIME NULL,
        last_success_at DATETIME NULL,
        last_request_id VARCHAR(128) NOT NULL DEFAULT '',
        rows_synced BIGINT NOT NULL DEFAULT 0,
        error_message VARCHAR(1000) NOT NULL DEFAULT '',
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]

UPSERT_SQL = (
    f"INSERT INTO `{GB_STANDARDS_TABLE}` ("
    + ",".join(f"`{column}`" for column in COLUMNS)
    + ") VALUES ("
    + ",".join(["%s"] * len(COLUMNS))
    + ") ON DUPLICATE KEY UPDATE "
    + ",".join(
        f"`{column}`=VALUES(`{column}`)"
        for column in COLUMNS
        if column not in ("samr_id", "first_seen_at", "last_changed_at", "content_hash")
    )
    + ",`last_changed_at`=IF(`content_hash` <=> VALUES(`content_hash`), "
    "`last_changed_at`, VALUES(`last_changed_at`))"
    + ",`content_hash`=VALUES(`content_hash`)"
)

_TAG_RE = re.compile(r"<[^>]+>")


class GbSyncError(RuntimeError):
    """国标目录同步失败；消息里不含数据库口令。"""


def _plain(value: Any, limit: int = 512) -> str:
    text = html.unescape(_TAG_RE.sub("", str(value or ""))).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _row_value(row: dict, *keys: str, limit: int = 512) -> str:
    for key in keys:
        if row.get(key) not in (None, ""):
            return _plain(row[key], limit=limit)
    return ""


def _day(value: Any) -> str | None:
    text = _plain(value, limit=32)
    if len(text) < 10:
        return None
    text = text.replace("/", "-")[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _class_code(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def parse_csv_list(value: str | None) -> list[str]:
    """逗号/顿号分隔的配置项，去掉空段。"""
    parts = re.split(r"[,，、]", str(value or ""))
    return [item.strip() for item in parts if item.strip()]


def load_category_map(path: str | Path | None = None) -> dict:
    """读取商品分类 → 国标目录族映射。"""
    map_path = Path(path) if path else DEFAULT_CATEGORY_MAP
    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GbSyncError(f"找不到国标分类映射：{map_path.name}") from exc
    except json.JSONDecodeError as exc:
        raise GbSyncError(f"国标分类映射不是合法 JSON：{map_path.name}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("families"), dict):
        raise GbSyncError("国标分类映射缺少 families")
    if not isinstance(data.get("categories"), dict):
        raise GbSyncError("国标分类映射缺少 categories")
    return data


def family_ids_for(category: str, mapping: dict) -> list[str]:
    raw = mapping.get("categories", {}).get(category)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _is_missing_relation(exc: BaseException) -> bool:
    """表或列还不存在：合同页不能因为没跑过国标同步就打不开。"""
    try:
        code = int(exc.args[0])
    except (IndexError, TypeError, ValueError):
        code = None
    if code in {1146, 1054}:
        return True
    text = str(exc).lower()
    return "doesn't exist" in text or "unknown table" in text or "unknown column" in text


def category_tokens(category: str, product_name: str = "") -> list[str]:
    """分类/品名里抽出排序加权用的词，去掉「毛绒（04）」这种运营编号。"""
    text = _CATEGORY_CODE_RE.sub(" ", f"{category} {product_name}")
    tokens: list[str] = []
    seen: set[str] = set()
    for part in _TOKEN_SPLIT_RE.split(text):
        token = part.strip()
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def standard_rank_score(row: dict, *, product_name: str = "", category: str = "") -> tuple:
    """分数越高越靠前：现行 > 即将实施，产品标准优先，名称命中分类词加权。"""
    status = str(row.get("status") or "")
    status_score = 100 if status == "现行" else 40 if status == "即将实施" else 0
    type_score = 25 if str(row.get("std_type") or "") == "产品" else 0
    name_cn = str(row.get("name_cn") or "")
    token_score = sum(60 for token in category_tokens(category, product_name) if token in name_cn)
    issue = str(row.get("issue_date") or "")
    return (status_score + type_score + token_score, issue, str(row.get("standard_no") or ""))


def rank_standards(rows: list[dict], *, product_name: str = "", category: str = "") -> list[dict]:
    """合同下拉的排序；不自动选中。多条同分时较新的发布日期在前。"""
    return sorted(
        rows,
        key=lambda row: standard_rank_score(row, product_name=product_name, category=category),
        reverse=True,
    )


def serialize_gb_option(row: dict) -> dict:
    return {
        "samrId": str(row.get("samr_id") or ""),
        "standardNo": str(row.get("standard_no") or ""),
        "nameCn": str(row.get("name_cn") or ""),
        "status": str(row.get("status") or ""),
        "nature": str(row.get("nature") or ""),
        "stdType": str(row.get("std_type") or ""),
    }


def fetch_sku_categories(env_path: str, sku_ids: list[str]) -> dict[str, str]:
    """SKU → realtime_products.category，供合同按商品表分类找目录族。"""
    unique = [sku for sku in dict.fromkeys(sku_ids) if sku]
    if not unique:
        return {}
    marks = ",".join(["%s"] * len(unique))
    sql = (
        f"SELECT sku_id, category FROM `{REALTIME_PRODUCT_TABLE}` "
        f"WHERE sku_id IN ({marks})"
    )
    try:
        rows = read_query(env_path, sql, tuple(unique)) or []
    except Exception as exc:
        if _is_missing_relation(exc):
            return {}
        raise
    return {
        str(row.get("sku_id") or ""): str(row.get("category") or "")
        for row in rows
    }


def fetch_family_standards(env_path: str, family_ids: list[str]) -> list[dict]:
    """某几个目录族下现行 / 即将实施的标准。表不存在时返回空列表。"""
    unique = [fid for fid in dict.fromkeys(family_ids) if fid]
    if not unique:
        return []
    marks = ",".join(["%s"] * len(unique))
    status_marks = ",".join(["%s"] * len(CONTRACT_GB_STATUSES))
    sql = f"""
        SELECT s.samr_id, s.standard_no, s.name_cn, s.status,
               s.nature, s.std_type, s.issue_date
        FROM `{GB_STANDARDS_TABLE}` s
        WHERE s.status IN ({status_marks})
          AND s.samr_id IN (
              SELECT f.samr_id FROM `{GB_FAMILY_TABLE}` f
              WHERE f.family_id IN ({marks})
          )
    """
    try:
        return list(read_query(env_path, sql, CONTRACT_GB_STATUSES + tuple(unique)) or [])
    except Exception as exc:
        if _is_missing_relation(exc):
            return []
        raise


def lookup_standard_by_no(env_path: str, standard_no: str) -> dict | None:
    code = str(standard_no or "").strip()
    if not code:
        return None
    sql = (
        f"SELECT samr_id, standard_no, name_cn, status, nature, std_type "
        f"FROM `{GB_STANDARDS_TABLE}` WHERE standard_no = %s LIMIT 1"
    )
    try:
        return read_query(env_path, sql, (code,), one=True)
    except Exception as exc:
        if _is_missing_relation(exc):
            return None
        raise


_GB_NO_RE = re.compile(r"^\s*GB\s*/?\s*T?\s*[\d.]+", re.I)


def compact_standard_no(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def looks_like_standard_no(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _GB_NO_RE.match(text):
        return True
    compact = compact_standard_no(text)
    return compact.startswith("GB") and any(ch.isdigit() for ch in compact)


def match_categories(query: str, mapping: dict) -> list[str]:
    """用员工说的品类词去对商品分类键，例如「毛绒」→ 毛绒（04）。"""
    query = str(query or "").strip()
    categories = mapping.get("categories") or {}
    ignore = {str(item) for item in (mapping.get("ignore") or [])}
    if not query:
        return []
    hits: list[str] = []
    if query in categories and query not in ignore:
        hits.append(query)
    tokens = category_tokens(query)
    for category in categories:
        if category in ignore or category in hits:
            continue
        stripped = _CATEGORY_CODE_RE.sub("", category)
        if query in category or query in stripped:
            hits.append(category)
            continue
        if any(token in category or token in stripped for token in tokens):
            hits.append(category)
    return hits


def match_families(query: str, mapping: dict) -> list[str]:
    query = str(query or "").strip()
    if not query:
        return []
    hits: list[str] = []
    for family_id, meta in (mapping.get("families") or {}).items():
        label = str((meta or {}).get("label") or family_id)
        if query == family_id or query in family_id or query in label:
            hits.append(str(family_id))
    return hits


def _fmt_dt(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value).strip()
    return text[:19]


def serialize_gb_record(row: dict) -> dict:
    option = serialize_gb_option(row)
    option["issueDate"] = str(row.get("issue_date") or "")[:10]
    option["implementDate"] = str(row.get("implement_date") or "")[:10]
    option["familyIds"] = [str(item) for item in (row.get("family_ids") or []) if item]
    return option


def _attach_families(env_path: str, rows: list[dict]) -> list[dict]:
    ids = [str(row.get("samr_id") or "") for row in rows if row.get("samr_id")]
    ids = [item for item in dict.fromkeys(ids) if item]
    by_id: dict[str, list[str]] = {}
    if ids:
        marks = ",".join(["%s"] * len(ids))
        try:
            links = read_query(
                env_path,
                f"SELECT samr_id, family_id FROM `{GB_FAMILY_TABLE}` "
                f"WHERE samr_id IN ({marks})",
                tuple(ids),
            ) or []
        except Exception as exc:
            if not _is_missing_relation(exc):
                raise
            links = []
        for link in links:
            samr_id = str(link.get("samr_id") or "")
            family_id = str(link.get("family_id") or "")
            if samr_id and family_id and family_id not in by_id.setdefault(samr_id, []):
                by_id[samr_id].append(family_id)
    for row in rows:
        row["family_ids"] = by_id.get(str(row.get("samr_id") or ""), [])
    return rows


def catalog_status(env_path: str) -> dict:
    """本地国标目录库水位与条数。不触发同步，不含标准全文。"""
    try:
        total_row = read_query(
            env_path, f"SELECT COUNT(*) AS n FROM `{GB_STANDARDS_TABLE}`", one=True,
        ) or {}
    except Exception as exc:
        if _is_missing_relation(exc):
            return {
                "ok": False,
                "tableReady": False,
                "error": "国标目录表还不存在，请先运行 scripts/sync_gb_standards.py",
            }
        raise
    by_status = read_query(
        env_path,
        f"SELECT status, COUNT(*) AS n FROM `{GB_STANDARDS_TABLE}` GROUP BY status",
    ) or []
    family_rows: list[dict] = []
    try:
        family_rows = list(read_query(
            env_path,
            f"SELECT family_id, COUNT(*) AS n FROM `{GB_FAMILY_TABLE}` "
            "GROUP BY family_id ORDER BY n DESC",
        ) or [])
    except Exception as exc:
        if not _is_missing_relation(exc):
            raise
    try:
        state = read_sync_state(env_path)
    except Exception as exc:
        if _is_missing_relation(exc):
            state = {}
        else:
            raise
    stamps = read_query(
        env_path,
        f"SELECT MAX(last_seen_at) AS last_seen, MAX(api_synced_at) AS api_synced, "
        f"MAX(last_changed_at) AS last_changed FROM `{GB_STANDARDS_TABLE}`",
        one=True,
    ) or {}
    return {
        "ok": True,
        "tableReady": True,
        "total": int(total_row.get("n") or 0),
        "byStatus": {str(row.get("status") or "未知"): int(row.get("n") or 0) for row in by_status},
        "families": [
            {"familyId": str(row.get("family_id") or ""), "count": int(row.get("n") or 0)}
            for row in family_rows
        ],
        "familyCount": len(family_rows),
        "sync": {
            "status": str(state.get("status") or "never"),
            "lastStartedAt": _fmt_dt(state.get("last_started_at")),
            "lastSuccessAt": _fmt_dt(state.get("last_success_at")),
            "rowsSynced": int(state.get("rows_synced") or 0),
            "errorMessage": str(state.get("error_message") or ""),
        },
        "lastSeenAt": _fmt_dt(stamps.get("last_seen")),
        "apiSyncedAt": _fmt_dt(stamps.get("api_synced")),
        "lastChangedAt": _fmt_dt(stamps.get("last_changed")),
        "note": "只含目录元数据（标准号、名称、状态、日期），不含标准全文 PDF。问具体商品请用 lookup_gb_standards。",
    }


def fetch_product_by_sku(env_path: str, sku: str) -> dict | None:
    sku = str(sku or "").strip()
    if not sku:
        return None
    sql = (
        f"SELECT sku_id, COALESCE(i_id, '') AS i_id, COALESCE(name, '') AS name, "
        f"COALESCE(category, '') AS category FROM `{REALTIME_PRODUCT_TABLE}` "
        "WHERE sku_id = %s LIMIT 1"
    )
    try:
        row = read_query(env_path, sql, (sku,), one=True)
    except Exception as exc:
        if _is_missing_relation(exc):
            return None
        raise
    if not row:
        return None
    return {
        "sku": str(row.get("sku_id") or ""),
        "styleCode": str(row.get("i_id") or ""),
        "name": str(row.get("name") or ""),
        "category": str(row.get("category") or ""),
    }


def search_products_for_gb(env_path: str, query: str, *, limit: int = 8) -> list[dict]:
    query = str(query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 20))
    pattern = f"%{query}%"
    sql = (
        f"SELECT sku_id, COALESCE(i_id, '') AS i_id, COALESCE(name, '') AS name, "
        f"COALESCE(category, '') AS category FROM `{REALTIME_PRODUCT_TABLE}` "
        "WHERE sku_id LIKE %s OR name LIKE %s OR category LIKE %s "
        "ORDER BY modified DESC, sku_id LIMIT %s"
    )
    try:
        rows = read_query(env_path, sql, (pattern, pattern, pattern, limit)) or []
    except Exception as exc:
        if _is_missing_relation(exc):
            return []
        raise
    return [
        {
            "sku": str(row.get("sku_id") or ""),
            "styleCode": str(row.get("i_id") or ""),
            "name": str(row.get("name") or ""),
            "category": str(row.get("category") or ""),
        }
        for row in rows
    ]


def search_standards_by_no(env_path: str, code: str, *, limit: int = 12) -> list[dict]:
    code = str(code or "").strip()
    if not code:
        return []
    compact = compact_standard_no(code)
    sql = (
        f"SELECT samr_id, standard_no, name_cn, status, nature, std_type, "
        f"issue_date, implement_date FROM `{GB_STANDARDS_TABLE}` "
        "WHERE standard_no = %s OR standard_no_compact = %s "
        "OR standard_no LIKE %s OR standard_no_compact LIKE %s "
        "ORDER BY CASE status WHEN '现行' THEN 0 WHEN '即将实施' THEN 1 ELSE 2 END, "
        "issue_date DESC LIMIT %s"
    )
    try:
        rows = list(read_query(
            env_path, sql, (code, compact, f"%{code}%", f"%{compact}%", limit),
        ) or [])
    except Exception as exc:
        if _is_missing_relation(exc):
            return []
        raise
    return _attach_families(env_path, rows)


def search_standards_by_name(env_path: str, query: str, *, limit: int = 12) -> list[dict]:
    query = str(query or "").strip()
    if not query:
        return []
    marks = ",".join(["%s"] * len(CONTRACT_GB_STATUSES))
    sql = (
        f"SELECT samr_id, standard_no, name_cn, status, nature, std_type, "
        f"issue_date, implement_date FROM `{GB_STANDARDS_TABLE}` "
        f"WHERE name_cn LIKE %s AND status IN ({marks}) "
        "ORDER BY CASE status WHEN '现行' THEN 0 ELSE 1 END, issue_date DESC "
        "LIMIT %s"
    )
    try:
        rows = list(read_query(
            env_path, sql, (f"%{query}%",) + CONTRACT_GB_STATUSES + (limit,),
        ) or [])
    except Exception as exc:
        if _is_missing_relation(exc):
            return []
        raise
    return _attach_families(env_path, rows)


def _latest_saved_standard(env_path: str, sku: str) -> dict | None:
    sku = str(sku or "").strip()
    if not sku:
        return None
    sql = (
        "SELECT standard_no, name_cn, samr_id FROM `contract_line_gb` "
        "WHERE sku_id = %s ORDER BY updated_at DESC LIMIT 1"
    )
    try:
        row = read_query(env_path, sql, (sku,), one=True)
    except Exception as exc:
        if _is_missing_relation(exc):
            return None
        raise
    if not row:
        return None
    return {
        "standardNo": str(row.get("standard_no") or ""),
        "nameCn": str(row.get("name_cn") or ""),
        "samrId": str(row.get("samr_id") or ""),
    }


def _lookup_envelope(**fields) -> dict:
    payload = {
        "ok": True,
        "mode": "",
        "query": "",
        "product": None,
        "products": [],
        "category": "",
        "familyIds": [],
        "mapped": True,
        "total": 0,
        "returned": 0,
        "standards": [],
        "savedStandard": None,
        "note": "这里列的是执行标准（GB/T…），不是商品条码「国标码」。候选有多条时不要擅自指定，合同页按明细勾选。",
    }
    payload.update(fields)
    payload["returned"] = len(payload.get("standards") or [])
    return payload


def _standards_for_families(
    env_path: str, family_ids: list[str], *, product_name: str = "", category: str = "",
    limit: int = 12,
) -> tuple[list[dict], int]:
    unique = [fid for fid in dict.fromkeys(family_ids) if fid]
    rows = rank_standards(
        fetch_family_standards(env_path, unique),
        product_name=product_name, category=category,
    )
    clipped = _attach_families(env_path, [dict(row) for row in rows[:limit]])
    return [serialize_gb_record(row) for row in clipped], len(rows)


def lookup_product_standards(
    env_path: str,
    *,
    sku: str = "",
    query: str = "",
    category: str = "",
    standard_no: str = "",
    family_id: str = "",
    limit: int = 12,
) -> dict:
    """按 SKU / 名称 / 分类 / 标准号查执行标准。不自动替员工选定合同用哪一条。"""
    limit = max(1, min(int(limit or 12), 40))
    sku = str(sku or "").strip()
    query = str(query or "").strip()
    category = str(category or "").strip()
    standard_no = str(standard_no or "").strip()
    family_id = str(family_id or "").strip()
    if query and not standard_no and looks_like_standard_no(query):
        standard_no, query = query, ""
    try:
        mapping = load_category_map()
    except Exception:
        mapping = {"categories": {}, "families": {}, "ignore": []}

    if standard_no:
        rows = search_standards_by_no(env_path, standard_no, limit=limit)
        return _lookup_envelope(
            mode="standard_no", query=standard_no,
            total=len(rows),
            standards=[serialize_gb_record(row) for row in rows],
            note="按标准号检索。现行与即将实施可能同时存在，不要把废止条目写进合同。",
        )

    if sku:
        product = fetch_product_by_sku(env_path, sku)
        if not product:
            return _lookup_envelope(
                mode="sku", query=sku, mapped=False,
                note=f"商品表没有 SKU {sku}。",
            )
        return _lookup_for_product(env_path, product, mapping, limit)

    if family_id:
        records, total = _standards_for_families(env_path, [family_id], limit=limit)
        return _lookup_envelope(
            mode="family", query=family_id, familyIds=[family_id],
            mapped=bool(records or family_id in (mapping.get("families") or {})),
            total=total, standards=records,
        )

    if category:
        family_ids = family_ids_for(category, mapping)
        if not family_ids:
            matched = match_categories(category, mapping)
            for item in matched:
                family_ids.extend(family_ids_for(item, mapping))
            family_ids = list(dict.fromkeys(family_ids))
        if not family_ids:
            return _lookup_envelope(
                mode="category", query=category, category=category, mapped=False,
                note=f"分类「{category}」尚未映射到国标目录族。",
            )
        records, total = _standards_for_families(
            env_path, family_ids, category=category, limit=limit,
        )
        return _lookup_envelope(
            mode="category", query=category, category=category,
            familyIds=family_ids, total=total, standards=records,
        )

    if query:
        products = search_products_for_gb(env_path, query, limit=8)
        if len(products) == 1:
            return _lookup_for_product(env_path, products[0], mapping, limit)
        if len(products) > 1:
            cats = list(dict.fromkeys(item["category"] for item in products if item.get("category")))
            if len(cats) == 1:
                family_ids = family_ids_for(cats[0], mapping)
                records, total = _standards_for_families(
                    env_path, family_ids, category=cats[0], product_name=query, limit=limit,
                )
                return _lookup_envelope(
                    mode="product_candidates", query=query, products=products,
                    category=cats[0], familyIds=family_ids, total=total, standards=records,
                    note=f"匹配到 {len(products)} 个 SKU，分类同为「{cats[0]}」，下列为该类执行标准。请用 sku 确认具体商品。",
                )
            return _lookup_envelope(
                mode="product_candidates", query=query, products=products, mapped=False,
                note=f"匹配到 {len(products)} 个商品且分类不同，请用 sku 或更具体的名称再查。",
            )
        family_ids: list[str] = []
        for item in match_categories(query, mapping):
            family_ids.extend(family_ids_for(item, mapping))
        family_ids.extend(match_families(query, mapping))
        family_ids = list(dict.fromkeys(family_ids))
        if family_ids:
            records, total = _standards_for_families(
                env_path, family_ids, category=query, product_name=query, limit=limit,
            )
            return _lookup_envelope(
                mode="category", query=query, category=query,
                familyIds=family_ids, total=total, standards=records,
            )
        rows = search_standards_by_name(env_path, query, limit=limit)
        return _lookup_envelope(
            mode="name_search", query=query, total=len(rows),
            standards=[serialize_gb_record(row) for row in rows],
            note="未命中商品表或分类映射，已按标准名称检索现行 / 即将实施条目。",
        )

    return _lookup_envelope(mode="empty", mapped=False, note="请提供 SKU、商品名称、分类、标准号或目录族。")


def _lookup_for_product(env_path: str, product: dict, mapping: dict, limit: int) -> dict:
    category = str(product.get("category") or "")
    family_ids = family_ids_for(category, mapping)
    saved = _latest_saved_standard(env_path, str(product.get("sku") or ""))
    if not family_ids:
        return _lookup_envelope(
            mode="sku", query=product.get("sku") or "", product=product,
            category=category, mapped=False, savedStandard=saved,
            note=f"商品分类「{category or '空'}」尚未映射到国标目录族。",
        )
    records, total = _standards_for_families(
        env_path, family_ids,
        product_name=str(product.get("name") or ""), category=category, limit=limit,
    )
    return _lookup_envelope(
        mode="sku", query=product.get("sku") or "", product=product,
        category=category, familyIds=family_ids, total=total, standards=records,
        savedStandard=saved,
    )


def fetch_product_categories(env_path: str) -> list[dict]:
    """商品表里实际出现过的分类及 SKU 数。"""
    sql = (
        "SELECT category, COUNT(*) AS sku_count "
        "FROM `realtime_products` GROUP BY category ORDER BY sku_count DESC, category"
    )
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
    return [
        {"category": str(row.get("category") or ""), "sku_count": int(row.get("sku_count") or 0)}
        for row in rows
    ]


def _family_queries(family_id: str, family: dict, *, status: str) -> list[dict]:
    label = family.get("label") or family_id
    queries = []
    for code in family.get("ccs") or []:
        if str(code).strip():
            queries.append({
                "label": f"CCS {code} · {label}",
                "ccs": str(code).strip(), "ics": "", "keyword": "",
                "status": status, "family_ids": [family_id],
            })
    for code in family.get("ics") or []:
        if str(code).strip():
            queries.append({
                "label": f"ICS {code} · {label}",
                "ccs": "", "ics": str(code).strip(), "keyword": "",
                "status": status, "family_ids": [family_id],
            })
    for word in family.get("keywords") or []:
        if str(word).strip():
            queries.append({
                "label": f"名称 {word} · {label}",
                "ccs": "", "ics": "", "keyword": str(word).strip(),
                "status": status, "family_ids": [family_id],
            })
    return queries


def _merge_queries(queries: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for query in queries:
        key = (
            query.get("ccs") or "", query.get("ics") or "",
            query.get("keyword") or "", query.get("status") or "",
        )
        if key not in merged:
            merged[key] = dict(query)
            merged[key]["family_ids"] = list(query.get("family_ids") or [])
            order.append(key)
            continue
        seen = set(merged[key]["family_ids"])
        for family_id in query.get("family_ids") or []:
            if family_id not in seen:
                merged[key]["family_ids"].append(family_id)
                seen.add(family_id)
    return [merged[key] for key in order]


def resolve_product_scope(
    categories: list[dict] | list[str],
    mapping: dict,
    *,
    status: str = "",
) -> dict:
    """根据商品表分类展开要同步的国标检索条件。"""
    ignore = {str(item) for item in (mapping.get("ignore") or [])}
    families = mapping.get("families") or {}
    matched: list[dict] = []
    ignored: list[str] = []
    unmapped: list[str] = []
    used_families: dict[str, list[str]] = {}
    for item in categories:
        if isinstance(item, dict):
            category = str(item.get("category") or "")
            sku_count = int(item.get("sku_count") or 0)
        else:
            category, sku_count = str(item), 0
        if category in ignore:
            ignored.append(category)
            continue
        family_ids = family_ids_for(category, mapping)
        if not family_ids:
            unmapped.append(category)
            continue
        unknown = [fid for fid in family_ids if fid not in families]
        if unknown:
            raise GbSyncError(
                f"商品分类「{category}」映射到未定义的国标目录族：" + "、".join(unknown)
            )
        matched.append({"category": category, "sku_count": sku_count, "families": family_ids})
        for family_id in family_ids:
            used_families.setdefault(family_id, [])
            if category not in used_families[family_id]:
                used_families[family_id].append(category)
    queries: list[dict] = []
    for family_id in used_families:
        queries.extend(_family_queries(family_id, families[family_id], status=status))
    queries = _merge_queries(queries)
    if not queries:
        raise GbSyncError(
            "商品表没有可映射到国标目录的分类。请维护 config/gb_category_map.json，"
            "或用 --scope all 拉全量目录。"
        )
    return {
        "queries": queries,
        "matched": matched,
        "ignored": ignored,
        "unmapped": unmapped,
        "families": used_families,
    }


def build_queries(
    *,
    scope: str = "filtered",
    ccs: str | list[str] = "",
    ics: str | list[str] = "",
    keywords: str | list[str] = "",
    status: str = "",
) -> list[dict]:
    """把配置展开成一次同步要跑的检索条件。

    `all`：整份国家标准目录（约 8 万条，请求多、耗时长）。
    `filtered`：按 CCS / ICS / 中文名关键字的并集，适合合同选国标。
    """
    scope = str(scope or "filtered").strip().lower()
    status = _plain(status, limit=16)
    ccs_list = ccs if isinstance(ccs, list) else parse_csv_list(ccs)
    ics_list = ics if isinstance(ics, list) else parse_csv_list(ics)
    keyword_list = keywords if isinstance(keywords, list) else parse_csv_list(keywords)
    queries: list[dict] = []
    if scope in ("all", "catalog", "full"):
        queries.append({"label": "全部国家标准", "ccs": "", "ics": "", "keyword": "", "status": status})
        return queries
    for code in ccs_list:
        queries.append({"label": f"CCS {code}", "ccs": code, "ics": "", "keyword": "", "status": status})
    for code in ics_list:
        queries.append({"label": f"ICS {code}", "ccs": "", "ics": code, "keyword": "", "status": status})
    for word in keyword_list:
        queries.append({"label": f"名称 {word}", "ccs": "", "ics": "", "keyword": word, "status": status})
    if not queries:
        raise GbSyncError(
            "未配置国标同步范围：请设置 GB_SYNC_CCS / GB_SYNC_ICS / GB_SYNC_KEYWORDS，"
            "或把 GB_SYNC_SCOPE=all 拉全量国家标准目录。"
        )
    return queries


def parse_search_response(data: Any) -> tuple[int, int, list[dict]]:
    """解析高级检索 JSON：total、当前页、rows。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise GbSyncError("国标目录接口返回了无效 JSON") from exc
    if not isinstance(data, dict):
        raise GbSyncError("国标目录接口返回了非对象 JSON")
    rows = data.get("rows") or []
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise GbSyncError("国标目录接口 rows 不是数组")
    records = [row for row in rows if isinstance(row, dict)]
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError):
        total = len(records)
    try:
        page_number = int(data.get("pageNumber") or 1)
    except (TypeError, ValueError):
        page_number = 1
    return max(0, total), max(1, page_number), records


def compact_payload(row: dict) -> dict:
    """只保留目录字段，去掉起草人等个人姓名。"""
    payload = {}
    for key in PAYLOAD_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            payload[key] = value
    return payload


def content_hash_for(fields: dict) -> str:
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_standard(row: dict, synced_at: str) -> dict | None:
    """把 SAMR 一行目录转成表字段；缺少主键或标准号则丢弃。"""
    samr_id = _row_value(row, "id", limit=64)
    standard_no = _row_value(row, "C_STD_CODE", "STD_CODE3", "STD_CODE2", "STD_CODE", limit=128)
    if not samr_id or not standard_no:
        return None
    name_cn = _row_value(row, "C_C_NAME", "C_NAME")
    name_en = _row_value(row, "C_E_NAME", "E_NAME")
    status = _row_value(row, "STATE", "STATE2", "G_STATE", limit=32)
    nature = _row_value(row, "STD_NATURE", "G_STD_NATURE", limit=32)
    std_type = _row_value(row, "STD_TYPE", limit=32)
    ics = _row_value(row, "ICS", limit=128)
    ics_name = _row_value(row, "ICS_NAME1_FULL", "G_ICS_NAME")
    ccs = _row_value(row, "CCS", limit=128)
    issue_date = _day(_row_value(row, "ISSUE_DATE", "CIRCULATION_DATE", limit=32))
    implement_date = _day(_row_value(row, "ACT_DATE", limit=32))
    replaced = _row_value(row, "TOTAL_REPE")
    if replaced == ",":
        replaced = ""
    open_hash = _row_value(row, "OPEN_HASH_CODE", limit=64)
    hash_fields = {
        "standard_no": standard_no,
        "name_cn": name_cn,
        "name_en": name_en,
        "status": status,
        "nature": nature,
        "std_type": std_type,
        "ics": ics,
        "ccs": ccs,
        "issue_date": issue_date or "",
        "implement_date": implement_date or "",
        "replaced_standards": replaced,
        "open_hash_code": open_hash,
    }
    return {
        "samr_id": samr_id,
        "standard_no": standard_no,
        "standard_no_compact": _row_value(row, "STD_CODE2", "STD_CODE4", limit=128),
        "name_cn": name_cn,
        "name_en": name_en,
        "status": status,
        "nature": nature,
        "std_type": std_type,
        "ics": ics,
        "ics_name": ics_name,
        "ccs": ccs,
        "ccs_norm": _class_code(ccs),
        "issue_date": issue_date,
        "implement_date": implement_date,
        "issue_ann_no": _row_value(row, "ISSUE_ANN_NO", limit=64),
        "replaced_standards": replaced,
        "committee_code": _row_value(row, "TA_CODE", "TM_CODE", limit=64),
        "committee_name": _row_value(row, "TA_NAME", "TM_NAME", limit=255),
        "plan_code": _row_value(row, "C_PLAN_CODE", "PLAN_CODE", limit=64),
        "open_hash_code": open_hash,
        "content_hash": content_hash_for(hash_fields),
        "detail_url": f"{SAMR_ORIGIN}{DETAIL_PATH}?id={urllib.parse.quote(samr_id)}",
        "source_payload": compact_payload(row),
        "first_seen_at": synced_at,
        "last_seen_at": synced_at,
        "last_changed_at": synced_at,
        "api_synced_at": synced_at,
    }


def classify_changes(existing_hashes: dict[str, str], records: list[dict]) -> dict:
    """按 content_hash 判断新增 / 内容更新 / 无变化。"""
    inserted = updated = unchanged = 0
    for record in records:
        previous = existing_hashes.get(record["samr_id"])
        if previous is None:
            inserted += 1
        elif previous == record["content_hash"]:
            unchanged += 1
        else:
            updated += 1
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


def search_params(query: dict, *, page: int, page_size: int) -> dict[str, str]:
    params = {
        "typeRadio": "std-param",
        "tid": "2",
        "std_p4": "",
        "std_p6_1": query.get("status") or "",
        "std_p8": query.get("keyword") or "",
        "std_p14": query.get("ics") or "",
        "std_p15": query.get("ccs") or "",
        "pageNumber": str(max(1, int(page))),
        "pageSize": str(max(1, min(int(page_size), 100))),
    }
    return params


class SamrCatalogClient:
    """全国标准信息公共服务平台高级检索客户端。"""

    def __init__(
        self,
        *,
        timeout: float = 30,
        page_size: int = 50,
        request_interval: float = 1.2,
        fetch_json: Callable[[str], Any] | None = None,
    ):
        self.timeout = max(5, float(timeout))
        self.page_size = max(1, min(int(page_size), 100))
        self.request_interval = max(0.0, float(request_interval))
        self._fetch_json = fetch_json or self._http_get_json
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        if self.request_interval <= 0:
            return
        wait = self.request_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _http_get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SEARCH_REFERER,
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=SSL_CONTEXT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise GbSyncError(f"国标目录接口 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise GbSyncError(f"国标目录接口连接失败：{exc.reason}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GbSyncError("国标目录接口返回了无效 JSON") from exc

    def fetch_page(self, query: dict, page: int) -> tuple[int, int, list[dict]]:
        self._throttle()
        params = search_params(query, page=page, page_size=self.page_size)
        url = SAMR_ORIGIN + SEARCH_PATH + "?" + urllib.parse.urlencode(params)
        self._last_request_at = time.monotonic()
        return parse_search_response(self._fetch_json(url))

    def iter_rows(self, query: dict, *, max_pages: int = 0):
        """分页产出目录行。max_pages=0 表示按 total 拉完。"""
        page = 1
        total = None
        while True:
            current_total, _, rows = self.fetch_page(query, page)
            if total is None:
                total = current_total
            yield from rows
            if not rows:
                break
            fetched = page * self.page_size
            if fetched >= total or len(rows) < self.page_size:
                break
            page += 1
            if max_pages and page > max_pages:
                break


def ensure_schema(env_path: str) -> None:
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                for sql in SCHEMA_SQL:
                    cursor.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _row_tuple(record: dict) -> tuple:
    return tuple(
        _json(record[column]) if column == "source_payload" else record[column]
        for column in COLUMNS
    )


def _existing_hashes(cursor, samr_ids: list[str]) -> dict[str, str]:
    if not samr_ids:
        return {}
    marks = ",".join(["%s"] * len(samr_ids))
    cursor.execute(
        f"SELECT samr_id, content_hash FROM `{GB_STANDARDS_TABLE}` WHERE samr_id IN ({marks})",
        samr_ids,
    )
    return {str(row["samr_id"]): str(row["content_hash"] or "") for row in cursor.fetchall()}


def upsert_standards(env_path: str, records: list[dict]) -> dict:
    """幂等写入规范化后的目录行，返回新增/更新/未变计数。"""
    if not records:
        return {"written": 0, "inserted": 0, "updated": 0, "unchanged": 0}
    unique: dict[str, dict] = {}
    for record in records:
        unique[record["samr_id"]] = record
    rows = list(unique.values())
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                existing = _existing_hashes(cursor, [row["samr_id"] for row in rows])
                stats = classify_changes(existing, rows)
                cursor.executemany(UPSERT_SQL, [_row_tuple(row) for row in rows])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    stats["written"] = len(rows)
    return stats


def _state_update(env_path: str, **values: Any) -> None:
    allowed = {
        "status", "watermark_modified", "last_started_at", "last_success_at",
        "last_request_id", "rows_synced", "error_message",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    columns = ["source_name", *values]
    row = [SYNC_SOURCE_NAME, *values.values()]
    update = ",".join(f"`{key}`=VALUES(`{key}`)" for key in values)
    sql = (
        f"INSERT INTO `{SYNC_STATE_TABLE}` ({','.join(f'`{key}`' for key in columns)}) "
        f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE {update}"
    )
    with connect(env_path) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, row)
        conn.commit()


def read_sync_state(env_path: str) -> dict:
    sql = f"SELECT * FROM `{SYNC_STATE_TABLE}` WHERE source_name=%s"
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (SYNC_SOURCE_NAME,))
            return cursor.fetchone() or {}


def replace_family_links(env_path: str, family_seen: dict[str, set[str]], synced_at: str) -> None:
    """按本轮实际拉到的标准重写目录族关联，方便合同按商品类筛选。"""
    with connect(env_path) as conn:
        try:
            with conn.cursor() as cursor:
                for family_id, samr_ids in family_seen.items():
                    cursor.execute(
                        f"DELETE FROM `{GB_FAMILY_TABLE}` WHERE family_id=%s",
                        (family_id,),
                    )
                    if not samr_ids:
                        continue
                    cursor.executemany(
                        f"INSERT INTO `{GB_FAMILY_TABLE}` (samr_id, family_id, api_synced_at) "
                        "VALUES (%s, %s, %s)",
                        [(samr_id, family_id, synced_at) for samr_id in sorted(samr_ids)],
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _synced_at() -> str:
    return business_now().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


class GbStandardsSyncer:
    """按查询条件拉目录、规范化、幂等写入镜像库。"""

    def __init__(
        self,
        env_path: str,
        client: SamrCatalogClient,
        *,
        queries: list[dict] | None = None,
        query_loader: Callable[[], dict] | None = None,
        max_pages: int = 0,
        upsert_batch: int = 50,
    ):
        self.env_path = env_path
        self.client = client
        self.queries = list(queries or [])
        self.query_loader = query_loader
        self.max_pages = max(0, int(max_pages))
        self.upsert_batch = max(1, int(upsert_batch))
        self._lock = threading.Lock()
        self._schema_ready = False
        self.last_scope: dict = {}

    def resolve_queries(self) -> list[dict]:
        if self.query_loader is None:
            if not self.queries:
                raise GbSyncError("没有可执行的国标检索条件")
            self.last_scope = {"queries": self.queries, "unmapped": [], "ignored": [], "matched": []}
            return self.queries
        scope = self.query_loader()
        queries = list(scope.get("queries") or [])
        if not queries:
            raise GbSyncError("没有可执行的国标检索条件")
        self.last_scope = scope
        self.queries = queries
        return queries

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        for attempt in range(3):
            try:
                ensure_schema(self.env_path)
                self._schema_ready = True
                return
            except Exception as exc:
                if attempt >= 2 or not is_transient_mysql_error(exc):
                    raise
                time.sleep(0.25 * (attempt + 1))

    def sync(self) -> dict:
        with self._lock:
            return self._sync_locked()

    def _sync_locked(self) -> dict:
        queries = self.resolve_queries()
        self._ensure_schema()
        started = _synced_at()
        _state_update(self.env_path, status="running", last_started_at=started, error_message="")
        totals = {"inserted": 0, "updated": 0, "unchanged": 0, "written": 0, "skipped": 0, "fetched": 0}
        seen: set[str] = set()
        family_seen: dict[str, set[str]] = {}
        query_summaries = []
        try:
            for query in queries:
                fetched = 0
                skipped = 0
                batch: list[dict] = []
                synced_at = _synced_at()
                family_ids = [str(item) for item in (query.get("family_ids") or []) if item]

                def flush() -> None:
                    if not batch:
                        return
                    stats = upsert_standards(self.env_path, batch)
                    for key in ("inserted", "updated", "unchanged", "written"):
                        totals[key] += stats[key]
                    batch.clear()

                for raw in self.client.iter_rows(query, max_pages=self.max_pages):
                    fetched += 1
                    record = normalize_standard(raw, synced_at)
                    if record is None:
                        skipped += 1
                        continue
                    samr_id = record["samr_id"]
                    for family_id in family_ids:
                        family_seen.setdefault(family_id, set()).add(samr_id)
                    if samr_id in seen:
                        continue
                    seen.add(samr_id)
                    batch.append(record)
                    if len(batch) >= self.upsert_batch:
                        flush()
                flush()
                totals["fetched"] += fetched
                totals["skipped"] += skipped
                query_summaries.append({
                    "label": query.get("label") or "",
                    "fetched": fetched,
                    "skipped": skipped,
                    "families": family_ids,
                })
            replace_family_links(self.env_path, family_seen, _synced_at())
            changed = totals["inserted"] + totals["updated"]
            _state_update(
                self.env_path,
                status="success",
                last_success_at=_synced_at(),
                rows_synced=len(seen),
                last_request_id=";".join(item["label"] for item in query_summaries)[:128],
                error_message="",
            )
            return {
                **totals,
                "unique": len(seen),
                "queries": query_summaries,
                "changed": changed,
                "unmapped": list(self.last_scope.get("unmapped") or []),
                "families": {
                    family_id: len(ids) for family_id, ids in family_seen.items()
                },
            }
        except Exception as exc:
            _state_update(
                self.env_path,
                status="error",
                error_message=f"{type(exc).__name__}: {exc}"[:1000],
            )
            raise


class GbStandardsScheduler:
    """每天在指定时刻跑一轮国标目录同步；默认关闭。"""

    def __init__(
        self,
        syncer: GbStandardsSyncer | None,
        *,
        enabled: bool,
        send_time: str = "02:30",
        poll_seconds: int = 30,
        initial_delay_seconds: int = 60,
    ):
        self.syncer = syncer
        self.enabled = bool(enabled and syncer)
        self.send_time = self._parse_time(send_time)
        self.poll_seconds = max(5, int(poll_seconds))
        self.initial_delay = max(0, int(initial_delay_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run = ""
        self.last_error = ""
        self.last_result: dict = {}

    @staticmethod
    def _parse_time(value) -> tuple[int, int]:
        try:
            hour, minute = str(value or "02:30").split(":", 1)
            return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
        except (TypeError, ValueError):
            return 2, 30

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "sendTime": f"{self.send_time[0]:02d}:{self.send_time[1]:02d}",
            "lastRun": self.last_run,
            "lastError": self.last_error,
            "lastResult": dict(self.last_result) if self.last_result else {},
        }

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gb-standards-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _already_succeeded_today(self) -> bool:
        today = business_today().isoformat()
        if self.last_run == today:
            return True
        try:
            state = read_sync_state(self.syncer.env_path)
        except Exception:
            return False
        if str(state.get("status") or "") != "success":
            return False
        last = state.get("last_success_at")
        if hasattr(last, "strftime"):
            stamp = last.strftime("%Y-%m-%d")
        else:
            stamp = str(last or "")[:10]
        return stamp == today

    def _loop(self) -> None:
        if self.initial_delay and self._stop.wait(self.initial_delay):
            return
        while not self._stop.wait(self.poll_seconds):
            if self._already_succeeded_today():
                continue
            current = business_now()
            if (current.hour, current.minute) < self.send_time:
                continue
            try:
                self.last_result = self.syncer.sync()
                self.last_run = business_today().isoformat()
                self.last_error = ""
                print(
                    "国标目录同步完成："
                    f"新增 {self.last_result.get('inserted', 0)}，"
                    f"更新 {self.last_result.get('updated', 0)}，"
                    f"未变 {self.last_result.get('unchanged', 0)}"
                )
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                print(f"国标目录同步失败：{self.last_error}")


def build_gb_sync_from_settings(
    setting: Callable[[str, str], str], *, env_path: str,
) -> tuple[GbStandardsSyncer | None, GbStandardsScheduler]:
    enabled = str(setting("GB_SYNC_ENABLED", "false")).strip().lower() in ("1", "true", "yes", "on")
    scope = setting("GB_SYNC_SCOPE", "products")
    status = setting("GB_SYNC_STATUS", "")
    map_setting = str(setting("GB_SYNC_CATEGORY_MAP", "") or "").strip()
    map_path = Path(map_setting) if map_setting else DEFAULT_CATEGORY_MAP
    if not map_path.is_absolute():
        map_path = ROOT / map_path
    client = SamrCatalogClient(
        timeout=float(setting("GB_SYNC_TIMEOUT_SECONDS", "30") or 30),
        page_size=int(setting("GB_SYNC_PAGE_SIZE", "50") or 50),
        request_interval=float(setting("GB_SYNC_REQUEST_INTERVAL", "1.2") or 1.2),
    )
    max_pages = int(setting("GB_SYNC_MAX_PAGES", "0") or 0)
    if str(scope or "products").strip().lower() in ("all", "catalog", "full", "filtered"):
        try:
            queries = build_queries(
                scope=scope,
                ccs=setting("GB_SYNC_CCS", ""),
                ics=setting("GB_SYNC_ICS", ""),
                keywords=setting("GB_SYNC_KEYWORDS", ""),
                status=status,
            )
        except GbSyncError:
            queries = []
        syncer = GbStandardsSyncer(
            env_path, client, queries=queries, max_pages=max_pages,
        ) if queries else None
    else:
        def query_loader():
            mapping = load_category_map(map_path)
            categories = fetch_product_categories(env_path)
            return resolve_product_scope(categories, mapping, status=status)

        syncer = GbStandardsSyncer(
            env_path, client, query_loader=query_loader, max_pages=max_pages,
        )
    scheduler = GbStandardsScheduler(
        syncer, enabled=enabled and bool(syncer),
        send_time=setting("GB_SYNC_TIME", "02:30"),
        initial_delay_seconds=int(setting("GB_SYNC_INITIAL_DELAY_SECONDS", "60") or 60),
    )
    return syncer, scheduler
