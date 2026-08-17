# -*- coding: utf-8 -*-
"""品控问题台账：登记、查询、每日 Excel 日报。"""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta
from pathlib import Path

from ..business_time import business_today
from ..paths import local_dir
from .parse import parse_quality_command, parse_quality_fields
from .report import build_quality_workbook, quality_report_markdown
from .scheduler import DailyQualityReportScheduler
from .service import QualityError, QualityLedger, load_supplier_names


def report_link_sig(secret: str, compact_date: str) -> str:
    return hmac.new(
        str(secret).encode("utf-8"),
        str(compact_date).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def report_link_valid(secret: str, compact_date: str, sig: str, *, today=None) -> bool:
    if not secret or not sig or not compact_date:
        return False
    try:
        day = date.fromisoformat(f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:8]}")
    except (TypeError, ValueError):
        return False
    current = today or business_today()
    if abs((current - day).days) > 7:
        return False
    expected = report_link_sig(secret, compact_date)
    return hmac.compare_digest(expected, str(sig))


def purchase_order_exists(po_id: str, env_path: str) -> bool:
    from ..database import REALTIME_MAIN_TABLE, connect
    try:
        with connect(env_path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT po_id FROM `{REALTIME_MAIN_TABLE}` WHERE po_id = %s LIMIT 1",
                    (po_id,),
                )
                return bool(cursor.fetchone())
    except Exception:
        return False


def build_quality(*, setting, store, sender, root, env_path, audit, flag):
    """装配台账与日报调度。"""
    root = Path(root)
    enabled = flag(setting("QUALITY_LEDGER_ENABLED", "false"))
    ledger = QualityLedger(
        store,
        suppliers=load_supplier_names(root),
        lookup_po=lambda po_id: purchase_order_exists(po_id, env_path),
        enabled=enabled,
    )
    scheduler = DailyQualityReportScheduler(
        ledger=ledger,
        sender=sender,
        audit=audit,
        output_dir=local_dir("outputs", root=root) / "quality",
        send_time=setting("QUALITY_REPORT_TIME", "17:30"),
        empty_mode=setting("QUALITY_REPORT_EMPTY", "skip"),
        link_secret=setting("QUALITY_REPORT_LINK_SECRET", ""),
        public_base=setting("APP_BASE_URL", "http://127.0.0.1:8777"),
    )
    ledger.scheduler = scheduler
    return ledger, scheduler


__all__ = [
    "DailyQualityReportScheduler", "QualityError", "QualityLedger",
    "build_quality", "build_quality_workbook", "parse_quality_command",
    "parse_quality_fields", "quality_report_markdown", "report_link_sig",
    "report_link_valid",
]
