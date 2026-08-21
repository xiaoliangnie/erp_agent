# -*- coding: utf-8 -*-
"""生产计划表：当月缺口判定（表内公式）+ 钉钉只发 xlsx。

判定与表内「计划入库数」同一公式：当月需求 vs 期初库存/在途。
群里不发缺口清单，只发当日生产计划表文件。
"""
from __future__ import annotations

import logging
from datetime import date

from ..business_time import business_today

logger = logging.getLogger(__name__)

VERDICT_OK = "库存满足"
VERDICT_INBOUND = "及时入库"
VERDICT_REPLENISH = "需补货"
ALERT_LIMIT = 40


def month_verdict(open_qty, open_transit, demand) -> str:
    """复现 Excel：期初>需求→满足；期初+在途>需求→及时入库；否则需补货。无需求则空。"""
    if demand is None:
        return ""
    qty = float(open_qty or 0)
    transit = float(open_transit or 0)
    need = float(demand)
    if qty > need:
        return VERDICT_OK
    if qty + transit > need:
        return VERDICT_INBOUND
    return VERDICT_REPLENISH


def month_gap(open_qty, open_transit, demand):
    if demand is None:
        return None
    gap = float(demand) - float(open_qty or 0) - float(open_transit or 0)
    return gap if gap > 0 else 0.0


def collect_month_alerts(source: dict, live: dict | None, today: date) -> dict:
    """只收当月及时入库、需补货。返回可直接渲染/推送的结构。"""
    anchor = (today.year, today.month)
    live = live or {}
    replenish = []
    inbound = []
    for item in source.get("styles") or []:
        style = item.get("styleId") or ""
        if not style:
            continue
        demand = (source.get("demands") or {}).get(style, {}).get(anchor)
        if demand is None or float(demand) <= 0:
            continue
        open_qty, open_transit = (source.get("opening") or {}).get(style, (None, None))
        verdict = month_verdict(open_qty, open_transit, demand)
        if verdict not in (VERDICT_INBOUND, VERDICT_REPLENISH):
            continue
        live_row = live.get(style) or {}
        row = {
            "owner": item.get("owner") or "",
            "line": item.get("line") or "",
            "styleId": style,
            "name": item.get("name") or "",
            "demand": float(demand),
            "openQty": float(open_qty or 0),
            "openTransit": float(open_transit or 0),
            "gap": month_gap(open_qty, open_transit, demand) or 0.0,
            "verdict": verdict,
            "liveQty": float(live_row.get("qty") or 0),
            "monthOut": float((live_row.get("byMonth") or {}).get(anchor) or 0),
        }
        if verdict == VERDICT_REPLENISH:
            replenish.append(row)
        else:
            inbound.append(row)
    replenish.sort(key=lambda row: (-row["gap"], row["styleId"]))
    inbound.sort(key=lambda row: (-row["demand"], row["styleId"]))
    return {
        "today": today.isoformat(),
        "month": anchor[1],
        "replenish": replenish,
        "inbound": inbound,
    }


def _qty(value) -> str:
    number = float(value or 0)
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _owner_counts(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        name = row.get("owner") or "未填责任人"
        counts[name] = counts.get(name, 0) + 1
    return " · ".join(f"{name} {counts[name]}" for name in sorted(counts))


def _line(row: dict, *, show_gap: bool) -> str:
    owner = row.get("owner") or "未填"
    extra = f" 缺口 {_qty(row['gap'])}" if show_gap else ""
    return (
        f"- {owner} / {row.get('line') or ''} / `{row['styleId']}` "
        f"{row.get('name') or ''} 需求 {_qty(row['demand'])} "
        f"期初 {_qty(row['openQty'])} 在途 {_qty(row['openTransit'])}{extra}"
    )


def format_plan_alert_markdown(alerts: dict) -> str:
    month = alerts.get("month") or ""
    today = alerts.get("today") or ""
    replenish = list(alerts.get("replenish") or [])
    inbound = list(alerts.get("inbound") or [])
    lines = [
        f"### 生产计划 · {month}月缺口 {today}",
        "",
        "口径：当月需求 vs 期初库存/在途（与表内「计划入库数」同一公式）。库存满足的款不列。",
        "",
        f"**需补货 {len(replenish)} 款**（期初+在途仍不够，有缺口）",
    ]
    if replenish:
        owners = _owner_counts(replenish)
        if owners:
            lines.append(owners)
        shown = replenish[:ALERT_LIMIT]
        lines.extend(_line(row, show_gap=True) for row in shown)
        if len(replenish) > ALERT_LIMIT:
            lines.append(f"- …其余 {len(replenish) - ALERT_LIMIT} 款见生产计划表")
    else:
        lines.append("无")
    lines.extend(["", f"**及时入库 {len(inbound)} 款**（在途到仓才够，需入仓）"])
    if inbound:
        owners = _owner_counts(inbound)
        if owners:
            lines.append(owners)
        shown = inbound[:ALERT_LIMIT]
        lines.extend(_line(row, show_gap=False) for row in shown)
        if len(inbound) > ALERT_LIMIT:
            lines.append(f"- …其余 {len(inbound) - ALERT_LIMIT} 款见生产计划表")
    else:
        lines.append("无")
    return "\n".join(lines)


def daily_alert_key(today: str) -> str:
    return f"plan-xlsx-{today}"


def push_plan_workbook(
    path,
    *,
    sender,
    audit=None,
    force: bool = False,
    operator: str = "scheduler",
    today: str = "",
) -> dict:
    """群里只发生产计划表 xlsx，不发缺口清单。每日默认幂等。"""
    from pathlib import Path

    dest = Path(path)
    day = str(today or business_today().isoformat())
    detail = {"today": day, "operator": operator, "path": str(dest), "filename": dest.name}
    if not dest.exists() or dest.stat().st_size <= 0:
        return {"skipped": True, "reason": "找不到生产计划表", **detail}
    conversation_id = getattr(sender, "group_conversation_id", "") if sender else ""
    ready = bool(
        sender
        and getattr(sender, "configured", False)
        and getattr(sender, "app_ready", False)
        and conversation_id
        and hasattr(sender, "upload_media")
        and hasattr(sender, "send_file")
    )
    if not ready:
        return {"skipped": True, "reason": "钉钉未配置企业机器人或群会话", **detail}
    key = daily_alert_key(day)
    if not force and audit is not None and audit.has_successful_delivery(key):
        return {"skipped": True, "reason": "今日已发送生产计划表", **detail}
    send_key = key if not force else f"{key}-{operator}"
    if audit is not None:
        audit.release_unsuccessful_key(send_key)
    try:
        media = sender.upload_media(dest, filetype="file")
        response = sender.send_file(
            conversation_id, media["mediaId"], dest.name, file_type="xlsx",
        )
    except Exception as exc:
        if audit is not None:
            audit.record_delivery(
                channel="dingtalk", target=conversation_id or "group",
                kind="plan_xlsx", status="failed", detail=detail, error=str(exc),
                idempotency_key=audit.next_attempt_key(send_key)
                if hasattr(audit, "next_attempt_key") else None,
            )
        raise
    if audit is not None:
        audit.record_delivery(
            channel="dingtalk", target=conversation_id, kind="plan_xlsx",
            status="sent",
            detail={**detail, "channel": (response or {}).get("channel") or "app"},
            idempotency_key=send_key,
        )
    return {"sent": True, **detail}
