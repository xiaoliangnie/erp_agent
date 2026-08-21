# -*- coding: utf-8 -*-
"""单款分析：模型先调工具读销量，再对照同品类写建议。

鞋服看季节阶段；自营百货用专属提示词，看七窗日销是否脉冲。
当天同一款只分析一次，结果落 `files/data/spu-analyze-cache.json`。
隔天缓存仍可读，但标 stale，要员工点重新分析。
不接公开网页搜索。建议必须写理由。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import date
from pathlib import Path

from ..business_time import business_now, business_today
from ..paths import local_dir
from .formula import baihuo_window_rates, daily_avg_baihuo, monthly_sales_baihuo

logger = logging.getLogger(__name__)

CACHE_NAME = "spu-analyze-cache.json"
MAX_STEPS = 6
REQUIRED_TOOLS = ("spu_style_sales", "spu_category_peers", "spu_season_notes")
BAIHUO_REQUIRED_TOOLS = ("spu_style_sales", "spu_category_peers", "spu_baihuo_notes")
_CACHE_LOCK = threading.Lock()

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "spu_style_sales",
            "description": (
                "读取该款在结果表里的销量、库存、标签、研判要点。"
                "必须先调用再下结论。数字已经算好，禁止改、禁止再算一遍当结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "styleId": {"type": "string", "description": "款式编码"},
                },
                "required": ["styleId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spu_category_peers",
            "description": (
                "同品类线对照款：服装优先同季节、同品类（T恤对T恤）。"
                "不是本款数字，也不是同面料。引用必须写对照款编码和近7天。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "styleId": {"type": "string", "description": "本款编码，用来圈同品类"},
                    "limit": {"type": "integer", "description": "对照款数量，默认 5，最多 8"},
                },
                "required": ["styleId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spu_season_notes",
            "description": (
                "按商品资料季节、当前月份、品名给出本款所处阶段"
                "（前置备货 / 当季主力 / 尾季清货 / 淡季）。"
                "服装必须先看这条再下补货结论。不是网上搜的，不能当销量数字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "styleId": {"type": "string", "description": "本款编码"},
                },
                "required": ["styleId"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "你是鞋服企业的资深采购计划。表上的日均、周转、补货建议、建议下单"
    "已经算好了，员工自己看得见，你不要再复述。\n"
    "\n"
    "【工具】必须读完三份工具再写：spu_style_sales、spu_category_peers、"
    "spu_season_notes。缺一不可。\n"
    "\n"
    "【禁止】不要把近7天/7~14天/周环比/周转/日均/补货建议/建议下单"
    "逐项报一遍。不要写「建议按建议下单补××」。不要编促销和外部新闻。\n"
    "\n"
    "【建议必须有理由】必须在 跟 / 减量观望 / 只补断码 / 不补 里选一个，"
    "写成「建议：X。因为①…②…」。至少两条工具依据，依据只能来自："
    "季节阶段、对照款编码+近7天、近30天形态、库存结构、新品标签、在途可撑天数、进货仓。"
    "进货仓有数时不要按补货建议再下一单。"
    "禁止写同面料、禁止编促销、禁止把对照款品名安到本款头上。\n"
    "\n"
    "【季节】服装先看 spu_season_notes 的阶段。"
    "8 月夏季款=尾季清货；6–8 月加绒/冬季=前置备货。"
    "新品标签时近7天低是铺货，不能当滞销。资料毛利率只在尾季讨论压货时可用，"
    "不能单独拿毛利决定补不补。\n"
    "\n"
    "【数字】原样抄工具返回值，禁止自己演算新数。说不清写「需人工核实」。\n"
    "\n"
    "【输出】四行：「趋势：」「库存：」「建议：」「复核：」。"
    "趋势用研判要点里的近30天形态；库存用库存结构；"
    "建议行不超过90字；复核没有写「无」。"
)

BAIHUO_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "spu_style_sales",
            "description": (
                "读取该款在自营百货结果表里的 7/14/15/30 天销量、三窗折月日均、库存、研判要点。"
                "必须先调用再下结论。日均已经算好，禁止改、禁止再算一遍当结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "styleId": {"type": "string", "description": "款式编码"},
                },
                "required": ["styleId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spu_category_peers",
            "description": (
                "同品类线对照款，按近 30 天出库排序。引用必须写对照款编码和 15/30 天。"
                "不是本款数字。百货不要按近 7 天排名。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "styleId": {"type": "string", "description": "本款编码，用来圈同品类"},
                    "limit": {"type": "integer", "description": "对照款数量，默认 5，最多 8"},
                },
                "required": ["styleId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spu_baihuo_notes",
            "description": (
                "自营百货研判备忘：慢动销 / 短窗脉冲 / 礼品团购形态。"
                "必须先看这条再下补货结论。不是网上搜的，不能当销量数字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "styleId": {"type": "string", "description": "本款编码"},
                },
                "required": ["styleId"],
            },
        },
    },
]

BAIHUO_SYSTEM_PROMPT = (
    "你是文创百货、礼品和自营日用的采购计划，不是鞋服买手。\n"
    "表上的日均、周转、补货建议已经算好："
    "月销量=(7天×4 + 15天×2 + 30天)/3，日均=月销量/30。"
    "员工自己看得见，你不要重算，也不要复述这些数字当结论。\n"
    "\n"
    "【工具】必须读完三份再写：spu_style_sales、spu_category_peers、"
    "spu_baihuo_notes。缺一不可。\n"
    "\n"
    "【禁止】不要用服装季节、加绒、冰爽、尾季清货、前置备货这些鞋服话。"
    "不要把近7天当主依据（百货经常整周是 0，这是慢动销不是滞销）。"
    "不要编团购、会议、政务订单。不要把补货建议/建议下单抄进建议行。\n"
    "\n"
    "【看什么】先看 7/15/30 天是否同向。"
    "7 天明显高于 30 天 = 短窗脉冲，不要按脉冲外推 30 天覆盖。"
    "30 天有量、7 天是 0 = 慢动销正常。"
    "对照款用 15/30 天，不要用近 7 天排名。"
    "销量已拆线上/线下：按 ERP 店铺设置分组，窗口是 7/15/30 天。"
    "线下=消防/渠道/公安/交警业务部 + 内部店铺；其余线上。"
    "工具里的「出库店铺」是镜像出库对上的真实店名和分组，禁止编造店铺、客户或团购。"
    "线下（渠道 KA、内部调拨尤其）可能一批出很多、过几天又是 0，不稳定；"
    "短窗热且线下占比高，不要按脉冲外推 30 天覆盖，优先少补或观望。"
    "线上稳、线下偶发时跟线上节奏，线下当一次性。"
    "礼品/文创常见「很久不走、忽然一批」：少补或观望。"
    "新品标签时长窗低是铺货，不能当滞销。"
    "在途能撑过短窗脉冲就先不补。"
    "进货仓有数时货已经到仓未上架，总库存公式不含它，禁止按补货建议再下一单。"
    "百货多数一款一码，不要用断码、缺码、尺码这些鞋服话。\n"
    "\n"
    "【建议必须有理由】必须在 跟 / 减量观望 / 少补 / 不补 里选一个，"
    "写成「建议：X。因为①…②…」。至少两条工具依据，依据只能来自："
    "7/15/30天对比、对照款编码+15/30天、近30天形态、库存结构、新品/礼品标签、"
    "在途可撑天数、进货仓。禁止编促销。\n"
    "\n"
    "【数字】原样抄工具返回值，禁止自己演算新数。说不清写「需人工核实」。\n"
    "\n"
    "【输出】四行：「趋势：」「库存：」「建议：」「复核：」。"
    "趋势用 7/15/30 对比和近30天形态；库存用库存结构；"
    "建议行不超过90字；复核没有写「无」。"
)


def cache_path(*, root=None) -> Path:
    return local_dir("data", root=root) / CACHE_NAME


def _read_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".spu-analyze-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def load_cached_analysis(style_id: str, *, today: date | None = None, root=None) -> dict | None:
    """读该款缓存。返回带 stale 标记的副本；没有则 None。"""
    style_id = str(style_id or "").strip()
    if not style_id:
        return None
    day = (today or business_today()).isoformat()
    with _CACHE_LOCK:
        row = _read_cache(cache_path(root=root)).get(style_id)
    if not isinstance(row, dict) or not row.get("analysis"):
        return None
    cached_day = str(row.get("day") or "")
    return {
        "ok": True,
        "styleId": style_id,
        "analysis": str(row.get("analysis") or ""),
        "analyzedAt": str(row.get("analyzedAt") or ""),
        "day": cached_day,
        "model": str(row.get("model") or ""),
        "cached": True,
        "stale": cached_day != day,
    }


def load_day_analyses(*, today: date | None = None, root=None, board: str | None = None) -> dict[str, dict]:
    """当天（及过期）缓存，给看板一次带上，关掉抽屉再打开不用重跑。"""
    day = (today or business_today()).isoformat()
    wanted = str(board or "").strip().lower()
    with _CACHE_LOCK:
        payload = _read_cache(cache_path(root=root))
    out: dict[str, dict] = {}
    for style_id, row in payload.items():
        if not isinstance(row, dict) or not row.get("analysis"):
            continue
        row_board = str(row.get("board") or "apparel").strip().lower()
        if wanted and row_board != wanted:
            continue
        cached_day = str(row.get("day") or "")
        out[str(style_id)] = {
            "analysis": str(row.get("analysis") or ""),
            "analyzedAt": str(row.get("analyzedAt") or ""),
            "day": cached_day,
            "stale": cached_day != day,
            "cached": True,
        }
    return out


def save_cached_analysis(style_id: str, row: dict, *, root=None) -> None:
    style_id = str(style_id or "").strip()
    if not style_id:
        return
    path = cache_path(root=root)
    with _CACHE_LOCK:
        payload = _read_cache(path)
        payload[style_id] = row
        _write_cache(path, payload)


def find_style(snapshot: dict, style_id: str) -> dict | None:
    wanted = str(style_id or "").strip()
    for item in snapshot.get("styles") or []:
        if item.get("styleId") == wanted:
            return item
    return None


def style_sales_payload(style: dict, snapshot: dict) -> dict:
    """给模型的本款数据包。字段名用 7~14天，不用「前7天」。"""
    board = str(snapshot.get("board") or "apparel")
    baihuo = board == "baihuo"
    windows = {
        "昨天": style.get("sales1"),
        "近3天": style.get("sales3"),
        "近7天": style.get("sales7"),
        "7~14天": style.get("salesPrev7"),
        "周环比": style.get("wowRatio"),
        "近15天": style.get("sales15"),
        "近30天": style.get("sales30"),
        "近45天": style.get("sales45"),
        "近60天": style.get("sales60"),
    }
    if baihuo:
        windows.pop("近45天", None)
        windows.pop("近60天", None)
        windows["近14天"] = style.get("sales14")
        windows["线上7天"] = style.get("sales7Online")
        windows["线下7天"] = style.get("sales7Offline")
        windows["线上15天"] = style.get("sales15Online")
        windows["线下15天"] = style.get("sales15Offline")
        windows["线上30天"] = style.get("sales30Online")
        windows["线下30天"] = style.get("sales30Offline")
    payload = {
        "数据时点": snapshot.get("computedAt") or "",
        "款式编码": "" if baihuo else (style.get("styleId") or ""),
        "商品编码": (style.get("styleId") or "") if baihuo else "",
        "品名": "" if baihuo else (style.get("name") or ""),
        "商品名称": (style.get("name") or "") if baihuo else "",
        "品类线": style.get("categoryLine") or "",
        "SKU数": style.get("skuCount"),
        "品类": style.get("category") or "",
        "销量窗口_不含当天": windows,
        "库存": {
            "实际库存": style.get("qty"),
            "订单占有": style.get("occupy"),
            "采购在途": style.get("inbound"),
            "进货仓库存": style.get("inQty") or 0,
            "总库存": style.get("onHand"),
        },
        "进货仓说明": (
            "进货仓不进总库存公式。有数说明货已到进货仓或未上架，不要按补货建议再下一单。"
            if float(style.get("inQty") or 0) > 0 else ""
        ),
        "周转天数": style.get("turnoverDays"),
        "缺货预警": style.get("stockout"),
        "补货建议": style.get("replenishQty"),
        "建议下单": style.get("orderQty"),
        "起订量": style.get("moq"),
        "标签": list(style.get("labels") or []),
        "款年": style.get("year") or "",
        "资料售价": style.get("salePrice"),
        "资料成本": style.get("costPrice"),
        "研判要点": evidence_pack(style, board=board),
        "备注": style.get("remark") or "",
        "表内公式说明": (
            (
                "补货建议=日均×30−总库存，建议下单只是向上取整。"
                if baihuo else
                "补货建议=日均×60−总库存，建议下单只是向上取整。"
            )
            + "这是算术结果不是采购结论。"
            + (
                "你必须表态跟/减/少补/不补，禁止把这两个数字抄进建议行。"
                if baihuo else
                "你必须表态跟/减/只补断码/不补，禁止把这两个数字抄进建议行。"
            )
        ),
    }
    if not baihuo:
        payload["断码SKU数"] = style.get("brokenSkus")
        payload["缺码SKU数"] = style.get("shortSkus")
    if baihuo:
        payload["三窗折月"] = baihuo_window_rates(
            style.get("sales7"), style.get("sales15"), style.get("sales30"),
        )
        payload["月销量"] = style.get("monthlySales") or monthly_sales_baihuo(
            style.get("sales7"), style.get("sales15"), style.get("sales30"),
        )
        payload["日均"] = style.get("dailyAvg")
        payload["日均公式"] = "月销量=(7天×4 + 15天×2 + 30天)/3；日均=月销量/30"
        payload["近30天逐日出库_旧到新_最后一位是昨天"] = style.get("salesDaily") or []
        payload["出库店铺"] = style.get("saleShops") or []
        payload["出库店铺说明"] = "镜像出库对上的真实店铺，按近30天件数排序；未对上标「(未对上店铺)」"
        payload["日均口径"] = "三窗折月"
    else:
        payload["资料季节"] = style.get("season") or ""
        payload["穿着季节"] = infer_wear_season(style)
        payload["近30天逐日出库_旧到新_最后一位是昨天"] = style.get("salesDaily") or []
        payload["日均_四窗口加权"] = style.get("dailyAvg")
        payload["日均口径"] = "四窗口加权"
    return payload


SUMMER_NAME_TOKENS = ("冰丝", "凉感", "冰爽", "夏季", "薄")
WINTER_NAME_TOKENS = ("加绒", "棉服", "棉衣", "羽绒", "鹅绒")
CLOTHING_LINES = ("服装-非通勤裤", "通勤裤")


def evidence_pack(style: dict, *, board: str = "apparel") -> dict:
    """从结果表已有数字抽出可引用的形态，不引入外部数据。"""
    if str(board or "") == "baihuo":
        return evidence_pack_baihuo(style)
    daily = [float(value or 0) for value in (style.get("salesDaily") or [])]
    total30 = sum(daily) or float(style.get("sales30") or 0)
    peak = max(daily) if daily else 0.0
    peak_share = round(peak / total30, 3) if total30 > 0 else None
    sku_count = int(style.get("skuCount") or 0)
    broken = int(style.get("brokenSkus") or 0)
    on_hand = float(style.get("onHand") or 0)
    inbound = float(style.get("inbound") or 0)
    avg = float(style.get("dailyAvg") or 0)
    sales7 = float(style.get("sales7") or 0)
    wow = style.get("wowRatio")
    if peak_share is not None and peak_share >= 0.35 and peak >= 3:
        shape = "脉冲"
    elif sales7 <= 0 and total30 > 0:
        shape = "近7天停销"
    elif wow is not None and float(wow) <= -0.2:
        shape = "周环比走弱"
    elif wow is not None and float(wow) >= 0.2:
        shape = "周环比走强"
    else:
        shape = "平稳"
    qty_tight = on_hand <= 0 or (avg > 0 and on_hand < avg * 7)
    code_tight = bool(sku_count) and broken >= max(1, round(sku_count * 0.4))
    if qty_tight and code_tight:
        stock_kind = "量码都紧"
    elif code_tight:
        stock_kind = "主要缺码"
    elif qty_tight:
        stock_kind = "主要缺量"
    else:
        stock_kind = "库存够"
    sale = float(style.get("salePrice") or 0)
    cost = float(style.get("costPrice") or 0)
    margin = round((sale - cost) / sale, 3) if sale > 0 and cost > 0 else None
    qty = float(style.get("qty") or 0)
    occupy = float(style.get("occupy") or 0)
    missing = int(style.get("missingInventory") or 0)
    return {
        "近30天形态": shape,
        "近30天零销天数": sum(1 for value in daily if value <= 0),
        "峰值日出库": peak,
        "峰值占近30天": peak_share,
        "断码占比": round(broken / sku_count, 2) if sku_count else None,
        "库存结构": stock_kind,
        "在途可撑天数_按日均": round(inbound / avg, 1) if avg > 0 and inbound > 0 else None,
        "订单占有偏高": occupy > 0 and qty > 0 and occupy >= qty * 0.5,
        "无库存记录SKU数": missing if missing else 0,
        "资料毛利率": margin,
        "标签": list(style.get("labels") or []),
        "款年": style.get("year") or "",
    }


def evidence_pack_baihuo(style: dict) -> dict:
    """百货看 7/15/30 和脉冲，不把近7天停销当成主形态。"""
    daily = [float(value or 0) for value in (style.get("salesDaily") or [])]
    total = sum(daily) or float(style.get("sales30") or 0)
    peak = max(daily) if daily else 0.0
    peak_share = round(peak / total, 3) if total > 0 else None
    rates = baihuo_window_rates(
        style.get("sales7"), style.get("sales15"), style.get("sales30"),
    )
    week = rates["7天"]
    month = rates["30天"]
    if week >= 3 and month > 0 and week >= month * 0.7:
        shape = "短窗脉冲"
    elif week <= 0 and month > 0:
        shape = "慢动销"
    elif peak_share is not None and peak_share >= 0.35 and peak >= 3:
        shape = "脉冲"
    else:
        shape = "平稳"
    on_hand = float(style.get("onHand") or 0)
    inbound = float(style.get("inbound") or 0)
    avg = float(style.get("dailyAvg") or 0)
    qty = float(style.get("qty") or 0)
    if qty < 1:
        stock_kind = "没货"
    elif on_hand <= 0 or (avg > 0 and on_hand < avg * 7):
        stock_kind = "库存紧"
    else:
        stock_kind = "库存够"
    sale = float(style.get("salePrice") or 0)
    cost = float(style.get("costPrice") or 0)
    margin = round((sale - cost) / sale, 3) if sale > 0 and cost > 0 else None
    occupy = float(style.get("occupy") or 0)
    missing = int(style.get("missingInventory") or 0)
    return {
        "近30天形态": shape,
        "近30天零销天数": sum(1 for value in daily if value <= 0),
        "三窗折月": rates,
        "峰值日出库": peak,
        "峰值占近30天": peak_share,
        "库存结构": stock_kind,
        "在途可撑天数_按日均": round(inbound / avg, 1) if avg > 0 and inbound > 0 else None,
        "订单占有偏高": occupy > 0 and qty > 0 and occupy >= qty * 0.5,
        "无库存记录": missing if missing else 0,
        "资料毛利率": margin,
        "标签": list(style.get("labels") or []),
        "款年": style.get("year") or "",
    }


def infer_wear_season(style: dict) -> str:
    """资料季节为主；品名加绒/冰爽可把未填或四季往冬夏修正。"""
    name = str(style.get("name") or "")
    raw = str(style.get("season") or "").strip()
    if any(token in name for token in WINTER_NAME_TOKENS):
        return "冬季"
    if any(token in name for token in SUMMER_NAME_TOKENS):
        return "夏季"
    if raw in ("夏季", "冬季", "春秋", "四季"):
        return raw
    return raw or "未填"


def season_stage(wear: str, month: int) -> str:
    """本款相对当前日历月的阶段。只服务分析，不改补货公式。"""
    if wear == "夏季":
        if month in (3, 4):
            return "前置备货"
        if month in (5, 6, 7):
            return "当季主力"
        if month in (8, 9):
            return "尾季清货"
        return "淡季"
    if wear == "冬季":
        if month in (6, 7, 8):
            return "前置备货"
        if month in (9, 10):
            return "起量"
        if month in (11, 12, 1):
            return "当季主力"
        if month in (2, 3):
            return "尾季清货"
        return "淡季"
    if wear == "春秋":
        if month in (2, 3, 8, 9):
            return "当季主力"
        if month in (4, 5, 10, 11):
            return "尾季/换季"
        return "淡季"
    if wear == "四季":
        return "四季款"
    return "季节未维护"


def category_peers_payload(snapshot: dict, style: dict, *, limit: int = 5) -> dict:
    try:
        width = max(1, min(int(limit or 5), 8))
    except (TypeError, ValueError):
        width = 5
    line = style.get("categoryLine") or ""
    mine = style.get("styleId") or ""
    mine_season = infer_wear_season(style)
    mine_cat = str(style.get("category") or "")
    clothing = line in CLOTHING_LINES or "服装" in line

    baihuo = str(snapshot.get("board") or "") == "baihuo"

    def peer_key(item: dict):
        same_cat = 0 if mine_cat and item.get("category") == mine_cat else 1
        if baihuo:
            return (same_cat, -float(item.get("sales30") or 0))
        return (same_cat, -float(item.get("sales7") or 0))

    same, other = [], []
    for item in snapshot.get("styles") or []:
        if item.get("styleId") == mine or item.get("categoryLine") != line:
            continue
        if clothing and mine_season in ("夏季", "冬季", "春秋"):
            if infer_wear_season(item) == mine_season:
                same.append(item)
            else:
                other.append(item)
        else:
            same.append(item)
    same.sort(key=peer_key)
    other.sort(key=peer_key)
    picked = list(same[:width])
    if len(picked) < width:
        picked.extend(other[: width - len(picked)])
    rows = []
    same_ids = {item.get("styleId") for item in same}
    for item in picked:
        if baihuo:
            rows.append({
                "商品编码": item.get("styleId"),
                "商品名称": item.get("name"),
                "品类": item.get("category") or "",
                "近7天": item.get("sales7"),
                "近15天": item.get("sales15"),
                "近30天": item.get("sales30"),
                "线上7天": item.get("sales7Online"),
                "线下7天": item.get("sales7Offline"),
                "线上15天": item.get("sales15Online"),
                "线下15天": item.get("sales15Offline"),
                "线上30天": item.get("sales30Online"),
                "线下30天": item.get("sales30Offline"),
                "周转天数": item.get("turnoverDays"),
                "缺货": item.get("stockout"),
                "对照": "同品类" if mine_cat and item.get("category") == mine_cat else "同品类线",
            })
        else:
            rows.append({
                "款式编码": item.get("styleId"),
                "品名": item.get("name"),
                "品类": item.get("category") or "",
                "季节": infer_wear_season(item),
                "近7天": item.get("sales7"),
                "7~14天": item.get("salesPrev7"),
                "周环比": item.get("wowRatio"),
                "周转天数": item.get("turnoverDays"),
                "缺货": item.get("stockout"),
                "对照": "同季节" if item.get("styleId") in same_ids else "不同季节，只作参考",
            })
    if baihuo:
        return {
            "品类线": line,
            "对照款": rows,
            "本款品类": mine_cat,
            "说明": "自营百货按近30天出库排序；对照看7/15/30天线下占比，不是本款销量",
        }
    return {
        "品类线": line,
        "本款季节": mine_season,
        "对照款": rows,
        "本款品类": mine_cat,
        "说明": "服装优先同季节、同品类；对照款不是本款销量，也不是同面料",
    }


def season_notes_payload(style: dict, *, today: date | None = None) -> dict:
    day = today or business_today()
    line = str(style.get("categoryLine") or "")
    category = str(style.get("category") or "")
    listed = str(style.get("season") or "").strip()
    wear = infer_wear_season(style)
    stage = season_stage(wear, day.month)
    notes = [
        f"资料季节「{listed or '未填'}」，按品名修正后按「{wear}」看，当前是{stage}。",
    ]
    if stage == "前置备货":
        notes.append("近7天低是正常的，不要按日均外推到旺季总量，更不要当成滞销。")
    elif stage == "尾季清货":
        notes.append("优先清货和断码；近7天走弱或远弱于同季节对照时，不要跟表上补货建议加量。")
    elif stage == "淡季":
        notes.append("淡季近7天不能代表下季；缺货也先观望，除非明确是下季前置。")
    elif stage == "当季主力":
        notes.append("看断码和同季节对照；表上补货建议可以参考，仍要对照同季节款。")
    elif stage == "起量":
        notes.append("开始放量，断码值得补；不要拿上一季日均压这一季。")
    elif stage == "四季款":
        notes.append("季节不是主因，看断码和周环比。")
    labels = [str(item) for item in (style.get("labels") or [])]
    if any("新品" in item for item in labels):
        notes.append("带「新品」标签：近7天低是铺货观察期，不能当成滞销清货。")
    year = str(style.get("year") or "")
    current_yy = str(day.year)[-2:]
    if year.isdigit() and year < current_yy and stage in ("尾季清货", "淡季"):
        notes.append(f"款年{year}是旧年款，{stage}时优先消化余量，不要按日均再铺。")
    if category:
        notes.append(f"品类是{category}，对照时同品类同季节优先于品类线里的爆款。")
    missing = int(style.get("missingInventory") or 0)
    if missing:
        notes.append(f"{missing}个SKU无库存记录按0计，断码可能偏严，复核时先看这些码。")
    if "鞋" in line:
        notes.append("鞋类优先看断码；单日脉冲常见于活动，日均被拉高时要复核。")
    return {
        "月份": day.month,
        "品类线": line,
        "品类": category,
        "资料季节": listed,
        "穿着季节": wear,
        "阶段": stage,
        "notes": notes,
        "来源": "商品资料季节 + 日历 + 品名关键词，不是网页搜索",
    }


GIFT_NAME_TOKENS = ("礼盒", "礼品", "纪念", "文创", "定制", "伴手礼", "会议", "钥匙扣", "徽章")


def baihuo_notes_payload(style: dict, *, today: date | None = None) -> dict:
    """百货备忘：慢动销 / 脉冲 / 礼品，不用服装季节。"""
    day = today or business_today()
    name = str(style.get("name") or "")
    category = str(style.get("category") or "")
    line = str(style.get("categoryLine") or "")
    rates = baihuo_window_rates(
        style.get("sales7"), style.get("sales15"), style.get("sales30"),
    )
    avg = daily_avg_baihuo(
        style.get("sales7"), style.get("sales15"), style.get("sales30"),
    )
    notes = [
        "日均是 (7天×4 + 15天×2 + 30天)/3 得到月销量，再除以 30。",
    ]
    if rates["7天"] >= 3 and rates["30天"] > 0 and rates["7天"] >= rates["30天"] * 0.7:
        notes.append("近 7 天已经接近或超过 30 天总量的七成，多半是一批出完，不要按 7 天铺 30 天。")
    elif rates["7天"] <= 0 and rates["30天"] > 0:
        notes.append("近 7 天是 0、30 天有量，是慢动销，不能当成滞销清货。")
    elif avg <= 0:
        notes.append("7/15/30 天都折不出日均，先观望，不要为了填表去补。")
    labels = [str(item) for item in (style.get("labels") or [])]
    if any("新品" in item for item in labels):
        notes.append("带「新品」标签：长窗低是铺货观察期，不能当成滞销。")
    if any(token in name or token in category for token in GIFT_NAME_TOKENS):
        notes.append("品名/分类像礼品或文创：常见很久不走、忽然一批，优先少补或观望。")
    year = str(style.get("year") or "")
    current_yy = str(day.year)[-2:]
    if year.isdigit() and year < current_yy:
        notes.append(f"款年{year}是旧年款，优先消化余量，不要按单日预测再铺。")
    missing = int(style.get("missingInventory") or 0)
    if missing:
        notes.append(f"{missing}条无库存记录按0件计，复核时先对一下库存。")
    inbound = float(style.get("inbound") or 0)
    if avg > 0 and inbound > 0 and inbound >= avg * 30:
        notes.append("在途已能撑过约 30 天，短窗再热也先等这批到。")
    offline30 = float(style.get("sales30Offline") or 0)
    online30 = float(style.get("sales30Online") or 0)
    sales30 = offline30 + online30
    if sales30 > 0 and offline30 >= sales30 * 0.6:
        notes.append(
            "近30天线下占比高。线下（含内部店铺、渠道 KA）常一批出完就不走，不要按这30天外推补货。"
        )
    elif sales30 > 0 and offline30 > 0 and online30 > 0:
        notes.append("近30天线上线下都有量，分开看：线下不稳，跟线上节奏。")
    shops = style.get("saleShops") or []
    if shops:
        top = shops[0] if isinstance(shops[0], dict) else {}
        shop_name = str(top.get("shopName") or "")
        group = str(top.get("groupName") or "")
        if shop_name:
            extra = f"（{group}）" if group else ""
            notes.append(f"近30天出库最多的店是{shop_name}{extra}，以结果表店铺清单为准，不要另编来源。")
    in_qty = float(style.get("inQty") or 0)
    if in_qty > 0:
        notes.append(
            f"进货仓已有{in_qty:g}，货到了还没上主仓，总库存公式不含它，不要按补货建议再下一单。"
        )
    if category:
        notes.append(f"品类是{category}，对照时同品类优先于品类线里的爆款。")
    return {
        "月份": day.month,
        "品类线": line,
        "品类": category,
        "三窗折月": rates,
        "日均": avg,
        "notes": notes,
        "来源": "结果表 7/15/30 天出库 + 品名/标签，不是网页搜索",
    }


def _tool_name(call: dict) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    return str((function or {}).get("name") or call.get("name") or "")


def _tool_arguments(call: dict) -> dict:
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    raw = (function or {}).get("arguments") or call.get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dispatch_tool(name: str, arguments: dict, *, snapshot: dict, today: date) -> dict:
    style_id = str((arguments or {}).get("styleId") or "").strip()
    style = find_style(snapshot, style_id)
    if style is None:
        return {"error": "该款式不在当前结果表里"}
    if name == "spu_style_sales":
        return style_sales_payload(style, snapshot)
    if name == "spu_category_peers":
        return category_peers_payload(snapshot, style, limit=(arguments or {}).get("limit"))
    if name == "spu_season_notes":
        return season_notes_payload(style, today=today)
    if name == "spu_baihuo_notes":
        return baihuo_notes_payload(style, today=today)
    return {"error": f"未知工具：{name}"}


def run_style_analysis(
    style_id: str,
    *,
    snapshot: dict,
    llm,
    today: date | None = None,
    root=None,
    force: bool = False,
) -> dict:
    """有当天缓存且未 force 则直接返回；否则跑工具循环并写入缓存。"""
    style_id = str(style_id or "").strip()
    day = today or business_today()
    cached = load_cached_analysis(style_id, today=day, root=root)
    if cached and not cached.get("stale") and not force:
        return cached
    style = find_style(snapshot, style_id)
    if style is None:
        raise ValueError("该款式不在当前结果表里")
    if llm is None or not getattr(llm, "configured", False):
        raise RuntimeError("采购助手未启用，无法生成分析")

    board = str(snapshot.get("board") or "apparel")
    baihuo = board == "baihuo"
    required = BAIHUO_REQUIRED_TOOLS if baihuo else REQUIRED_TOOLS
    tools = BAIHUO_TOOL_SCHEMAS if baihuo else TOOL_SCHEMAS
    prompt = BAIHUO_SYSTEM_PROMPT if baihuo else SYSTEM_PROMPT
    user = (
        f"分析自营百货款式 {style_id}。读完三份工具后，给出跟/减/少补/不补的判断，"
        "建议必须写成「建议：X。因为①…②…」。不要用断码缺码或鞋服季节话术，不要复述补货建议。"
        if baihuo else
        f"分析款式 {style_id}。读完三份工具后，给出跟/减/只补断码/不补的判断，"
        "建议必须写成「建议：X。因为①…②…」，不要复述表上已经算好的补货建议。"
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user},
    ]
    used: set[str] = set()
    text = ""
    for _step in range(MAX_STEPS):
        missing = [name for name in required if name not in used]
        choice = (
            {"type": "function", "function": {"name": missing[0]}}
            if missing else "auto"
        )
        answer = llm.chat(messages, tools=tools, tool_choice=choice)
        calls = answer.get("tool_calls") or []
        if calls:
            messages.append({
                "role": "assistant",
                "content": answer.get("content") or "",
                "tool_calls": calls,
            })
            for call in calls:
                name = _tool_name(call)
                result = dispatch_tool(
                    name, _tool_arguments(call), snapshot=snapshot, today=day,
                )
                if name in required:
                    used.add(name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or name or "tool"),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            continue
        text = str(answer.get("content") or "").strip()
        if missing:
            name = missing[0]
            result = dispatch_tool(
                name, {"styleId": style_id}, snapshot=snapshot, today=day,
            )
            used.add(name)
            messages.append({
                "role": "system",
                "content": f"{name} 结果如下，读完再下判断，不要复述补货建议。\n"
                + json.dumps(result, ensure_ascii=False),
            })
            continue
        if text:
            break
    if not text:
        raise RuntimeError("模型没有返回分析内容")
    stamp = business_now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "styleId": style_id,
        "board": board,
        "day": day.isoformat(),
        "analyzedAt": stamp,
        "analysis": text,
        "model": str(getattr(llm, "model", "") or ""),
    }
    save_cached_analysis(style_id, row, root=root)
    return {
        "ok": True,
        "styleId": style_id,
        "analysis": text,
        "analyzedAt": stamp,
        "day": day.isoformat(),
        "model": row["model"],
        "cached": False,
        "stale": False,
    }
