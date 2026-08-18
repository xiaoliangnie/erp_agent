# -*- coding: utf-8 -*-
"""评估 `/api/health`：库不可用、镜像滞后、Stream 重连增长、催办/品控/代发 lastError。

巡检脚本 `scripts/health_watch.py` 调这里；不要 import `backend.app`（会把整站装配起来）。
评估函数纯数据进纯数据出，离线测试不发 HTTP、不发钉钉。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .business_time import BUSINESS_TIMEZONE, business_now


DEFAULT_LAG_MINUTES = 15
DEFAULT_REPEAT_MINUTES = 60
DEFAULT_TIMEOUT_SECONDS = 10
ALERT_TITLE = "采购服务健康告警"


@dataclass(frozen=True)
class Issue:
    code: str
    text: str


@dataclass
class Evaluation:
    issues: list[Issue]
    should_alert: bool
    state: dict
    fingerprint: str = ""
    restart_count: int = 0


def parse_watch_time(value, fallback: datetime | None = None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return fallback
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BUSINESS_TIMEZONE)
    return moment.astimezone(BUSINESS_TIMEZONE)


def issue_fingerprint(issues: list[Issue]) -> str:
    return "|".join(f"{item.code}:{item.text}" for item in issues)


def collect_issues(payload: dict | None, *, fetch_error: str = "",
                   previous: dict | None = None, lag_minutes: int = DEFAULT_LAG_MINUTES) -> list[Issue]:
    """根据本轮 health JSON 和上一轮巡检状态列出告警项。"""
    previous = previous or {}
    issues: list[Issue] = []
    if payload is None:
        reason = (fetch_error or "无法读取 /api/health").strip()
        return [Issue("unreachable", reason)]

    if payload.get("ok") is False:
        database = str(payload.get("database") or "unavailable")
        error = str(payload.get("error") or "").strip()
        detail = f"{database}" + (f"（{error}）" if error else "")
        issues.append(Issue("ok_false", f"数据库 {detail}"))

    mirror = payload.get("realtimeMirror") or {}
    if mirror.get("enabled"):
        lag = payload.get("syncLagMinutes")
        if lag is None:
            synced_at = str(payload.get("syncedAt") or "").strip()
            if not synced_at:
                issues.append(Issue("mirror_lag", "尚无成功同步时间"))
        else:
            try:
                lag_value = int(lag)
            except (TypeError, ValueError):
                lag_value = None
            if lag_value is not None and lag_value > int(lag_minutes):
                issues.append(Issue(
                    "mirror_lag",
                    f"镜像滞后 {lag_value} 分钟（阈值 {int(lag_minutes)}）",
                ))
        last_error = str(mirror.get("lastError") or "").strip()
        if last_error:
            issues.append(Issue("mirror_error", last_error[:300]))

    stream = (payload.get("dingtalk") or {}).get("stream") or {}
    try:
        current = int(stream.get("restartCount") or 0)
    except (TypeError, ValueError):
        current = 0
    previous_count = previous.get("restartCount")
    if stream.get("enabled") and previous_count is not None:
        try:
            seen = int(previous_count)
        except (TypeError, ValueError):
            seen = 0
        if current > seen:
            issues.append(Issue("stream_restart", f"Stream restartCount {seen} → {current}"))

    reminder = (payload.get("dingtalk") or {}).get("reminder") or {}
    reminder_error = str(reminder.get("lastError") or "").strip()
    if reminder_error:
        issues.append(Issue("reminder_error", reminder_error[:300]))
    if reminder.get("enabled") and not reminder.get("running"):
        issues.append(Issue("reminder_dead", "催办调度已启用但线程未在运行"))

    quality = payload.get("quality") or {}
    quality_error = str(quality.get("lastError") or "").strip()
    if quality_error:
        issues.append(Issue("quality_error", quality_error[:300]))
    if quality.get("enabled") and not quality.get("running"):
        issues.append(Issue("quality_dead", "品控日报调度已启用但线程未在运行"))

    dropship = payload.get("dropship") or {}
    dropship_error = str(dropship.get("lastError") or "").strip()
    if dropship_error:
        issues.append(Issue("dropship_error", dropship_error[:300]))
    if dropship.get("enabled") and not dropship.get("running"):
        issues.append(Issue("dropship_dead", "代发调度已启用但线程未在运行"))
    return issues


def evaluate_health(payload: dict | None, *, fetch_error: str = "",
                    previous: dict | None = None, now: datetime | None = None,
                    lag_minutes: int = DEFAULT_LAG_MINUTES,
                    repeat_minutes: int = DEFAULT_REPEAT_MINUTES) -> Evaluation:
    previous = dict(previous or {})
    current = now or business_now()
    issues = collect_issues(
        payload, fetch_error=fetch_error, previous=previous, lag_minutes=lag_minutes,
    )
    fingerprint = issue_fingerprint(issues)
    stream = ((payload or {}).get("dingtalk") or {}).get("stream") or {}
    try:
        restart_count = int(stream.get("restartCount") or 0)
    except (TypeError, ValueError):
        restart_count = 0
    if payload is None:
        try:
            restart_count = int(previous.get("restartCount") or 0)
        except (TypeError, ValueError):
            restart_count = 0
    should_alert = False
    last_alert_at = previous.get("lastAlertAt") or ""
    if issues:
        previous_fp = str(previous.get("lastIssueFingerprint") or "")
        if fingerprint != previous_fp:
            should_alert = True
        else:
            last_alert = parse_watch_time(last_alert_at)
            elapsed = None if last_alert is None else (current - last_alert).total_seconds()
            should_alert = last_alert is None or elapsed >= int(repeat_minutes) * 60
        if should_alert:
            last_alert_at = current.isoformat(timespec="seconds")
    else:
        fingerprint = ""
    state = {
        "restartCount": restart_count,
        "lastIssueFingerprint": fingerprint,
        "lastAlertAt": last_alert_at if issues else "",
        "checkedAt": current.isoformat(timespec="seconds"),
    }
    return Evaluation(
        issues=issues,
        should_alert=should_alert,
        state=state,
        fingerprint=fingerprint,
        restart_count=restart_count,
    )


def render_alert(issues: list[Issue], *, url: str = "") -> str:
    lines = ["采购服务健康检查发现问题：", ""]
    labels = {
        "unreachable": "健康检查不可达",
        "ok_false": "服务不健康",
        "mirror_lag": "镜像同步滞后",
        "mirror_error": "镜像同步失败",
        "stream_restart": "钉钉 Stream 重连",
        "reminder_error": "交期催办失败",
        "reminder_dead": "交期催办调度停摆",
        "quality_error": "品控日报失败",
        "quality_dead": "品控日报调度停摆",
        "dropship_error": "代发导出失败",
        "dropship_dead": "代发调度停摆",
    }
    for item in issues:
        title = labels.get(item.code, item.code)
        lines.append(f"- **{title}**：{item.text}")
    if url:
        lines.extend(["", f"来源：`{url}`"])
    return "\n".join(lines)


def load_state(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def fetch_health(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> tuple[dict | None, str]:
    """GET /api/health。503 时仍尝试解析 JSON（库挂了也要把子系统状态带回来）。"""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            return None, f"HTTP {exc.code}"
        if isinstance(payload, dict):
            return payload, ""
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"无法连接：{exc.reason}"
    except TimeoutError:
        return None, "健康检查超时"
    try:
        payload = json.loads(body) if body else None
    except json.JSONDecodeError:
        return None, "健康检查返回的不是合法 JSON"
    if not isinstance(payload, dict):
        return None, "健康检查返回的不是对象"
    return payload, ""
