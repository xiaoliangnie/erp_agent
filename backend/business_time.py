# -*- coding: utf-8 -*-
"""采购业务统一使用中国时区，避免部署主机时区影响日期和页面时间。"""
from datetime import datetime
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def business_now() -> datetime:
    return datetime.now(BUSINESS_TIMEZONE)


def business_today():
    return business_now().date()


def business_timestamp(timespec="minutes") -> str:
    return business_now().isoformat(timespec=timespec)
