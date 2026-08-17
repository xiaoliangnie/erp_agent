# -*- coding: utf-8 -*-
"""健康巡检评估：假 health JSON + 上一轮状态 → 告警项。不发 HTTP、不发钉钉。"""
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.business_time import BUSINESS_TIMEZONE
from backend.health_watch import (
    Issue,
    collect_issues,
    evaluate_health,
    load_state,
    render_alert,
    save_state,
)
from backend.logging_setup import ShanghaiFormatter, resolve_log_path


def _health(**overrides):
    payload = {
        "ok": True,
        "database": "connected",
        "rows": 10,
        "syncedAt": "2026-08-13 12:00:00",
        "syncLagMinutes": 1,
        "realtimeMirror": {"enabled": True, "running": False, "lastError": ""},
        "dingtalk": {
            "stream": {
                "enabled": True, "configured": True, "running": True,
                "lastError": "", "restartCount": 0,
            },
            "reminder": {
                "enabled": True, "running": True, "lastRun": "2026-08-13", "lastError": "",
            },
        },
    }
    payload.update(overrides)
    return payload


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=BUSINESS_TIMEZONE)


class HealthWatchEvaluateTests(unittest.TestCase):
    def test_healthy_payload_has_no_issues(self):
        result = evaluate_health(_health(), previous={}, now=NOW)
        self.assertEqual([], result.issues)
        self.assertFalse(result.should_alert)
        self.assertEqual(0, result.state["restartCount"])

    def test_ok_false_alerts(self):
        result = evaluate_health(
            _health(ok=False, database="unavailable", error="OperationalError"),
            previous={}, now=NOW,
        )
        self.assertEqual(["ok_false"], [item.code for item in result.issues])
        self.assertTrue(result.should_alert)
        self.assertIn("OperationalError", result.issues[0].text)

    def test_unreachable_without_payload(self):
        result = evaluate_health(None, fetch_error="无法连接：Connection refused", previous={}, now=NOW)
        self.assertEqual(["unreachable"], [item.code for item in result.issues])
        self.assertTrue(result.should_alert)

    def test_unreachable_keeps_previous_restart_count(self):
        result = evaluate_health(
            None, fetch_error="timeout", previous={"restartCount": 4}, now=NOW,
        )
        self.assertEqual(4, result.state["restartCount"])

    def test_mirror_lag_over_threshold(self):
        issues = collect_issues(_health(syncLagMinutes=40), previous={}, lag_minutes=15)
        self.assertEqual(["mirror_lag"], [item.code for item in issues])
        self.assertIn("40", issues[0].text)

    def test_mirror_lag_skipped_when_disabled(self):
        issues = collect_issues(
            _health(syncLagMinutes=90, realtimeMirror={"enabled": False, "lastError": ""}),
            previous={}, lag_minutes=15,
        )
        self.assertEqual([], issues)

    def test_mirror_missing_synced_at_when_enabled(self):
        issues = collect_issues(
            _health(syncedAt="", syncLagMinutes=None),
            previous={}, lag_minutes=15,
        )
        self.assertEqual(["mirror_lag"], [item.code for item in issues])

    def test_mirror_last_error(self):
        issues = collect_issues(
            _health(realtimeMirror={"enabled": True, "lastError": "ProxyAPIError: 429"}),
            previous={},
        )
        self.assertEqual(["mirror_error"], [item.code for item in issues])

    def test_restart_count_first_observation_does_not_alert(self):
        payload = _health()
        payload["dingtalk"]["stream"]["restartCount"] = 3
        result = evaluate_health(payload, previous={}, now=NOW)
        self.assertEqual([], result.issues)
        self.assertEqual(3, result.state["restartCount"])

    def test_restart_count_growth_alerts(self):
        payload = _health()
        payload["dingtalk"]["stream"]["restartCount"] = 5
        result = evaluate_health(payload, previous={"restartCount": 3}, now=NOW)
        self.assertEqual(["stream_restart"], [item.code for item in result.issues])
        self.assertIn("3 → 5", result.issues[0].text)
        self.assertEqual(5, result.state["restartCount"])

    def test_same_restart_count_does_not_alert(self):
        payload = _health()
        payload["dingtalk"]["stream"]["restartCount"] = 5
        issues = collect_issues(payload, previous={"restartCount": 5})
        self.assertEqual([], issues)

    def test_reminder_last_error(self):
        payload = _health()
        payload["dingtalk"]["reminder"]["lastError"] = "DingTalkError: 超时"
        issues = collect_issues(payload, previous={})
        self.assertEqual(["reminder_error"], [item.code for item in issues])

    def test_reminder_dead_when_enabled_but_not_running(self):
        payload = _health()
        payload["dingtalk"]["reminder"] = {"enabled": True, "running": False, "lastError": ""}
        issues = collect_issues(payload, previous={})
        self.assertEqual(["reminder_dead"], [item.code for item in issues])

    def test_reminder_not_started_is_not_dead(self):
        payload = _health()
        payload["dingtalk"]["reminder"] = {"enabled": False, "running": False, "lastError": ""}
        issues = collect_issues(payload, previous={})
        self.assertEqual([], issues)

    def test_same_fingerprint_is_rate_limited(self):
        payload = _health(ok=False, database="unavailable", error="OperationalError")
        first = evaluate_health(payload, previous={}, now=NOW)
        self.assertTrue(first.should_alert)
        second = evaluate_health(payload, previous=first.state, now=NOW + timedelta(minutes=10))
        self.assertTrue(second.issues)
        self.assertFalse(second.should_alert)
        third = evaluate_health(
            payload, previous=second.state, now=NOW + timedelta(minutes=61),
        )
        self.assertTrue(third.should_alert)

    def test_changed_fingerprint_alerts_immediately(self):
        first_payload = _health(ok=False, database="unavailable", error="OperationalError")
        first = evaluate_health(first_payload, previous={}, now=NOW)
        second_payload = _health()
        second_payload["dingtalk"]["reminder"]["lastError"] = "催办失败"
        second = evaluate_health(second_payload, previous=first.state, now=NOW + timedelta(minutes=1))
        self.assertEqual(["reminder_error"], [item.code for item in second.issues])
        self.assertTrue(second.should_alert)

    def test_recovery_clears_fingerprint(self):
        payload = _health(ok=False, error="OperationalError")
        first = evaluate_health(payload, previous={}, now=NOW)
        recovered = evaluate_health(_health(), previous=first.state, now=NOW + timedelta(minutes=1))
        self.assertEqual([], recovered.issues)
        self.assertFalse(recovered.should_alert)
        self.assertEqual("", recovered.state["lastIssueFingerprint"])
        self.assertEqual("", recovered.state["lastAlertAt"])

    def test_render_alert_lists_codes_in_chinese(self):
        text = render_alert(
            [Issue("ok_false", "数据库 unavailable"), Issue("mirror_lag", "镜像滞后 40 分钟")],
            url="http://127.0.0.1:8777/api/health",
        )
        self.assertIn("服务不健康", text)
        self.assertIn("镜像同步滞后", text)
        self.assertIn("127.0.0.1:8777", text)

    def test_state_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "health_watch_state.json"
        save_state(path, {"restartCount": 2, "lastIssueFingerprint": "ok_false:x"})
        loaded = load_state(path)
        self.assertEqual(2, loaded["restartCount"])
        self.assertEqual({}, load_state(Path(tmp.name) / "missing.json"))
        tmp.cleanup()


class LoggingSetupTests(unittest.TestCase):
    def test_formatter_uses_shanghai_not_utc(self):
        formatter = ShanghaiFormatter("%(asctime)s")
        record = logging.LogRecord("t", logging.INFO, __file__, 0, "hi", None, None)
        record.created = datetime(2026, 8, 13, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual("2026-08-14 00:00:00", formatter.formatTime(record))

    def test_relative_log_path_resolves_under_repo_root(self):
        path = resolve_log_path("data/app.log")
        self.assertTrue(str(path).replace("\\", "/").endswith("files/data/app.log"))
        self.assertIsNone(resolve_log_path(""))
        self.assertIsNone(resolve_log_path(None))


if __name__ == "__main__":
    unittest.main()
