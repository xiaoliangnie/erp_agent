# -*- coding: utf-8 -*-
"""ERP Digital Worker 错误。未知写结果不得重试。"""


class ErpError(RuntimeError):
    """可回给运维的确定失败（尚未开始写，或页面未就绪）。"""


class ErpUnknownResult(ErpError):
    """写操作已发出但结果不确定。调用方不得重试，只能等人核对。"""
