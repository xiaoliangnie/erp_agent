# -*- coding: utf-8 -*-
"""ERP Digital Worker：专用账号 + 类型化命令，不让模型开浏览器。"""

from .config import ALLOWED_COMMANDS, load_digital_worker, load_worker_secrets
from .errors import ErpError, ErpUnknownResult
from .loop import DigitalWorkerLoop
from .runtime import DigitalRuntime
from .session import playwright_available

__all__ = [
    "ALLOWED_COMMANDS",
    "DigitalRuntime",
    "DigitalWorkerLoop",
    "ErpError",
    "ErpUnknownResult",
    "load_digital_worker",
    "load_worker_secrets",
    "playwright_available",
]
