# -*- coding: utf-8 -*-
"""运行状态：下次执行与数据源卡。全程离线。"""
import unittest
from datetime import datetime

from backend.business_time import BUSINESS_TIMEZONE
from backend.ops_status import next_daily, next_insole, next_interval, source_card


NOW = datetime(2026, 8, 21, 13, 54, tzinfo=BUSINESS_TIMEZONE)


class NextRunTests(unittest.TestCase):
    def test_daily_before_clock_waits_today(self):
        nxt, done = next_daily(NOW, "14:00", last_run="")
        self.assertFalse(done)
        self.assertEqual("2026-08-21 14:00:00", nxt.strftime("%Y-%m-%d %H:%M:%S"))

    def test_daily_after_clock_without_run_is_due(self):
        nxt, done = next_daily(NOW, "08:30", last_run="")
        self.assertFalse(done)
        self.assertEqual(NOW, nxt)

    def test_daily_already_ran_goes_tomorrow(self):
        nxt, done = next_daily(NOW, "08:30", last_run="2026-08-21")
        self.assertTrue(done)
        self.assertEqual("2026-08-22 08:30:00", nxt.strftime("%Y-%m-%d %H:%M:%S"))

    def test_interval_overdue_is_now(self):
        nxt = next_interval(NOW, "2026-08-21 13:50:00", 60)
        self.assertEqual(NOW, nxt)

    def test_insole_uses_current_open_slot_if_not_done(self):
        nxt, done = next_insole(
            NOW, start="09:30", end="18:30", interval_minutes=60,
            last_slot="2026-08-21 12:30",
        )
        self.assertFalse(done)
        self.assertEqual("2026-08-21 13:30:00", nxt.strftime("%Y-%m-%d %H:%M:%S"))


class SourceCardTests(unittest.TestCase):
    def test_prefers_dashboard_meta_and_state_warning(self):
        card = source_card(
            {
                "source": "供应链 API 本地实时镜像",
                "databaseNow": "2026-08-21 13:54:39",
                "syncedAt": "2026-08-21 13:53:33",
                "minDate": "2026-01-01",
                "maxDate": "2026-08-21",
                "orders": 2561,
                "rows": 26629,
            },
            {"syncedAt": "2026-08-21 13:53:33", "syncLagMinutes": 0, "fresh": True},
            {"source": "供应链 API 本地实时镜像", "year": "2026",
             "warning": "2026 年明细只读到 2026-08-21，之后下的采购单未纳入"},
        )
        self.assertEqual(2561, card["orders"])
        self.assertEqual(26629, card["rows"])
        self.assertIn("只读到", card["warning"])
        self.assertEqual("2026", card["year"])


if __name__ == "__main__":
    unittest.main()
