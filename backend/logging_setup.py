# -*- coding: utf-8 -*-
"""进程内统一日志：东八区时间 / 级别 / 模块。

只在 `server.py` / `app.main` 和巡检脚本里调用 `configure_logging`。模块里用
`logging.getLogger(__name__)` 即可；未配置时 ERROR 仍走 lastResort，测试不会写文件。
"""
from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .business_time import BUSINESS_TIMEZONE


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
ROOT = Path(__file__).resolve().parents[1]
_configured = False


class ShanghaiFormatter(logging.Formatter):
    """asctime 固定 Asia/Shanghai，不跟机器时区走。"""

    def formatTime(self, record, datefmt=None):
        moment = datetime.fromtimestamp(record.created, BUSINESS_TIMEZONE)
        return moment.strftime(datefmt or self.datefmt or DATE_FORMAT)


def resolve_log_path(log_file: str | None, *, root: Path | None = None) -> Path | None:
    text = str(log_file or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    return (root or ROOT) / path


def configure_logging(*, level: str | None = None, log_file: str | None = None,
                      stream: bool = True, force: bool = False) -> None:
    """给根 logger 挂 stderr 和可选文件。重复调用默认忽略。"""
    global _configured
    if _configured and not force:
        return
    numeric = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    formatter = ShanghaiFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
    if stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)
    path = resolve_log_path(log_file)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, encoding="utf-8", maxBytes=2_000_000, backupCount=5)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    _configured = True
