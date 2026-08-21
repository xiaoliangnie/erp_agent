# -*- coding: utf-8 -*-
"""抖音换鞋垫定时任务：09:30–18:30 每小时一轮，也可手动触发。

自动跑不经 pending_action，不等人确认。先发【开始执行】清单，写完再发总结。
ERP 被代发或上一批鞋垫占用时本轮跳过，等下一轮 tick，不排队空等。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..business_time import business_now
from .insole import (
    build_insole_log, execute_insole_orders, format_insole_idle,
    format_insole_result, format_insole_start, load_reserved_insole_orders,
    load_written_insole_orders, locate_insole_orders, persist_insole_success,
)
from ..order_source import OrderSourceError
from ..paths import DATA_DIR, ROOT, resolve_repo_path


logger = logging.getLogger(__name__)

DEFAULT_START = (9, 30)
DEFAULT_END = (18, 30)
DEFAULT_SHOP = "抖音"
STATE_KEEP_DAYS = 3
SCHEDULE_DONE_KINDS = (
    "insole_schedule_idle", "insole_schedule_done", "insole_schedule_start",
)


def parse_hhmm(value, default=DEFAULT_START) -> tuple[int, int]:
    try:
        hour, minute = str(value or "").split(":", 1)
        return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
    except (TypeError, ValueError):
        return default


def slot_at(now: datetime, *, start=DEFAULT_START, end=DEFAULT_END,
            interval_minutes: int = 60) -> str | None:
    """当前分钟落在哪一档。两端都含；未开窗返回 None。"""
    interval = max(1, int(interval_minutes))
    start_min = start[0] * 60 + start[1]
    end_min = end[0] * 60 + end[1]
    current = now.hour * 60 + now.minute
    if current < start_min or current > end_min:
        return None
    index = (current - start_min) // interval
    slot_min = start_min + index * interval
    if slot_min > end_min:
        return None
    return f"{now.date().isoformat()} {slot_min // 60:02d}:{slot_min % 60:02d}"


class DouyinInsoleScheduler:
    """进程内后台线程。tick 只跑当前档；手动走 `run_once`。"""

    def __init__(
        self,
        *,
        setting,
        env_path: str,
        runtime,
        root=None,
        enabled: bool = False,
        start_time: str = "09:30",
        end_time: str = "18:30",
        interval_minutes: int = 60,
        poll_seconds: int = 30,
        shop: str = DEFAULT_SHOP,
        sender=None,
        audit=None,
        directory=None,
        conversation_id: str = "",
        oto_buyers: str = "安安",
        oto_user_ids: str = "",
        mirror=None,
    ):
        self.setting = setting
        self.env_path = env_path
        self.runtime = runtime
        self.root = root if root is not None else ROOT
        self.enabled = bool(enabled)
        self.start_clock = parse_hhmm(start_time, DEFAULT_START)
        self.end_clock = parse_hhmm(end_time, DEFAULT_END)
        self.interval_minutes = max(1, int(interval_minutes))
        self.poll_seconds = max(5, int(poll_seconds))
        self.shop = str(shop or DEFAULT_SHOP).strip() or DEFAULT_SHOP
        self.sender = sender
        self.audit = audit
        self.directory = directory
        self.conversation_id = str(conversation_id or "").strip()
        self.oto_buyers = _split_names(oto_buyers if oto_buyers is not None else "安安")
        self.oto_user_ids = _split_names(oto_user_ids)
        self.mirror = mirror
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self.done_slots: set[str] = set()
        self.last_slot = ""
        self.last_error = ""
        self.last_run = ""
        self._load_state()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "busy": self._busy.locked(),
            "shop": self.shop,
            "startTime": f"{self.start_clock[0]:02d}:{self.start_clock[1]:02d}",
            "endTime": f"{self.end_clock[0]:02d}:{self.end_clock[1]:02d}",
            "intervalMinutes": self.interval_minutes,
            "lastSlot": self.last_slot,
            "lastRun": self.last_run,
            "lastError": self.last_error,
            "dingtalk": bool(self._can_send()),
            "otoBuyers": list(self.oto_buyers),
        }

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._recover_done_slots()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="insole-schedule", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Insole scheduler tick failed")

    def tick(self, *, now: datetime | None = None) -> dict:
        current = now or business_now()
        slot = slot_at(
            current, start=self.start_clock, end=self.end_clock,
            interval_minutes=self.interval_minutes,
        )
        if slot is None:
            return {"skipped": True, "reason": "不在执行窗口"}
        if slot in self.done_slots:
            return {"skipped": True, "reason": "本档已跑过", "slot": slot}
        if self._busy.locked():
            return {"skipped": True, "reason": "上一轮还在跑", "slot": slot}
        return self.run_once(trigger="schedule", slot=slot, operator="scheduler")

    def run_once(self, *, trigger: str = "manual", slot: str = "",
                 operator: str = "manual", notify: bool = True) -> dict:
        """定位抖音待处理单 → 发清单 → 写入 → 发总结。占用 ERP 时不排队。"""
        if not self._busy.acquire(blocking=False):
            return {
                "skipped": True, "reason": "上一轮抖音鞋垫还在跑",
                "trigger": trigger, "slot": slot,
            }
        started = time.monotonic()
        held = False
        try:
            located = self._locate()
            processable = list(located.get("processable") or [])
            if not processable:
                text = format_insole_idle(located, slot=slot, trigger=trigger)
                if notify:
                    self._notify(text, kind="insole_schedule_idle", slot=slot, trigger=trigger)
                self._mark_done(slot, trigger)
                self.last_error = ""
                return {
                    "ok": True, "empty": True, "trigger": trigger, "slot": slot,
                    "processableCount": 0, "reply": text, "startText": text,
                }
            if not self._try_erp():
                reason = "ERP 正被代发或上一批鞋垫占用"
                if trigger == "manual":
                    self.last_error = reason
                    return {
                        "failed": True, "reason": reason, "trigger": trigger, "slot": slot,
                        "reply": reason + "，请稍后再触发。",
                    }
                return {"skipped": True, "reason": reason, "trigger": trigger, "slot": slot}
            held = True
            start_text = format_insole_start(located, slot=slot, trigger=trigger)
            if notify:
                self._notify(start_text, kind="insole_schedule_start", slot=slot, trigger=trigger)
            executed = execute_insole_orders(self.runtime, processable)
            log = build_insole_log(processable, executed)
            persist_insole_success(
                log, env_path=self.env_path, root=self.root, mirror=self.mirror,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result = {
                "okCount": executed.get("okCount") or 0,
                "skippedCount": executed.get("skippedCount") or 0,
                "failedCount": executed.get("failedCount") or 0,
                "attempted": executed.get("attempted") or len(processable),
                "elapsedMs": elapsed_ms,
                "prepareMs": executed.get("prepareMs"),
                "writeMs": executed.get("writeMs"),
                "readMs": executed.get("readMs"),
                "beforeMs": executed.get("beforeMs"),
                "afterMs": executed.get("afterMs"),
                "oIds": [row.get("o_id") for row in processable],
                "failed": executed.get("failed") or [],
                "log": log,
                "headline": self._headline(trigger, slot),
            }
            done_text = format_insole_result(result, elapsed_ms=elapsed_ms)
            if notify:
                self._notify(done_text, kind="insole_schedule_done", slot=slot, trigger=trigger)
            self._mark_done(slot, trigger)
            self.last_error = ""
            return {
                "ok": True, "empty": False, "trigger": trigger, "slot": slot,
                "processableCount": len(processable),
                "startText": start_text, "doneText": done_text,
                "reply": f"{start_text}\n\n{done_text}",
                "result": result,
            }
        except (OrderSourceError, ValueError) as exc:
            self.last_error = str(exc)
            logger.warning("抖音鞋垫定时任务失败：%s", exc)
            return {
                "failed": True, "reason": str(exc), "trigger": trigger, "slot": slot,
                "reply": f"【任务失败】抖音换鞋垫：{exc}",
            }
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("抖音鞋垫定时任务失败")
            text = f"【任务失败】抖音换鞋垫：{exc}"
            if notify:
                self._notify(text, kind="insole_schedule_failed", slot=slot, trigger=trigger)
            return {
                "failed": True, "reason": self.last_error, "trigger": trigger, "slot": slot,
                "reply": text,
            }
        finally:
            if held:
                self._release_erp()
            self._busy.release()

    def _headline(self, trigger: str, slot: str) -> str:
        if trigger == "manual":
            return "手动触发抖音换鞋垫"
        if slot:
            clock = slot.split(" ")[-1] if slot else ""
            return f"定时抖音换鞋垫 {clock or slot}"
        return "定时抖音换鞋垫"

    def _locate(self) -> dict:
        written = load_written_insole_orders(self.setting, root=self.root)
        db = resolve_repo_path(
            self.setting("AGENT_DATABASE_PATH", "files/data/agent.sqlite3"),
            root=self.root,
        )
        reserved = load_reserved_insole_orders(db)
        return locate_insole_orders(
            self.setting, self.env_path, shop=self.shop,
            written=written, reserved=reserved, root=self.root,
        )

    def _try_erp(self) -> bool:
        if self.runtime is None:
            raise ValueError("ERP Digital Worker 未装配")
        locker = getattr(self.runtime, "try_exclusive", None)
        if callable(locker):
            return bool(locker())
        return True

    def _release_erp(self) -> None:
        release = getattr(self.runtime, "release_exclusive", None)
        if callable(release):
            release()

    def _state_path(self) -> Path:
        if self.root is not None and Path(self.root).resolve() != ROOT.resolve():
            return Path(self.root) / "insole_schedule_state.json"
        return DATA_DIR / "insole_schedule_state.json"

    def _fresh_slots(self, slots) -> set[str]:
        cutoff = (business_now() - timedelta(days=STATE_KEEP_DAYS)).date()
        kept = set()
        for raw in slots or []:
            slot = str(raw or "").strip()
            if not slot:
                continue
            try:
                day = datetime.strptime(slot.split(" ", 1)[0], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                kept.add(slot)
        return kept

    def _load_state(self) -> None:
        path = self._state_path()
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        self.done_slots = self._fresh_slots(payload.get("doneSlots") or [])
        self.last_slot = str(payload.get("lastSlot") or self.last_slot or "")
        self.last_run = str(payload.get("lastRun") or self.last_run or "")

    def _save_state(self) -> None:
        path = self._state_path()
        payload = {
            "doneSlots": sorted(self._fresh_slots(self.done_slots)),
            "lastSlot": self.last_slot,
            "lastRun": self.last_run,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(prefix=".insole-schedule-", dir=str(path.parent))
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(payload, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                os.replace(temp_name, path)
            except BaseException:
                Path(temp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("鞋垫定时档位落盘失败：%s", exc)

    def _recover_done_slots(self) -> None:
        """重启后从本机档位文件和已发出的定时通知收回已跑过的档。"""
        self._load_state()
        audit = self.audit
        getter = getattr(audit, "list_deliveries", None) if audit is not None else None
        if not callable(getter):
            return
        recovered = set(self.done_slots)
        for kind in SCHEDULE_DONE_KINDS:
            try:
                rows = getter(kind=kind, status="sent", limit=200)
            except Exception:
                logger.exception("回收鞋垫定时档位失败：%s", kind)
                continue
            for row in rows or []:
                detail = row.get("detail") if isinstance(row, dict) else {}
                if not isinstance(detail, dict):
                    detail = {}
                slot = str(detail.get("slot") or "").strip()
                if slot:
                    recovered.add(slot)
        fresh = self._fresh_slots(recovered)
        if fresh != self.done_slots:
            self.done_slots = fresh
            self._save_state()

    def _mark_done(self, slot: str, trigger: str) -> None:
        if trigger == "schedule" and slot:
            self.done_slots.add(slot)
            self.last_slot = slot
            self.last_run = business_now().isoformat(timespec="seconds")
            self._save_state()
            return
        self.last_run = business_now().isoformat(timespec="seconds")

    def _can_send(self) -> bool:
        sender = self.sender
        conversation_id = self.conversation_id or getattr(sender, "group_conversation_id", "")
        return bool(
            sender
            and (getattr(sender, "app_ready", False) or getattr(sender, "webhook_ready", False))
            and conversation_id
        )

    def _oto_targets(self) -> list[str]:
        extra = [item for item in self.oto_user_ids if item]
        if self.directory is None or not self.oto_buyers:
            return extra
        resolve = getattr(self.directory, "resolve", None)
        if resolve is None:
            return extra
        result = resolve(self.oto_buyers)
        ids = list(extra)
        for user_id in result.get("userIds") or []:
            if user_id and user_id not in ids:
                ids.append(user_id)
        return ids

    def _notify(self, text: str, *, kind: str, slot: str, trigger: str) -> None:
        if not text or not self._can_send():
            return
        sender = self.sender
        conversation_id = self.conversation_id or getattr(sender, "group_conversation_id", "") or "group"
        title = "抖音换鞋垫"
        try:
            if getattr(sender, "app_ready", False) and hasattr(sender, "reply_text"):
                sender.reply_text(conversation_id=conversation_id, text=text)
            elif getattr(sender, "send_markdown", None):
                sender.send_markdown(title, text)
            if self.audit is not None:
                self.audit.record_delivery(
                    channel="dingtalk", target=conversation_id,
                    kind=kind, status="sent",
                    detail={"slot": slot, "trigger": trigger, "text": text[:500]},
                    idempotency_key=(
                        f"{kind}-{slot or trigger}-{hash(text) & 0xFFFFFFFF:x}"
                    ),
                )
        except Exception as exc:
            logger.exception("抖音鞋垫定时通知失败：%s", exc)


def _split_names(value) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    import re
    return tuple(part for part in re.split(r"[,，、;；\s]+", text) if part)
