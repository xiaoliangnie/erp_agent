# -*- coding: utf-8 -*-
"""鞋服 SPU 结果表每日重算：默认 09:00 写 spu_style_snapshot 和当日总表 xlsx。

生产计划表也在同一时点重生成（配置了 `SPU_PLAN_SOURCE_XLSX` 才跑）：
每天只刷新库存进度（现势库存/在途/当月净销量/判定）；订货量沿用上次月底上传。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from ..business_time import business_now
from .production_plan import build_production_plan
from .service import BOARDS, build_style_alerts, save_style_snapshot
from .workbook import style_workbook_path, write_style_workbook

logger = logging.getLogger(__name__)


def parse_hhmm(value, default=(9, 0)) -> tuple[int, int]:
    try:
        hour, minute = str(value or "").split(":", 1)
        return max(0, min(int(hour), 23)), max(0, min(int(minute), 59))
    except (TypeError, ValueError):
        return default


class DailySpuSnapshotScheduler:
    """进程内后台线程；与 /api/spu/refresh 共用一把锁，同时只有一次重算。"""

    def __init__(self, *, env_path: str, enabled: bool = True, run_time: str = "09:00",
                 poll_seconds: int = 60, lock: threading.Lock | None = None,
                 plan_source: str = ""):
        self.env_path = env_path
        self.enabled = bool(enabled)
        self.run_time = parse_hhmm(run_time)
        self.poll_seconds = max(10, int(poll_seconds))
        self.lock = lock or threading.Lock()
        self.plan_source = str(plan_source or "").strip()
        self.sender = None
        self.audit = None
        self.alert_enabled = True
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_run = ""
        self.last_error = ""
        self.plan_last_run = ""
        self.plan_last_error = ""
        self.plan_last_alert = ""

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "runTime": f"{self.run_time[0]:02d}:{self.run_time[1]:02d}",
            "lastRun": self.last_run,
            "lastError": self.last_error,
            "planSource": self.plan_source,
            "planLastRun": self.plan_last_run,
            "planLastError": self.plan_last_error,
            "planLastAlert": self.plan_last_alert,
        }

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="spu-snapshot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("SPU snapshot scheduler tick failed")

    def tick(self, *, now: datetime | None = None) -> dict:
        current = now or business_now()
        today = current.date().isoformat()
        if (current.hour, current.minute) < self.run_time:
            return {"skipped": True, "reason": "未到执行时间", "today": today}
        if self.last_run == today:
            return {"skipped": True, "reason": "今日已重算", "today": today}
        result = self.run_once(today=today)
        return result

    def run_once(self, *, today: str = "") -> dict:
        if not self.lock.acquire(blocking=False):
            return {"skipped": True, "reason": "已有一次重算在跑", "today": today}
        try:
            written = 0
            xlsx_paths = []
            for board in BOARDS:
                result = build_style_alerts(self.env_path, board=board)
                written += save_style_snapshot(self.env_path, result, board=board)
                xlsx = style_workbook_path(board=board)
                try:
                    write_style_workbook(result, xlsx)
                    xlsx_paths.append(str(xlsx))
                except PermissionError:
                    # 员工把当日表开着时写不进去；结果表已更新，明早再写文件。
                    logger.warning("SPU 总表 xlsx 被占用，跳过写文件：%s", xlsx)
            self.last_run = today or business_now().date().isoformat()
            self.last_error = ""
            logger.info("SPU 结果表每日重算完成：%s 款，xlsx %s", written, xlsx_paths)
            self._run_plan()
            return {
                "ok": True, "styles": written, "xlsx": xlsx_paths[0] if xlsx_paths else "",
                "xlsxPaths": xlsx_paths, "today": self.last_run,
            }
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("SPU 结果表每日重算失败")
            return {"failed": True, "reason": self.last_error, "today": today}
        finally:
            self.lock.release()

    def _run_plan(self) -> None:
        """生产计划表每日跟库存刷新；需求数沿用上次月底上传的订货表。"""
        if not self.plan_source:
            return
        if not Path(self.plan_source).exists():
            self.plan_last_error = f"找不到员工订货表：{self.plan_source}"
            logger.warning(self.plan_last_error)
            return
        try:
            result = build_production_plan(self.plan_source, self.env_path)
            self.plan_last_run = business_now().date().isoformat()
            self.plan_last_error = ""
            logger.info(
                "生产计划表每日重生成完成：%s 款，xlsx %s", result["styles"], result["xlsx"],
            )
            self._push_workbook(result, force=False, operator="scheduler")
        except PermissionError:
            self.plan_last_error = "生产计划表 xlsx 被占用，本轮跳过写文件"
            logger.warning(self.plan_last_error)
        except Exception as exc:
            self.plan_last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("生产计划表每日重生成失败")

    def _push_workbook(self, result: dict, *, force: bool, operator: str) -> None:
        if not self.alert_enabled:
            return
        from .alerts import push_plan_workbook

        try:
            pushed = push_plan_workbook(
                result.get("xlsx") or "",
                sender=self.sender, audit=self.audit,
                force=force, operator=operator,
                today=result.get("today") or "",
            )
            self.plan_last_alert = pushed.get("reason") or (
                f"已发送 {Path(pushed.get('path') or '').name}" if pushed.get("sent") else ""
            )
        except Exception as exc:
            self.plan_last_alert = f"{type(exc).__name__}: {exc}"
            logger.exception("生产计划表钉钉发送失败")
