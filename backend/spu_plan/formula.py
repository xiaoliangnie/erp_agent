# -*- coding: utf-8 -*-
"""总表公式。鞋服日均四窗口；百货三窗折月再除 30。不读采购周期。"""
from __future__ import annotations

import math

SKIP_WARNING_TAGS = ("清仓品", "淘汰品", "有升级")
# 总表「是否淘汰」列：标签 → 填写词（对齐员工手填习惯：淘汰品→淘汰、有升级→有升级）
OBSOLETE_LABEL_WORDS = (("淘汰品", "淘汰"), ("清仓品", "清仓"), ("有升级", "有升级"))
TURNOVER_ALERT_DAYS = 30
REPLENISH_COVER_DAYS = 60
BAIHUO_REPLENISH_COVER_DAYS = 30
# 百货：月销量 = (7天×4 + 15天×2 + 30天) / 3，日均 = 月销量 / 30
BAIHUO_AVG_WINDOWS = (7, 15, 30)


def _qty(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def daily_avg(sales_1, sales_3, sales_7, sales_15) -> float:
    """日均 = ROUND((昨天 + 3天/3 + 7天/7 + 15天/15) / 4, 1)。"""
    try:
        value = (
            _qty(sales_1)
            + _qty(sales_3) / 3.0
            + _qty(sales_7) / 7.0
            + _qty(sales_15) / 15.0
        ) / 4.0
    except ZeroDivisionError:
        return 0.0
    return round(value, 1)


def monthly_sales_baihuo(sales_7, sales_15, sales_30) -> float:
    """月销量 = ROUND((7天×4 + 15天×2 + 30天) / 3, 1)。"""
    return round((_qty(sales_7) * 4.0 + _qty(sales_15) * 2.0 + _qty(sales_30)) / 3.0, 1)


def daily_avg_baihuo(sales_7, sales_15, sales_30) -> float:
    """日均 = ROUND(月销量 / 30, 1)。月销量见 monthly_sales_baihuo。"""
    return round(monthly_sales_baihuo(sales_7, sales_15, sales_30) / 30.0, 1)


def baihuo_window_rates(sales_7, sales_15, sales_30) -> dict:
    """三窗件数和折合月销，给模型对照脉冲，不另算结论。"""
    monthly = monthly_sales_baihuo(sales_7, sales_15, sales_30)
    return {
        "7天": round(_qty(sales_7), 1),
        "15天": round(_qty(sales_15), 1),
        "30天": round(_qty(sales_30), 1),
        "折合月销_7天": round(_qty(sales_7) * 4.0, 1),
        "折合月销_15天": round(_qty(sales_15) * 2.0, 1),
        "折合月销_30天": round(_qty(sales_30), 1),
        "月销量": monthly,
        "日均": round(monthly / 30.0, 1),
    }


def sku_code_status(qty, sales_7: float) -> str:
    """数据源 A：库存<1 断码；否则库存<7天销量 缺码；否则充足。"""
    stock = _qty(qty)
    if stock < 1:
        return "断码"
    if stock < _qty(sales_7):
        return "缺码"
    return "充足"


def style_stock(qty, occupy, inbound) -> float:
    """总库存 = 实际 − 占有 + 在途。"""
    return _qty(qty) - _qty(occupy) + _qty(inbound)


def obsolete_label(labels) -> str:
    """「是否淘汰」从标签解析；命中多个用顿号连接，没命中留空。"""
    texts = [str(tag) for tag in (labels or [])]
    hits = []
    for tag, word in OBSOLETE_LABEL_WORDS:
        if any(tag in text for text in texts):
            hits.append(word)
    return "、".join(hits)


def order_qty(replenish, moq=None):
    """建议下单：补货建议向上取整。有起订量按起订量的倍数，否则按 10 的倍数。"""
    if replenish is None:
        return None
    raw = _qty(replenish)
    if raw <= 0:
        return 0
    step = _qty(moq)
    if step <= 0:
        step = 10.0
    return int(math.ceil(raw / step) * step)


def style_year(style_id: str) -> str:
    """年份 = MID(款式编码, 3, 2)，如 BH24… → 24。"""
    text = str(style_id or "").strip()
    return text[2:4] if len(text) >= 4 else ""


def replenish_qty(daily_avg_value, on_hand, cover_days=None) -> int:
    """补货建议 = ROUNDUP(日均 × 覆盖天数 − 总库存, 0)。鞋服 60 天，百货 30 天。"""
    try:
        days = float(REPLENISH_COVER_DAYS if cover_days is None else cover_days)
    except (TypeError, ValueError):
        days = float(REPLENISH_COVER_DAYS)
    if days <= 0:
        days = float(REPLENISH_COVER_DAYS)
    raw = _qty(daily_avg_value) * days - _qty(on_hand)
    if raw >= 0:
        return int(math.ceil(raw))
    return int(math.floor(raw))


def style_warning(*, qty, occupy, inbound, sales_1, sales_3, sales_7, sales_15,
                  skip_warning: bool, daily_avg_value=None) -> dict:
    """款式预警。带清仓/淘汰/有升级只出数，不标缺货。"""
    avg = (
        daily_avg(sales_1, sales_3, sales_7, sales_15)
        if daily_avg_value is None
        else round(_qty(daily_avg_value), 1)
    )
    on_hand = style_stock(qty, occupy, inbound)
    if avg <= 0:
        turnover = None
        stockout = False
    else:
        turnover = on_hand / avg
        stockout = (not skip_warning) and turnover < TURNOVER_ALERT_DAYS
    return {
        "dailyAvg": avg,
        "onHand": on_hand,
        "turnoverDays": None if turnover is None else round(turnover, 2),
        "stockout": stockout,
        "skipWarning": bool(skip_warning),
    }
