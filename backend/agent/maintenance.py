# -*- coding: utf-8 -*-
"""审计保留与 outputs 清理。低频后台线程，骨架同催办调度。"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from pathlib import Path

from ..business_time import business_now, business_today
from .store import now


logger = logging.getLogger(__name__)


class MaintenanceScheduler:
    def __init__(self, *, store, root: Path, retention_days: int = 90,
                 output_days: int = 30, poll_seconds: int = 3600):
        self.store = store
        self.root = Path(root)
        self.retention_days = max(1, int(retention_days))
        self.output_days = max(1, int(output_days))
        self.poll_seconds = max(60, int(poll_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.enabled = False
        self.last_run = ""
        self.last_error = ""

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "lastRun": self.last_run,
            "lastError": self.last_error,
            "retentionDays": self.retention_days,
        }

    def start(self) -> None:
        self.enabled = True
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agent-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Maintenance tick failed")

    def tick(self, *, now_value=None) -> dict:
        try:
            result = self.run_once()
            self.last_run = business_today().isoformat()
            self.last_error = ""
            return result
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("Maintenance failed: %s", self.last_error)
            return {"failed": True, "reason": self.last_error}

    def run_once(self) -> dict:
        cutoff = (business_now() - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        forecast_cutoff = (business_now() - timedelta(days=30)).isoformat(timespec="seconds")
        deleted = {}
        with self.store.write() as conn:
            for table, column in (
                ("agent_messages", "created_at"),
                ("agent_runs", "started_at"),
                ("tool_executions", "created_at"),
                ("notification_deliveries", "created_at"),
            ):
                cursor = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = cursor.rowcount or 0
            cursor = conn.execute(
                "UPDATE forecast_runs SET output_json='{}' WHERE created_at < ? AND output_json <> '{}'",
                (forecast_cutoff,),
            )
            deleted["forecast_runs_cleared"] = cursor.rowcount or 0
        removed_files = 0
        for folder in ("generated", "agent", "quality"):
            removed_files += _purge_dir(self.root / "outputs" / folder, self.output_days)
        return {"ok": True, "deleted": deleted, "files": removed_files, "ranAt": now()}


def _purge_dir(directory: Path, days: int) -> int:
    if not directory.exists():
        return 0
    cutoff = business_now().timestamp() - days * 86400
    removed = 0
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
