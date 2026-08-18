# -*- coding: utf-8 -*-
"""代发订单：Digital Worker 抓取 + 导出 Excel。

取数复用换鞋垫同一套 ``DigitalRuntime``：``ERP_AI_*`` 登录，cookie 落
``files/data/secrets/erp-ai-state.json``，Playwright 串行执行。
淘系 / 拼多多必须把订单列表嵌在 epaas 外壳里，顶层才有解密 SDK。
"""

from .collect import prepare_dropship_list
from .export import export_today_dropship, public_export_result
from .scheduler import DailyDropshipScheduler, dropship_file_has_rows, dropship_row_count
from .workbook import (
    COLUMNS,
    SHEET1_COLUMNS,
    SHEET2_COLUMNS,
    dropship_filename,
    dropship_output_path,
    write_blank_dropship_template,
    write_dropship_workbook,
)

__all__ = [
    "COLUMNS",
    "SHEET1_COLUMNS",
    "SHEET2_COLUMNS",
    "DailyDropshipScheduler",
    "dropship_file_has_rows",
    "dropship_row_count",
    "dropship_filename",
    "dropship_output_path",
    "export_today_dropship",
    "prepare_dropship_list",
    "public_export_result",
    "write_blank_dropship_template",
    "write_dropship_workbook",
]
