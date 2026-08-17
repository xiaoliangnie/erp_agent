# -*- coding: utf-8 -*-
"""抖音换鞋垫：镜像定位 + 鞋码映射 + 串行写入。

尺码来自同单鞋子规格。半码按码数舍去小数（40.5→40）再换算毫米。
默认只处理 Question / WaitConfirm。Delivering / 发货中只列出，不写 ERP。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .policy import load_policy
from .service import ExchangeError
from ..business_time import BUSINESS_TIMEZONE, business_now
from ..order_source import OrderSourceError, _identifier, _mirror_state, source_status
from ..database import connect
from ..paths import DATA_DIR, ROOT, resolve_repo_path

logger = logging.getLogger(__name__)


SOURCE_SKU = "XZ25401308-101"
DEFAULT_SHOP = "抖音"
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
TARGET_PREFIXES = ("XZ25401308-099", "XZ25401308099")
INSOLE_WRITTEN_PATH = DATA_DIR / "insole_written.json"
WRITTEN_TTL_HOURS = 48
WRITTEN_REASON = "本批已写入，镜像尚未跟上"
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
    return match.group(1) if match else ""


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


def sync_insole_mirror(env_path: str, writes: list[dict], *, mirror=None) -> dict:
    """写入成功后先按单拉代理，再把源 SKU 换成目标，不等 60 秒增量。"""
    oids = [str(item.get("o_id") or "") for item in writes if item.get("o_id")]
    refreshed: dict = {}
    if mirror is not None and oids:
        try:
            refreshed = mirror.refresh_orders(oids)
        except Exception as exc:
            logger.warning("鞋垫写入后按单刷新镜像失败：%s", exc)
            refreshed = {"ok": False, "error": str(exc)}
    applied = []
    if env_path and writes and Path(env_path).is_file():
        try:
            from ..realtime_mirror import replace_order_item_sku
            for item in writes:
                oid = str(item.get("o_id") or "")
                target = str(item.get("target_sku") or "")
                if oid and target and replace_order_item_sku(env_path, oid, SOURCE_SKU, target):
                    applied.append(oid)
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


def classify_insole_row(order: dict, *, shop: str = DEFAULT_SHOP, written: dict | None = None) -> dict:
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
    if shop and shop not in shop_name:
        row["bucket"] = "skipped"
        row["reason"] = f"非{shop}店铺"
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


def fetch_insole_lines(setting: Callable[[str, str], str], env_path: str) -> tuple[list[dict], dict]:
    """从订单镜像拉出仍含源鞋垫 SKU 的整单明细。"""
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
    style_sql = f"i.`{item_style}`" if item_style else "''"
    name_sql = f"i.`{item_name}`" if item_name else "''"
    props_sql = f"i.`{item_props}`" if item_props else "''"
    qty_sql = f"i.`{item_qty}`" if item_qty else "''"
    so_sql = f"o.`{so_col}`" if so_col else "''"
    status_sql = f"o.`{status_col}`" if status_col else "''"
    shop_sql = f"o.`{shop_col}`" if shop_col else "''"
    date_sql = f"o.`{date_col}`" if date_col else "''"
    sql = f"""
        SELECT o.`{oid_col}` AS o_id, {so_sql} AS so_id, {status_sql} AS status,
               {shop_sql} AS shop_name, {date_sql} AS order_date,
               i.`{item_sku}` AS sku_id, {style_sql} AS i_id, {name_sql} AS name,
               {props_sql} AS properties_value, {qty_sql} AS qty
        FROM `{item_table}` i
        JOIN `{order_table}` o ON CAST(o.`{oid_col}` AS CHAR)=CAST(i.`{item_oid}` AS CHAR)
        WHERE i.`{item_oid}` IN (
            SELECT DISTINCT `{item_oid}` FROM `{item_table}` WHERE `{item_sku}`=%s
        )
        ORDER BY o.`{oid_col}` DESC, i.`{item_sku}`
    """
    with connect(env, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT status, last_success_at, error_message
                   FROM realtime_sync_state WHERE source_name='orders' LIMIT 1"""
            )
            sync = cursor.fetchone() or {}
            cursor.execute(sql, (SOURCE_SKU,))
            lines = list(cursor.fetchall() or [])
    return lines, {
        "status": str(sync.get("status") or ""),
        "lastSuccessAt": str(sync.get("last_success_at") or ""),
        "errorMessage": str(sync.get("error_message") or ""),
    }


def locate_insole_orders(
    setting: Callable[[str, str], str] | None = None,
    env_path: str = "",
    *,
    shop: str = DEFAULT_SHOP,
    o_ids: list[str] | None = None,
    lines: list[dict] | None = None,
    sync: dict | None = None,
    written: dict | None = None,
    root=None,
) -> dict:
    """定位抖音鞋垫候选。`lines` 供离线用例注入，不连库。"""
    special_mapping()
    if lines is None:
        if setting is None:
            raise OrderSourceError("订单镜像查询尚未配置")
        lines, sync = fetch_insole_lines(setting, env_path)
        if written is None:
            written = load_written_insole_orders(setting, root=root)
    elif written is None:
        written = {}
    wanted = {str(item).strip() for item in (o_ids or []) if str(item).strip()}
    classified = []
    for order in _aggregate_orders(lines):
        if wanted and order["o_id"] not in wanted:
            continue
        classified.append(classify_insole_row(
            order, shop=shop or DEFAULT_SHOP, written=written,
        ))
    processable = [row for row in classified if row["bucket"] == "processable"]
    parked = [row for row in classified if row["bucket"] == "parked"]
    skipped = [row for row in classified if row["bucket"] == "skipped"]
    return {
        "sourceSku": SOURCE_SKU,
        "shop": shop or DEFAULT_SHOP,
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
    }


def format_insole_list(located: dict, *, limit: int = 5) -> str:
    """钉钉 / 对话用的订单清单，只展开处理信息与目标鞋垫。"""
    processable = located.get("processable") or []
    parked = located.get("parked") or []
    lines = [
        f"抖音鞋垫待处理 {located.get('processableCount') or 0} 单"
        f"（Question / WaitConfirm；半码按码数舍去小数后映射）。",
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
        lines.append(f"还有 {extra} 单未展开，确认后按完整清单处理。")
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
    if located.get("sync"):
        stamp = located["sync"].get("lastSuccessAt") or located["sync"].get("status") or ""
        if stamp:
            lines.append(f"镜像同步：{stamp}")
    return "\n".join(lines)


def format_insole_result(result: dict | None, *, limit: int = 5) -> str:
    """确认执行后的结果日志；只展开处理信息与写入结果。"""
    result = result or {}
    ok = int(result.get("okCount") or 0)
    skipped = int(result.get("skippedCount") or 0)
    failed = int(result.get("failedCount") or 0)
    lines = [f"【任务完成】鞋垫换货：成功 {ok}，跳过 {skipped}，失败 {failed}。"]
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
    """按目标 SKU 分组试算，再一次串行 execute。写并发仍是 1。"""
    if runtime is None:
        raise ExchangeError(
            "ERP Digital Worker 未装配。请先 scripts/run_erp_worker.py login，"
            "不要求打开 ERP_AI_ENABLED"
        )
    if not orders:
        raise ExchangeError("没有可处理的鞋垫订单")
    prepare = getattr(runtime, "prepare", None)
    if callable(prepare):
        prepare()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for order in orders:
        grouped[str(order.get("target_sku") or "")].append(order)
    planned = []
    for target, group in grouped.items():
        result = runtime.run("erp.exchange_items", exchange_job_for(
            target, [item["o_id"] for item in group],
        ))
        planned.extend(result.get("plans") or [])
    ok_plans = [item for item in planned if item.get("ok")]
    skipped_plans = [item for item in planned if not item.get("ok")]
    executed = {"succeeded": [], "failed": [], "attempted": 0}
    if ok_plans:
        executed = runtime.run("erp.exchange_items", {
            "confirm": True,
            "plans": ok_plans,
            "plan": {"plans": ok_plans},
        })
    return {
        "okCount": len(executed.get("succeeded") or []),
        "skippedCount": len(skipped_plans),
        "failedCount": len(executed.get("failed") or []),
        "attempted": executed.get("attempted") or len(ok_plans),
        "plans": planned,
        "succeeded": executed.get("succeeded") or [],
        "failed": executed.get("failed") or [],
    }
