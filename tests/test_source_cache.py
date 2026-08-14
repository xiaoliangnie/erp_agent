# -*- coding: utf-8 -*-
"""source_cache 键必须是解析后的年份，垃圾 year 不能各占一份全年副本。"""
import unittest
from datetime import date
from unittest.mock import patch

import backend.app as app_mod


class ResolveSourceYearTests(unittest.TestCase):
    def test_junk_year_falls_back_to_current(self):
        years = ["2024", "2025", "2026"]
        self.assertEqual("2026", app_mod.resolve_source_year("0000", years, current_year="2026"))
        self.assertEqual("2026", app_mod.resolve_source_year("9999", years, current_year="2026"))
        self.assertEqual("2026", app_mod.resolve_source_year(None, years, current_year="2026"))
        self.assertEqual("2024", app_mod.resolve_source_year("2024", years, current_year="2026"))

    def test_missing_current_year_uses_first(self):
        self.assertEqual("2024", app_mod.resolve_source_year(None, ["2024", "2025"], current_year="2026"))

    def test_empty_years_raises(self):
        with self.assertRaisesRegex(RuntimeError, "没有有效年份"):
            app_mod.resolve_source_year("2026", [])


class TrimSourceCacheTests(unittest.TestCase):
    def test_drops_expired_before_oldest(self):
        cache = {
            "2023": {"expires": 1},
            "2024": {"expires": 100},
            "2025": {"expires": 200},
            "2026": {"expires": 300},
        }
        app_mod.trim_source_cache(cache, now=50, keep_key="2026", limit=3)
        self.assertEqual({"2024", "2025", "2026"}, set(cache))

    def test_evicts_oldest_when_all_fresh(self):
        cache = {
            "2023": {"expires": 100},
            "2024": {"expires": 200},
            "2025": {"expires": 300},
            "2026": {"expires": 400},
        }
        app_mod.trim_source_cache(cache, now=0, keep_key="2026", limit=3)
        self.assertEqual({"2024", "2025", "2026"}, set(cache))


class SourceCacheKeyTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(app_mod._cache)
        app_mod._cache.clear()

    def tearDown(self):
        app_mod._cache.clear()
        app_mod._cache.update(self._saved)

    def test_junk_years_share_resolved_key(self):
        rows_calls = []

        def fake_rows(year, _env):
            rows_calls.append(year)
            return []

        sync = {
            "fresh": True, "syncedAt": "2026-08-13", "syncLagMinutes": 1,
            "databaseNow": "", "sourceStatus": "",
        }
        with patch("backend.app.fetch_realtime_years", return_value=["2024", "2025", "2026"]), \
             patch("backend.app.fetch_realtime_purchase_rows", side_effect=fake_rows), \
             patch("backend.app.fetch_realtime_sync_state", return_value=sync), \
             patch("backend.app.business_today", return_value=date(2026, 8, 13)):
            app_mod.source_cache("0000")
            app_mod.source_cache("9999")
            app_mod.source_cache(None)
        self.assertEqual(["2026"], list(app_mod._cache.keys()))
        self.assertEqual(["2026"], rows_calls)


if __name__ == "__main__":
    unittest.main()
