# -*- coding: utf-8 -*-
"""鞋垫换货：镜像定位 + 鞋码映射 + 串行写入。

查询池默认含抖音 / 快手 / 视频号。尺码来自同单鞋子规格。
半码按码数舍去小数（40.5→40）再换算毫米。
默认只处理 Question / WaitConfirm。Delivering / 发货中只列出，不写 ERP。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import sqlite3
import tempfile
import threading
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .policy import load_policy
from .service import ExchangeError
from ..business_time import BUSINESS_TIMEZONE, business_now
from ..staff_names import buyer_names_equivalent
from ..order_source import OrderSourceError, _identifier, _mirror_state, source_status
from ..database import connect
from ..paths import DATA_DIR, ROOT, resolve_repo_path

logger = logging.getLogger(__name__)


SOURCE_SKU = "XZ25401308-101"
SHOP_POOL = ("抖音", "快手", "视频号")
DEFAULT_SHOP = ""
INSOLE_WRITE_DELAY_MS = 50
INSOLE_WRITE_CONCURRENCY = 3
INSOLE_READ_CONCURRENCY = 5
INSOLE_BY_MM = {
    "225": "XZ25401308-09901",
    "230": "XZ25401308-09902",
    "235": "XZ25401308-09903",
    "240": "XZ25401308-09904",
    "245": "XZ25401308-09905",
    "250": "XZ25401308-09906",
    "255": "XZ25401308-09907",
    "260": "XZ25401308-09908",
    "265": "XZ25401308-09909",
    "270": "XZ25401308-09910",
    "275": "XZ25401308-09911",
    "280": "XZ25401308-09912",
    "285": "XZ25401308099BL01",
    "290": "XZ25401308099BL02",
}
PROCESSABLE_STATUSES = frozenset({
    "Question", "WaitConfirm", "异常", "待审核",
})
PARKED_STATUSES = frozenset({
    "Delivering", "发货中",
})
FORBIDDEN_STATUSES = frozenset({
    "Cancelled", "Delete", "Merged", "Sent", "取消", "退款", "关闭", "已发货",
})
MM_IN_PARENS = re.compile(r"(\d{3}(?:\.\d+)?)\s*\)")
MM_ANY = re.compile(r"(\d{3}(?:\.\d+)?)")
EU_LABEL = re.compile(r"鞋码大小[:：]\s*(\d{2}(?:\.\d+)?)")
EU_BEFORE_MM = re.compile(r"(\d{2}(?:\.\d+)?)\s*\(\s*\d{3}")
# 快手 / 视频号：网眼款;42 或 布面款;39 (245)
LOOSE_EU = re.compile(r"[;；]\s*(\d{2}(?:\.\d+)?)\s*(?:\(|$)")
TARGET_PREFIXES = ("XZ25401308-099", "XZ25401308099")
INSOLE_WRITTEN_PATH = DATA_DIR / "insole_written.json"
WRITTEN_TTL_HOURS = 48
WRITTEN_REASON = "本批已写入，镜像尚未跟上"
DONE_STATUS = "已处理"
DONE_LIMIT = 20
INSOLE_LINES_TTL = 60
INSOLE_LINES_STALE = 300
INSOLE_OID_CHUNK = 400
_lines_cache: dict = {}
_lines_lock = threading.Lock()
_lines_rebuilding: set[str] = set()
RESERVED_REASON = "他人待确认或正在写入"
RESERVED_STATUSES = frozenset({"pending", "confirmed"})
_WRITE_LOCK = threading.Lock()


def _truncate_decimal(value: str) -> str:
    text = str(value or "").strip()
    if "." in text:
        return text.split(".", 1)[0]
    return text


def mm_from_props(text: str) -> str:
    """从「鞋码大小:41(255)」取出括号内毫米数原文。"""
    match = MM_IN_PARENS.search(text or "")
    if not match:
        match = MM_ANY.search(text or "")
    if not match:
        return ""
    value = match.group(1)
    if value.endswith(".0"):
        value = value[:-2]
    return value


def eu_size_from_props(text: str) -> str:
    match = EU_LABEL.search(text or "")
    if match:
        return match.group(1)
    match = EU_BEFORE_MM.search(text or "")
    if match:
        return match.group(1)
    match = LOOSE_EU.search(text or "")
    return match.group(1) if match else ""


def shop_keys(shop) -> tuple[str, ...]:
    """空值表示整池（抖音/快手/视频号），不是放开所有店铺。"""
    if isinstance(shop, (list, tuple, set)):
        keys = tuple(str(item).strip() for item in shop if str(item).strip())
        return keys or SHOP_POOL
    text = str(shop or "").strip()
    if not text or text in ("全部", "all", "*"):
        return SHOP_POOL
    parts = tuple(item.strip() for item in re.split(r"[,，/、]", text) if item.strip())
    return parts or SHOP_POOL


def shop_matches(shop_name: str, shop) -> bool:
    name = str(shop_name or "")
    return any(key in name for key in shop_keys(shop))


def mm_from_eu_size(size: str) -> str:
    """码 = 毫米 ÷ 5 − 10，所以毫米 = (码 + 10) × 5。半码先舍去小数。"""
    try:
        number = int(_truncate_decimal(size))
    except (TypeError, ValueError):
        return ""
    if 35 <= number <= 50:
        return str((number + 10) * 5)
    return ""


def resolve_insole_size(props: str = "", mm: str = "") -> dict:
    """半码按码数舍去小数；只有毫米小数时再舍去后落到 5mm 档。"""
    eu = eu_size_from_props(props)
    raw_mm = str(mm or "").strip() or mm_from_props(props)
    resolved = ""
    note = ""
    if eu:
        resolved = mm_from_eu_size(eu)
        if resolved and "." in eu:
            note = f"半码 {eu} 舍去小数按 {_truncate_decimal(eu)} 码→{resolved}mm"
    if not resolved and raw_mm:
        if "." in raw_mm:
            try:
                whole = int(_truncate_decimal(raw_mm))
            except ValueError:
                whole = 0
            stepped = whole - (whole % 5) if whole else 0
            resolved = str(stepped) if stepped else ""
            if resolved:
                note = f"毫米 {raw_mm} 舍去小数按 {resolved}mm"
        else:
            resolved = raw_mm
    return {
        "eu_size": eu,
        "raw_mm": raw_mm,
        "shoe_mm": resolved,
        "target_sku": target_sku_for_mm(resolved),
        "size_note": note,
    }


def target_sku_for_mm(mm: str) -> str:
    return INSOLE_BY_MM.get(str(mm or "").strip(), "")


def _parse_written_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if "T" in text:
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BUSINESS_TIMEZONE)
        return parsed
    except ValueError:
        return None


def _written_path(path=None, *, root=None) -> Path:
    if path:
        return Path(path)
    if root is not None and Path(root).resolve() != ROOT.resolve():
        return Path(root) / "insole_written.json"
    return INSOLE_WRITTEN_PATH


def _fresh_writes(payload: dict) -> dict:
    cutoff = business_now() - timedelta(hours=WRITTEN_TTL_HOURS)
    kept = {}
    for key, raw in (payload or {}).items():
        oid = str(key or "").strip()
        if not oid or not isinstance(raw, dict):
            continue
        stamp = _parse_written_at(raw.get("at") or "")
        if stamp is not None and stamp < cutoff:
            continue
        kept[oid] = {
            "target_sku": str(raw.get("target_sku") or ""),
            "at": str(raw.get("at") or ""),
        }
    return kept


def load_insole_writes(path=None, *, root=None) -> dict:
    """本机已写入台账。文件损坏就当没有，不挡定位。"""
    resolved = _written_path(path, root=root)
    if not resolved.is_file():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _fresh_writes(payload if isinstance(payload, dict) else {})


def remember_insole_writes(writes: list[dict], *, path=None, root=None) -> dict:
    """记下刚写成功的内部单号，定位时先排除，不等镜像增量。"""
    resolved = _written_path(path, root=root)
    stamp = business_now().isoformat()
    incoming = {}
    for item in writes or []:
        oid = str(item.get("o_id") or item.get("oId") or "").strip()
        target = str(item.get("target_sku") or item.get("targetSku") or "").strip()
        if oid:
            incoming[oid] = {"target_sku": target, "at": stamp}
    if not incoming:
        return {}
    with _WRITE_LOCK:
        history = load_insole_writes(resolved, root=root)
        history.update(incoming)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(prefix=".insole-written-", dir=str(resolved.parent))
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(history, file, ensure_ascii=False, indent=2, sort_keys=True)
                    file.write("\n")
                os.replace(temp_name, resolved)
            except BaseException:
                Path(temp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("鞋垫已写入台账写失败：%s", exc)
            return incoming
    return incoming


def load_executed_insole_writes(db_path, *, hours: int = WRITTEN_TTL_HOURS) -> dict:
    """从已执行的 pending_actions 回收成功单号，避免重启后台账丢了。"""
    path = Path(db_path)
    if not path.is_file():
        return {}
    cutoff = business_now() - timedelta(hours=max(1, int(hours)))
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT preview_json, result_json, executed_at
            FROM pending_actions
            WHERE tool = 'process_insole_orders' AND status = 'executed'
            """
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("读取鞋垫已执行动作失败：%s", exc)
        return {}
    writes = {}
    for row in rows:
        stamp = _parse_written_at(row["executed_at"] or "")
        if stamp is not None and stamp < cutoff:
            continue
        try:
            preview = json.loads(row["preview_json"] or "{}")
            result = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            continue
        targets = {
            str(item.get("oId") or item.get("o_id") or ""): str(item.get("targetSku") or item.get("target_sku") or "")
            for item in (preview.get("orders") or [])
        }
        ok_ids = set()
        for item in result.get("log") or []:
            if str(item.get("result") or "") == "ok":
                ok_ids.add(str(item.get("oId") or item.get("o_id") or ""))
        for item in result.get("succeeded") or []:
            ok_ids.add(str(item.get("o_id") or item.get("oId") or ""))
        at = str(row["executed_at"] or "")
        for oid in ok_ids:
            if oid:
                writes[oid] = {"target_sku": targets.get(oid) or "", "at": at}
    return writes


def load_written_insole_orders(setting: Callable[[str, str], str] | None = None,
                               root=None, path=None) -> dict:
    """JSON 台账 + 已执行动作，定位时用来丢掉刚写过的单。"""
    writes = load_insole_writes(path, root=root)
    if setting is None:
        return writes
    db = resolve_repo_path(
        setting("AGENT_DATABASE_PATH", "files/data/agent.sqlite3"),
        root=Path(root) if root is not None else ROOT,
    )
    writes.update(load_executed_insole_writes(db))
    return writes


def _oids_from_action(preview: dict, arguments: dict) -> list[str]:
    oids = []
    seen = set()
    for item in (preview.get("orders") or []):
        if not isinstance(item, dict):
            continue
        oid = str(item.get("oId") or item.get("o_id") or "").strip()
        if oid and oid not in seen:
            seen.add(oid)
            oids.append(oid)
    for raw in list(preview.get("oIds") or []) + list(arguments.get("o_ids") or []):
        oid = str(raw or "").strip()
        if oid and oid not in seen:
            seen.add(oid)
            oids.append(oid)
    return oids


def load_reserved_insole_orders(
    db_path, *, exclude_action_id: str | None = None, viewer: str | None = None,
) -> dict:
    """pending / confirmed 动作里已经占住的内部单号，定位时不要再发给别人。

    ``viewer`` 是当前查询的人：本人待确认仍给自己看，只对其他人隐藏。
    """
    path = Path(db_path)
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(RESERVED_STATUSES))
        rows = conn.execute(
            f"""
            SELECT id, operator, status, preview_json, arguments_json
            FROM pending_actions
            WHERE tool = 'process_insole_orders' AND status IN ({placeholders})
            """,
            tuple(RESERVED_STATUSES),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("读取鞋垫占用动作失败：%s", exc)
        return {}
    reserved = {}
    skip = str(exclude_action_id or "")
    viewer_name = str(viewer or "").strip()
    for row in rows:
        action_id = str(row["id"] or "")
        if skip and action_id == skip:
            continue
        who = str(row["operator"] or "")
        if viewer_name and who and buyer_names_equivalent(
            viewer_name, who, include_nick=True,
        ):
            continue
        try:
            preview = json.loads(row["preview_json"] or "{}")
            arguments = json.loads(row["arguments_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(preview, dict):
            preview = {}
        if not isinstance(arguments, dict):
            arguments = {}
        info = {
            "action_id": action_id,
            "operator": who,
            "status": str(row["status"] or ""),
        }
        for oid in _oids_from_action(preview, arguments):
            reserved.setdefault(oid, info)
    return reserved


def sync_insole_mirror(env_path: str, writes: list[dict], *, mirror=None,
                       refresh: bool = False) -> dict:
    """写入成功后回写镜像明细。按单拉代理默认关掉，避免钉钉确认后再等一轮接口。"""
    oids = [str(item.get("o_id") or "") for item in writes if item.get("o_id")]
    refreshed: dict = {}
    if refresh and mirror is not None and oids:
        try:
            refreshed = mirror.refresh_orders(oids)
        except Exception as exc:
            logger.warning("鞋垫写入后按单刷新镜像失败：%s", exc)
            refreshed = {"ok": False, "error": str(exc)}
    applied = []
    if env_path and writes and Path(env_path).is_file():
        try:
            from ..realtime_mirror import replace_order_item_skus
            applied = replace_order_item_skus(
                env_path,
                [
                    {
                        "o_id": item.get("o_id"),
                        "source_sku": SOURCE_SKU,
                        "target_sku": item.get("target_sku"),
                    }
                    for item in writes
                ],
            )
        except Exception as exc:
            logger.warning("鞋垫写入后回写镜像明细失败：%s", exc)
    return {"refresh": refreshed, "applied": applied}


def special_mapping() -> dict:
    policy = load_policy()
    special = next(
        (item for item in policy["specialMappings"] if item["sourceSku"] == SOURCE_SKU),
        None,
    )
    if not special:
        raise ExchangeError(f"换货规则未维护源 SKU {SOURCE_SKU} 的鞋垫映射")
    return special


def _is_insole_target(sku: str) -> bool:
    text = str(sku or "")
    return any(text.startswith(prefix) or prefix in text for prefix in TARGET_PREFIXES)


def classify_insole_row(order: dict, *, shop: str = DEFAULT_SHOP, written: dict | None = None,
                        reserved: dict | None = None) -> dict:
    """给一张已聚合的订单打上 processable / parked 及原因。"""
    row = dict(order)
    status = str(row.get("status") or "")
    shop_name = str(row.get("shop") or "")
    sized = resolve_insole_size(row.get("shoe_props") or "", row.get("shoe_mm") or "")
    mm = sized["shoe_mm"] or str(row.get("shoe_mm") or "")
    target = str(row.get("target_sku") or "") or sized["target_sku"]
    row["shoe_mm"] = mm
    row["target_sku"] = target
    row["size_note"] = sized.get("size_note") or row.get("size_note") or ""
    row["source_sku"] = SOURCE_SKU
    if not shop_matches(shop_name, shop):
        row["bucket"] = "skipped"
        row["reason"] = f"非{'/'.join(shop_keys(shop))}店铺"
        return row
    if status in FORBIDDEN_STATUSES:
        row["bucket"] = "skipped"
        row["reason"] = f"状态 {status} 禁止换货"
        return row
    if not row.get("has_source"):
        row["bucket"] = "skipped"
        row["reason"] = "线上已没有源 SKU"
        return row
    if written and str(row.get("o_id") or "") in written:
        row["bucket"] = "skipped"
        row["reason"] = WRITTEN_REASON
        return row
    hold = (reserved or {}).get(str(row.get("o_id") or ""))
    if hold:
        who = str(hold.get("operator") or "").strip()
        row["bucket"] = "skipped"
        row["reason"] = f"{RESERVED_REASON}（{who}）" if who else RESERVED_REASON
        return row
    if status in PARKED_STATUSES:
        row["bucket"] = "parked"
        row["reason"] = "发货中本批不处理"
        return row
    if not target:
        row["bucket"] = "parked"
        row["reason"] = "同单鞋码舍去小数后仍没有对应鞋垫 SKU"
        return row
    if status not in PROCESSABLE_STATUSES:
        row["bucket"] = "parked"
        row["reason"] = f"状态 {status} 本批不处理"
        return row
    row["bucket"] = "processable"
    row["reason"] = ""
    return row


def _pick_shoe(items: list[dict]) -> dict:
    shoes = [
        item for item in items
        if item.get("sku") != SOURCE_SKU and not _is_insole_target(item.get("sku") or "")
    ]
    return shoes[0] if shoes else {}


def _aggregate_orders(lines: list[dict]) -> list[dict]:
    by_oid: dict[str, dict] = {}
    for raw in lines:
        oid = str(raw.get("o_id") or "")
        if not oid:
            continue
        order = by_oid.setdefault(oid, {
            "o_id": oid,
            "so_id": str(raw.get("so_id") or ""),
            "status": str(raw.get("status") or ""),
            "shop": str(raw.get("shop_name") or raw.get("shop") or ""),
            "order_date": str(raw.get("order_date") or "")[:19],
            "has_source": False,
            "items": [],
        })
        sku = str(raw.get("sku_id") or raw.get("sku") or "")
        item = {
            "sku": sku,
            "style": str(raw.get("i_id") or raw.get("style") or ""),
            "name": str(raw.get("name") or ""),
            "props": str(raw.get("properties_value") or raw.get("props") or ""),
            "qty": str(raw.get("qty") or ""),
        }
        order["items"].append(item)
        if sku == SOURCE_SKU:
            order["has_source"] = True
    rows = []
    for order in by_oid.values():
        shoe = _pick_shoe(order["items"])
        sized = resolve_insole_size(shoe.get("props") or "")
        rows.append({
            "o_id": order["o_id"],
            "so_id": order["so_id"],
            "status": order["status"],
            "shop": order["shop"],
            "order_date": order["order_date"],
            "has_source": order["has_source"],
            "shoe_sku": shoe.get("sku") or "",
            "shoe_props": shoe.get("props") or "",
            "shoe_mm": sized["shoe_mm"],
            "target_sku": sized["target_sku"],
            "size_note": sized["size_note"],
            "source_sku": SOURCE_SKU,
        })
    return rows


def _chunks(values: list[str], size: int = INSOLE_OID_CHUNK):
    step = max(1, int(size))
    for index in range(0, len(values), step):
        yield values[index:index + step]


def merge_insole_lines(headers: dict[str, dict], items: list[dict]) -> list[dict]:
    """单头 + 明细在内存拼行，避免镜像上 CAST JOIN 扫全表。"""
    lines = []
    for item in items:
        oid = str(item.get("o_id") or "").strip()
        header = headers.get(oid)
        if not header:
            continue
        lines.append({**header, **item, "o_id": oid})
    lines.sort(key=lambda row: (str(row.get("o_id") or ""), str(row.get("sku_id") or "")), reverse=True)
    return lines


def invalidate_insole_lines_cache() -> None:
    with _lines_lock:
        _lines_cache.clear()


def _lines_cache_key(env: str, shops) -> str:
    return f"{env}|{','.join(shop_keys(shops))}"


def _sync_payload(sync: dict) -> dict:
    return {
        "status": str(sync.get("status") or ""),
        "lastSuccessAt": str(sync.get("last_success_at") or ""),
        "errorMessage": str(sync.get("error_message") or ""),
    }


def fetch_insole_lines(setting: Callable[[str, str], str], env_path: str,
                       *, o_ids: list[str] | None = None, shops=None) -> tuple[list[dict], dict]:
    """从订单镜像拉出仍含源鞋垫 SKU 的整单明细。

    未指定单号时只拉查询池店铺、且状态仍可能处理的单。清单查询走 60 秒缓存，
    过期先回上一份再后台刷新，避免钉钉连着问两次都扫库。
    """
    wanted = [str(item).strip() for item in (o_ids or []) if str(item).strip()]
    env = str(setting("EXCHANGE_ORDER_DATABASE_ENV_FILE", "") or "").strip() or env_path
    cache_key = "" if wanted else _lines_cache_key(env, shops)
    if cache_key:
        now = time.monotonic()
        with _lines_lock:
            cached = _lines_cache.get(cache_key)
            if cached and cached["expires"] > now:
                return list(cached["lines"]), dict(cached["sync"])
            stale = bool(cached and cached.get("staleUntil", 0) > now)
        if stale:
            _schedule_lines_rebuild(setting, env_path, shops, cache_key)
            return list(cached["lines"]), dict(cached["sync"])
    lines, sync = _query_insole_lines(setting, env_path, o_ids=wanted, shops=shops)
    if cache_key:
        now = time.monotonic()
        with _lines_lock:
            _lines_cache[cache_key] = {
                "lines": list(lines),
                "sync": dict(sync),
                "expires": now + INSOLE_LINES_TTL,
                "staleUntil": now + INSOLE_LINES_STALE,
            }
    return lines, sync


def _schedule_lines_rebuild(setting, env_path, shops, cache_key: str) -> None:
    with _lines_lock:
        if cache_key in _lines_rebuilding:
            return
        _lines_rebuilding.add(cache_key)

    def worker():
        try:
            lines, sync = _query_insole_lines(setting, env_path, o_ids=[], shops=shops)
            now = time.monotonic()
            with _lines_lock:
                _lines_cache[cache_key] = {
                    "lines": list(lines),
                    "sync": dict(sync),
                    "expires": now + INSOLE_LINES_TTL,
                    "staleUntil": now + INSOLE_LINES_STALE,
                }
        except Exception:
            logger.exception("后台刷新鞋垫定位缓存失败")
        finally:
            with _lines_lock:
                _lines_rebuilding.discard(cache_key)

    threading.Thread(target=worker, name="insole-lines-cache", daemon=True).start()


def warm_insole_lines_cache(setting: Callable[[str, str], str], env_path: str,
                            *, shops=None) -> None:
    """服务起来后先扫一轮进缓存，避免员工第一条钉钉再等冷查询。"""
    def worker():
        try:
            fetch_insole_lines(setting, env_path, shops=shops)
            logger.info("鞋垫定位缓存已预热")
        except Exception:
            logger.warning("鞋垫定位缓存预热失败", exc_info=True)

    threading.Thread(target=worker, name="insole-cache-warm", daemon=True).start()


def _query_insole_lines(setting: Callable[[str, str], str], env_path: str,
                        *, o_ids: list[str], shops=None) -> tuple[list[dict], dict]:
    availability = source_status(setting)
    if not availability["configured"]:
        raise OrderSourceError(availability.get("message") or "订单镜像尚未配置")
    item_table = _identifier(
        setting("EXCHANGE_ORDER_ITEM_TABLE", "realtime_order_items"),
        "EXCHANGE_ORDER_ITEM_TABLE",
    )
    order_table = _identifier(
        setting("EXCHANGE_ORDER_TABLE", "realtime_orders"), "EXCHANGE_ORDER_TABLE",
    )
    env = str(setting("EXCHANGE_ORDER_DATABASE_ENV_FILE", "") or "").strip() or env_path
    blocked = _mirror_state(env, order_table)
    if blocked:
        raise OrderSourceError(blocked.get("message") or "订单镜像不可用")
    item_oid = _identifier(setting("EXCHANGE_ORDER_ITEM_ORDER_ID_COLUMN", "o_id"), "EXCHANGE_ORDER_ITEM_ORDER_ID_COLUMN")
    item_sku = _identifier(setting("EXCHANGE_ORDER_ITEM_SKU_COLUMN", "sku_id"), "EXCHANGE_ORDER_ITEM_SKU_COLUMN")
    item_style = _identifier(setting("EXCHANGE_ORDER_ITEM_STYLE_COLUMN", "i_id"), "EXCHANGE_ORDER_ITEM_STYLE_COLUMN", required=False)
    item_name = _identifier(setting("EXCHANGE_ORDER_ITEM_NAME_COLUMN", "name"), "EXCHANGE_ORDER_ITEM_NAME_COLUMN", required=False)
    item_props = _identifier(
        setting("EXCHANGE_ORDER_ITEM_PROPERTIES_COLUMN", "properties_value"),
        "EXCHANGE_ORDER_ITEM_PROPERTIES_COLUMN", required=False,
    )
    item_qty = _identifier(setting("EXCHANGE_ORDER_ITEM_QTY_COLUMN", "qty"), "EXCHANGE_ORDER_ITEM_QTY_COLUMN", required=False)
    oid_col = _identifier(setting("EXCHANGE_ORDER_ID_COLUMN", "o_id"), "EXCHANGE_ORDER_ID_COLUMN")
    so_col = _identifier(setting("EXCHANGE_ORDER_PLATFORM_ID_COLUMN", "so_id"), "EXCHANGE_ORDER_PLATFORM_ID_COLUMN", required=False)
    status_col = _identifier(setting("EXCHANGE_ORDER_STATUS_COLUMN", "status"), "EXCHANGE_ORDER_STATUS_COLUMN", required=False)
    shop_col = _identifier(setting("EXCHANGE_ORDER_SHOP_COLUMN", "shop_name"), "EXCHANGE_ORDER_SHOP_COLUMN", required=False)
    date_col = _identifier(setting("EXCHANGE_ORDER_DATE_COLUMN", "order_date"), "EXCHANGE_ORDER_DATE_COLUMN", required=False)
    style_sql = f"`{item_style}`" if item_style else "''"
    name_sql = f"`{item_name}`" if item_name else "''"
    props_sql = f"`{item_props}`" if item_props else "''"
    qty_sql = f"`{item_qty}`" if item_qty else "''"
    so_sql = f"`{so_col}`" if so_col else "''"
    status_sql = f"`{status_col}`" if status_col else "''"
    shop_sql = f"`{shop_col}`" if shop_col else "''"
    date_sql = f"`{date_col}`" if date_col else "''"
    wanted = [str(item).strip() for item in o_ids or [] if str(item).strip()]
    started = time.monotonic()
    with connect(env, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT status, last_success_at, error_message
                   FROM realtime_sync_state WHERE source_name='orders' LIMIT 1"""
            )
            sync = _sync_payload(cursor.fetchone() or {})
            sku_sql = f"SELECT DISTINCT `{item_oid}` AS o_id FROM `{item_table}` WHERE `{item_sku}`=%s"
            sku_params: list = [SOURCE_SKU]
            if wanted:
                sku_sql += " AND `{col}` IN ({ph})".format(
                    col=item_oid, ph=",".join(["%s"] * len(wanted)),
                )
                sku_params.extend(wanted)
            cursor.execute(sku_sql, sku_params)
            sku_oids = [
                str(row.get("o_id") or "").strip()
                for row in (cursor.fetchall() or [])
                if str(row.get("o_id") or "").strip()
            ]
            if not sku_oids:
                return [], sync
            headers: dict[str, dict] = {}
            live = tuple(PROCESSABLE_STATUSES | PARKED_STATUSES)
            keys = shop_keys(shops)
            for chunk in _chunks(sku_oids):
                marks = ",".join(["%s"] * len(chunk))
                filters = [f"`{oid_col}` IN ({marks})"]
                params: list = list(chunk)
                if not wanted:
                    if status_col:
                        filters.append(
                            f"`{status_col}` IN ({','.join(['%s'] * len(live))})"
                        )
                        params.extend(live)
                    if shop_col and keys:
                        filters.append(
                            "(" + " OR ".join([f"`{shop_col}` LIKE %s"] * len(keys)) + ")"
                        )
                        params.extend([f"%{key}%" for key in keys])
                cursor.execute(
                    f"""SELECT `{oid_col}` AS o_id, {so_sql} AS so_id,
                               {status_sql} AS status, {shop_sql} AS shop_name,
                               {date_sql} AS order_date
                        FROM `{order_table}`
                        WHERE {" AND ".join(filters)}""",
                    params,
                )
                for row in cursor.fetchall() or []:
                    oid = str(row.get("o_id") or "").strip()
                    if oid:
                        headers[oid] = dict(row)
            if not headers:
                return [], sync
            items: list[dict] = []
            for chunk in _chunks(list(headers)):
                marks = ",".join(["%s"] * len(chunk))
                cursor.execute(
                    f"""SELECT `{item_oid}` AS o_id, `{item_sku}` AS sku_id,
                               {style_sql} AS i_id, {name_sql} AS name,
                               {props_sql} AS properties_value, {qty_sql} AS qty
                        FROM `{item_table}`
                        WHERE `{item_oid}` IN ({marks})""",
                    list(chunk),
                )
                items.extend(dict(row) for row in (cursor.fetchall() or []))
    lines = merge_insole_lines(headers, items)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if elapsed_ms >= 800:
        logger.info("鞋垫定位分步查询 %s 单 / %s 行，用时 %sms", len(headers), len(lines), elapsed_ms)
    return lines, sync


def locate_insole_orders(
    setting: Callable[[str, str], str] | None = None,
    env_path: str = "",
    *,
    shop: str = DEFAULT_SHOP,
    o_ids: list[str] | None = None,
    lines: list[dict] | None = None,
    sync: dict | None = None,
    written: dict | None = None,
    reserved: dict | None = None,
    exclude_action_id: str | None = None,
    viewer: str | None = None,
    root=None,
) -> dict:
    """定位抖音鞋垫候选。`lines` 供离线用例注入，不连库。"""
    special_mapping()
    if lines is None:
        if setting is None:
            raise OrderSourceError("订单镜像查询尚未配置")
        lines, sync = fetch_insole_lines(
            setting, env_path, o_ids=o_ids, shops=shop,
        )
        if written is None:
            written = load_written_insole_orders(setting, root=root)
        if reserved is None:
            db = resolve_repo_path(
                setting("AGENT_DATABASE_PATH", "files/data/agent.sqlite3"),
                root=Path(root) if root is not None else ROOT,
            )
            reserved = load_reserved_insole_orders(
                db, exclude_action_id=exclude_action_id, viewer=viewer,
            )
    else:
        if written is None:
            written = {}
        if reserved is None:
            reserved = {}
    wanted = {str(item).strip() for item in (o_ids or []) if str(item).strip()}
    classified = []
    for order in _aggregate_orders(lines):
        if wanted and order["o_id"] not in wanted:
            continue
        classified.append(classify_insole_row(
            order, shop=shop, written=written, reserved=reserved,
        ))
    processable = [row for row in classified if row["bucket"] == "processable"]
    parked = [row for row in classified if row["bucket"] == "parked"]
    skipped = [row for row in classified if row["bucket"] == "skipped"]
    return {
        "sourceSku": SOURCE_SKU,
        "shop": ",".join(shop_keys(shop)),
        "sync": sync or {},
        "processable": processable,
        "parked": parked,
        "skipped": skipped,
        "processableCount": len(processable),
        "parkedCount": len(parked),
        "skippedCount": len(skipped),
        "oIds": [row["o_id"] for row in processable],
    }


def public_order(row: dict) -> dict:
    return {
        "oId": row.get("o_id") or "",
        "soId": row.get("so_id") or "",
        "status": row.get("status") or "",
        "shop": row.get("shop") or "",
        "orderDate": row.get("order_date") or "",
        "shoeSku": row.get("shoe_sku") or "",
        "shoeProps": row.get("shoe_props") or "",
        "shoeMm": row.get("shoe_mm") or "",
        "targetSku": row.get("target_sku") or "",
        "sizeNote": row.get("size_note") or "",
        "sourceSku": row.get("source_sku") or SOURCE_SKU,
        "reason": row.get("reason") or "",
        "at": row.get("at") or "",
    }


def _done_order(row: dict | None = None, *, oid="", target="", at="", reason=WRITTEN_REASON) -> dict:
    payload = public_order({
        **(row or {}),
        "o_id": (row or {}).get("o_id") or oid,
        "target_sku": (row or {}).get("target_sku") or target,
        "status": DONE_STATUS,
        "reason": reason,
        "at": at or (row or {}).get("at") or "",
    })
    payload["status"] = DONE_STATUS
    payload["reason"] = reason
    return payload


def recent_done_orders(*, setting=None, root=None, extra=None, limit: int = DONE_LIMIT) -> list[dict]:
    """刚写成功的单：镜像可能还是 Question，清单里要立刻显示已处理。"""
    written = load_written_insole_orders(setting, root=root)
    rows = []
    seen = set()
    for item in extra or []:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("oId") or item.get("o_id") or "").strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        rows.append(item if item.get("oId") else _done_order(
            oid=oid,
            target=str(item.get("targetSku") or item.get("target_sku") or ""),
            at=str(item.get("at") or ""),
        ))
    for oid, meta in sorted(
        written.items(),
        key=lambda item: str(item[1].get("at") or ""),
        reverse=True,
    ):
        if oid in seen:
            continue
        seen.add(oid)
        rows.append(_done_order(
            oid=oid,
            target=str(meta.get("target_sku") or ""),
            at=str(meta.get("at") or ""),
        ))
    return rows[: max(1, int(limit))]


def format_insole_list(located: dict, *, limit: int = 5, pending_confirm: bool = True) -> str:
    """钉钉 / 对话用的订单清单，只展开处理信息与目标鞋垫。"""
    processable = located.get("processable") or []
    parked = located.get("parked") or []
    pool = str(located.get("shop") or "").replace(",", "/") or "抖音/快手/视频号"
    lines = [
        f"鞋垫待处理 {located.get('processableCount') or 0} 单"
        f"（{pool}；Question / WaitConfirm；半码按码数舍去小数后映射）。",
    ]
    shown = processable[: max(1, int(limit))]
    if shown:
        lines.append("内部单号 / 平台单号　状态　鞋码 → 目标鞋垫")
    for row in shown:
        lines.append(
            f"- {row['o_id']} / {row.get('so_id') or '-'}　{row.get('status') or ''}　"
            f"{row.get('shoe_props') or '无鞋码'} → {row.get('target_sku')}"
        )
    extra = len(processable) - len(shown)
    if extra > 0:
        if pending_confirm:
            lines.append(f"还有 {extra} 单未展开，确认后按完整清单处理。")
        else:
            lines.append(f"还有 {extra} 单未展开，按完整清单写入。")
    if parked:
        reasons = defaultdict(int)
        for row in parked:
            reasons[row.get("reason") or "暂不处理"] += 1
        detail = "；".join(f"{name} {count}" for name, count in reasons.items())
        lines.append(f"暂不处理 {len(parked)} 单：{detail}。")
    written_skipped = [
        row for row in (located.get("skipped") or [])
        if (row.get("reason") or "") == WRITTEN_REASON
    ]
    if written_skipped:
        lines.append(f"已排除刚写入、镜像尚未跟上的 {len(written_skipped)} 单。")
    reserved_skipped = [
        row for row in (located.get("skipped") or [])
        if str(row.get("reason") or "").startswith(RESERVED_REASON)
    ]
    if reserved_skipped:
        lines.append(f"已排除他人待确认或正在写入的 {len(reserved_skipped)} 单。")
    if located.get("sync"):
        stamp = located["sync"].get("lastSuccessAt") or located["sync"].get("status") or ""
        if stamp:
            lines.append(f"镜像同步：{stamp}")
    return "\n".join(lines)


def format_elapsed(ms) -> str:
    """把毫秒收成「11 秒」「1 分 3 秒」，给钉钉结果日志用。"""
    try:
        total = max(0, int(round(float(ms) / 1000)))
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分")
    if seconds or not parts:
        parts.append(f"{seconds} 秒")
    return " ".join(parts)


def format_insole_result(result: dict | None, *, limit: int = 5, elapsed_ms=None) -> str:
    """确认执行后的结果日志；只展开处理信息与写入结果。"""
    result = result or {}
    ok = int(result.get("okCount") or 0)
    skipped = int(result.get("skippedCount") or 0)
    failed = int(result.get("failedCount") or 0)
    elapsed = elapsed_ms
    if elapsed is None:
        elapsed = result.get("elapsedMs")
        if elapsed is None:
            elapsed = result.get("elapsed_ms")
    kind = str(result.get("headline") or "鞋垫换货").strip() or "鞋垫换货"
    headline = f"【任务完成】{kind}：成功 {ok}，跳过 {skipped}，失败 {failed}"
    pretty = format_elapsed(elapsed)
    if pretty:
        headline += f"，用时 {pretty}"
    phases = []
    prepare_ms = result.get("prepareMs")
    write_ms = result.get("writeMs")
    before_ms = result.get("beforeMs")
    after_ms = result.get("afterMs")
    read_ms = result.get("readMs")
    if prepare_ms not in (None, ""):
        phases.append(f"开页 {format_elapsed(prepare_ms)}")
    if write_ms not in (None, ""):
        phases.append(f"写入 {format_elapsed(write_ms)}")
    if before_ms not in (None, "") or after_ms not in (None, ""):
        if before_ms not in (None, ""):
            phases.append(f"写前快照 {format_elapsed(before_ms)}")
        if after_ms not in (None, ""):
            phases.append(f"写后回读 {format_elapsed(after_ms)}")
    elif read_ms not in (None, ""):
        phases.append(f"回读 {format_elapsed(read_ms)}")
    if phases:
        headline += f"（{'，'.join(phases)}）"
    lines = [headline + "。"]
    rows = list(result.get("log") or [])
    if not rows:
        for oid in result.get("oIds") or []:
            rows.append({"oId": oid, "result": "ok"})
    shown = rows[: max(1, int(limit))]
    if shown:
        lines.append("内部单号　结果　目标鞋垫")
    for row in shown:
        status = row.get("result") or "ok"
        extra = f"　{row['targetSku']}" if row.get("targetSku") else ""
        err = f"（{row.get('error')}）" if row.get("error") else ""
        lines.append(f"- {row.get('oId') or row.get('o_id')}　{status}{extra}{err}")
    extra_n = len(rows) - len(shown)
    if extra_n > 0:
        lines.append(f"其余 {extra_n} 单已缩略。")
    return "\n".join(lines)


def format_insole_start(located: dict | None, *, slot: str = "", trigger: str = "schedule",
                        limit: int = 5) -> str:
    """定时/手动开跑时的清单，不要求员工再确认。"""
    located = located or {}
    count = int(located.get("processableCount") or 0)
    if trigger == "manual":
        headline = f"【开始执行】手动触发抖音换鞋垫：本轮 {count} 单"
    elif slot:
        headline = f"【开始执行】定时抖音换鞋垫 {slot}：本轮 {count} 单"
    else:
        headline = f"【开始执行】定时抖音换鞋垫：本轮 {count} 单"
    return "\n".join([
        headline + "。",
        format_insole_list(located, limit=limit, pending_confirm=False),
    ])


def format_insole_idle(located: dict | None = None, *, slot: str = "",
                       trigger: str = "schedule") -> str:
    """本轮没有可写订单。有人工待确认时不要说成「没有待处理」。"""
    located = located or {}
    parked = list(located.get("parked") or [])
    parked_n = int(located.get("parkedCount") or len(parked) or 0)
    reserved = [
        row for row in (located.get("skipped") or [])
        if str(row.get("reason") or "").startswith(RESERVED_REASON)
    ]
    if trigger == "manual":
        headline = "【本轮结束】手动触发抖音换鞋垫"
    elif slot:
        headline = f"【本轮结束】定时抖音换鞋垫 {slot}"
    else:
        headline = "【本轮结束】定时抖音换鞋垫"
    if reserved:
        body = f"有 {len(reserved)} 单在人工待确认，本轮不抢"
    else:
        body = "没有待处理订单"
    extras = []
    if parked_n:
        reasons = defaultdict(int)
        for row in parked:
            reasons[str(row.get("reason") or "暂不处理")] += 1
        if reasons:
            extras.append("；".join(f"{name} {count}" for name, count in reasons.items()))
        else:
            extras.append(f"暂不处理 {parked_n} 单")
    if extras:
        body += f"（{'；'.join(extras)}）"
    return f"{headline}：{body}。"


def build_insole_log(processable: list[dict], executed: dict | None) -> list[dict]:
    """把写入结果对齐到定位清单，供结果日志和回写镜像。"""
    executed = executed or {}
    ok_ids = {str(item.get("o_id") or "") for item in executed.get("succeeded") or []}
    fail_map = {
        str(item.get("o_id") or ""): str(item.get("error") or item.get("reason") or "")
        for item in executed.get("failed") or []
    }
    skip_map = {
        str(item.get("o_id") or ""): str(item.get("reason") or "")
        for item in executed.get("plans") or [] if not item.get("ok")
    }
    log = []
    for row in processable:
        oid = str(row.get("o_id") or "")
        target = row.get("target_sku") or ""
        if oid in ok_ids:
            log.append({"oId": oid, "targetSku": target, "result": "ok"})
        elif oid in fail_map:
            log.append({
                "oId": oid, "targetSku": target,
                "result": "failed", "error": fail_map[oid],
            })
        else:
            log.append({
                "oId": oid, "targetSku": target,
                "result": "skipped", "error": skip_map.get(oid, ""),
            })
    return log


def persist_insole_success(log: list[dict], *, env_path: str, root=None, mirror=None) -> list[dict]:
    """把成功写入的单记进台账并回写镜像。"""
    writes = [
        {"o_id": item["oId"], "target_sku": item.get("targetSku") or ""}
        for item in log if item.get("result") == "ok" and item.get("oId")
    ]
    if writes:
        remember_insole_writes(writes, root=root)
        sync_insole_mirror(env_path, writes, mirror=mirror)
        invalidate_insole_lines_cache()
    return writes


def exchange_job_for(target_sku: str, o_ids: list[str]) -> dict:
    special = special_mapping()
    if target_sku not in special["targetSkus"]:
        raise ExchangeError(f"目标 SKU {target_sku} 不在鞋垫白名单")
    return {
        "rules": {
            "strategy": "direct",
            "replacements": [{
                "from": SOURCE_SKU,
                "to": target_sku,
                "sourceStyle": special["sourceStyle"],
                "targetStyle": special["targetStyle"],
            }],
        },
        "targets": {"o_ids": list(o_ids), "limit": max(1, min(len(o_ids), 500))},
    }


def execute_insole_orders(runtime: Any, orders: list[dict]) -> dict:
    """按目标 SKU 分组试算，再一次串行 execute。整批作业持有同一把锁。"""
    if runtime is None:
        raise ExchangeError(
            "ERP Digital Worker 未装配。请先 scripts/run_erp_worker.py login，"
            "不要求打开 ERP_AI_ENABLED"
        )
    if not orders:
        raise ExchangeError("没有可处理的鞋垫订单")
    exclusive = getattr(runtime, "exclusive", None)
    with exclusive() if callable(exclusive) else nullcontext():
        prepare_started = time.monotonic()
        prepare = getattr(runtime, "prepare", None)
        if callable(prepare):
            prepare()
        prepare_ms = int((time.monotonic() - prepare_started) * 1000)
        planned = []
        for order in orders:
            target = str(order.get("target_sku") or "")
            oid = str(order.get("o_id") or "")
            if not oid or not target:
                planned.append({
                    "o_id": oid, "ok": False,
                    "reason": "缺少内部单号或目标鞋垫",
                })
                continue
            planned.append({
                "o_id": oid,
                "ok": True,
                "mode": "ChangeItem",
                "src_sku_id": SOURCE_SKU,
                "new_sku_id": target,
            })
        ok_plans = [item for item in planned if item.get("ok")]
        skipped_plans = [item for item in planned if not item.get("ok")]
        executed = {"succeeded": [], "failed": [], "attempted": 0}
        write_started = time.monotonic()
        if ok_plans:
            executed = runtime.run("erp.exchange_items", {
                "confirm": True,
                "plans": ok_plans,
                "plan": {"plans": ok_plans},
                "delayMs": INSOLE_WRITE_DELAY_MS,
                "concurrency": INSOLE_WRITE_CONCURRENCY,
                "readConcurrency": INSOLE_READ_CONCURRENCY,
            })
        write_ms = int((time.monotonic() - write_started) * 1000)
        return {
            "okCount": len(executed.get("succeeded") or []),
            "skippedCount": len(skipped_plans),
            "failedCount": len(executed.get("failed") or []),
            "attempted": executed.get("attempted") or len(ok_plans),
            "prepareMs": prepare_ms,
            "writeMs": executed.get("elapsedMs", write_ms),
            "readMs": executed.get("readMs"),
            "beforeMs": executed.get("beforeMs"),
            "afterMs": executed.get("afterMs"),
            "plans": planned,
            "succeeded": executed.get("succeeded") or [],
            "failed": executed.get("failed") or [],
            "reconciliation": executed.get("reconciliation") or {},
            "evidence": executed.get("evidence") or {},
        }


def list_insole_board(
    setting: Callable[[str, str], str] | None = None,
    env_path: str = "",
    *,
    shop: str = DEFAULT_SHOP,
    viewer: str | None = None,
    root=None,
) -> dict:
    """网页换货页用的鞋垫清单。"""
    located = locate_insole_orders(
        setting, env_path, shop=shop, viewer=viewer, root=root,
    )
    from_locate = [
        _done_order(row)
        for row in located.get("skipped") or []
        if row.get("reason") == WRITTEN_REASON
    ]
    done = recent_done_orders(setting=setting, root=root, extra=from_locate)
    return {
        "shop": located["shop"],
        "sourceSku": located["sourceSku"],
        "sync": located["sync"],
        "processableCount": located["processableCount"],
        "parkedCount": located["parkedCount"],
        "skippedCount": located["skippedCount"],
        "doneCount": len(done),
        "processable": [public_order(row) for row in located["processable"]],
        "parked": [public_order(row) for row in located["parked"]],
        "done": done,
    }


def process_one_insole_order(
    o_id,
    *,
    setting: Callable[[str, str], str] | None = None,
    env_path: str = "",
    erp: Any = None,
    root=None,
    mirror=None,
    viewer: str | None = None,
    shop: str = DEFAULT_SHOP,
) -> dict:
    """网页点「处理」：只写这一单，点击即确认。"""
    oid = str(o_id or "").strip()
    if not oid:
        raise ExchangeError("请指定内部单号")
    started = time.monotonic()
    located = locate_insole_orders(
        setting, env_path, shop=shop, o_ids=[oid],
        viewer=viewer, root=root,
    )
    if not located["processable"]:
        if located["parked"]:
            raise ExchangeError(f"{oid} 是发货中，暂不处理")
        skipped = located["skipped"]
        if skipped:
            raise ExchangeError(str(skipped[0].get("reason") or "这单现在不能处理"))
        raise ExchangeError("找不到可处理的鞋垫订单")
    result = execute_insole_orders(erp, located["processable"])
    log = build_insole_log(located["processable"], result)
    persist_insole_success(
        log, env_path=env_path, root=root, mirror=mirror,
    )
    order = public_order(located["processable"][0])
    done = _done_order(located["processable"][0]) if result["okCount"] else None
    return {
        "okCount": result["okCount"],
        "skippedCount": result["skippedCount"],
        "failedCount": result["failedCount"],
        "attempted": result["attempted"],
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "order": order,
        "done": done,
        "log": log,
        "failed": result.get("failed") or [],
        "reconciliation": result.get("reconciliation") or {},
        "evidence": result.get("evidence") or {},
    }
