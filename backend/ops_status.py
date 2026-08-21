# -*- coding: utf-8 -*-
"""运行状态面板：数据源摘要 + 调度下次执行。不触发看板重算。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .business_time import BUSINESS_TIMEZONE, business_now
from .exchange.insole_scheduler import parse_hhmm, slot_at


def parse_stamp(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text)
        elif len(text) == 10:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        elif len(text) == 16:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
        else:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUSINESS_TIMEZONE)
    return parsed.astimezone(BUSINESS_TIMEZONE)


def parse_clock(value, default=(0, 0)) -> tuple[int, int]:
    try:
        hour, minute = str(value or "").split(":", 1)
        return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
    except (TypeError, ValueError):
        return default


def ran_on_date(last_run, today: str) -> bool:
    text = str(last_run or "").strip()
    if not text:
        return False
    return text.startswith(today) or text[:10] == today


def due_seconds(now: datetime, when: datetime | None) -> int | None:
    if when is None:
        return None
    return int((when - now).total_seconds())


def next_daily(now: datetime, hhmm: str, *, last_run="", ran_today: bool | None = None):
    """返回 (下次时刻, 今日是否已跑)。过了点还没跑则下次=现在。"""
    hour, minute = parse_clock(hhmm, (0, 0))
    today = now.date().isoformat()
    done = ran_on_date(last_run, today) if ran_today is None else bool(ran_today)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if done:
        return target + timedelta(days=1), True
    if now >= target:
        return now, False
    return target, False


def next_interval(now: datetime, last_run, interval_seconds: int):
    last = parse_stamp(last_run)
    seconds = max(1, int(interval_seconds or 1))
    if last is None:
        return now
    nxt = last + timedelta(seconds=seconds)
    return now if nxt <= now else nxt


def next_insole(now: datetime, *, start="09:30", end="18:30",
                interval_minutes: int = 60, last_slot=""):
    start_clock = parse_hhmm(start)
    end_clock = parse_hhmm(end)
    interval = max(1, int(interval_minutes or 60))
    current = slot_at(
        now, start=start_clock, end=end_clock, interval_minutes=interval,
    )
    last = str(last_slot or "").strip()
    if current and current != last:
        when = parse_stamp(current) or now
        return when, False
    cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(36 * 60):
        slot = slot_at(
            cursor, start=start_clock, end=end_clock, interval_minutes=interval,
        )
        if slot and slot != last:
            return parse_stamp(slot) or cursor, True
        cursor += timedelta(minutes=1)
    return None, bool(last)


def source_card(meta: dict | None, sync: dict | None, state: dict | None) -> dict:
    meta = meta or {}
    sync = sync or {}
    state = state or {}
    return {
        "name": state.get("source") or meta.get("source") or "供应链 API 本地实时镜像",
        "queriedAt": meta.get("databaseNow") or sync.get("databaseNow") or "",
        "syncedAt": sync.get("syncedAt") or meta.get("syncedAt") or "",
        "syncLagMinutes": sync.get("syncLagMinutes", meta.get("syncLagMinutes")),
        "fresh": sync.get("fresh", meta.get("fresh")),
        "year": state.get("year") or meta.get("selectedYear") or "",
        "minDate": meta.get("minDate") or "",
        "maxDate": meta.get("maxDate") or "",
        "orders": meta.get("orders"),
        "rows": meta.get("rows"),
        "warning": state.get("warning") or meta.get("warning") or "",
        "sourceStatus": sync.get("sourceStatus") or meta.get("sourceStatus") or "",
        "today": meta.get("today") or "",
    }


def _stamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def schedule_row(
    *,
    item_id: str,
    label: str,
    group: str,
    enabled: bool,
    running: bool,
    last_run: str = "",
    next_at: datetime | None = None,
    ran_today: bool | None = None,
    last_error: str = "",
    detail: str = "",
    now: datetime | None = None,
) -> dict:
    current = now or business_now()
    error = str(last_error or "").strip()
    if not enabled:
        state = "off"
    elif error:
        state = "error"
    elif next_at is not None and next_at <= current and ran_today is False:
        state = "late"
    elif next_at is not None and next_at <= current:
        state = "due"
    else:
        state = "ok"
    due = due_seconds(current, next_at)
    return {
        "id": item_id,
        "label": label,
        "group": group,
        "enabled": bool(enabled),
        "running": bool(running),
        "state": state,
        "detail": detail,
        "lastRun": last_run or "",
        "nextRun": _stamp(next_at),
        "dueInSeconds": due,
        "ranToday": ran_today,
        "lastError": error,
    }


def build_schedules(now: datetime, items: list[dict[str, Any]]) -> list[dict]:
    """items 已是 schedule_row 产出，按到点时间排序（关闭的放后面）。"""
    rows = list(items)

    def sort_key(row: dict):
        if not row.get("enabled"):
            return (2, 10**12, row["label"])
        due = row.get("dueInSeconds")
        if due is None:
            return (1, 10**12, row["label"])
        return (0, due, row["label"])

    rows.sort(key=sort_key)
    return rows
