"""Crisis alerts must push at most once per ticker per ET day, across restarts.

The legacy send_alert dedup only remembers a message for five minutes and only
after a channel actually accepted it; with no channel configured every restart
re-appended the same row to critical_alerts.jsonl.
"""
from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import notifications
import tests as test_pkg

ET = ZoneInfo("America/New_York")


class CrisisDailyDedupTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.dedupe_path = self.dir / "alert_dedupe.json"
        self.alerts_path = self.dir / "critical_alerts.jsonl"
        self._patches = [
            patch.object(notifications, "ALERT_DEDUPE_PATH", self.dedupe_path),
            patch.object(notifications, "ALERTS_PATH", self.alerts_path),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        # A reload test re-executes notifications.py and drops tests/__init__
        # redirections; point them back at the suite temp dir so later tests
        # cannot write live signals files.
        tests_dir = test_pkg.TMP_DIR
        notifications.ALERT_DEDUPE_PATH = tests_dir / "alert_dedupe.json"
        notifications.ALERTS_PATH = tests_dir / "critical_alerts.jsonl"
        notifications.DEDUP_PATH = tests_dir / "notify_dedup.json"
        self._tmp.cleanup()

    def test_same_ticker_same_day_pushes_once(self):
        sent = {"sent": ["test"], "skipped": [], "dedup": False}
        with patch.object(notifications, "send_alert", return_value=sent) as push:
            first = notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
            second = notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
        self.assertEqual(push.call_count, 1)
        self.assertFalse(first.get("dedup"))
        self.assertTrue(second.get("dedup"))
        self.assertEqual(second["skipped"], ["daily_dedup"])
        self.assertEqual(second["date"], datetime.now(ET).date().isoformat())

    def test_restart_reload_does_not_push_again(self):
        sent = {"sent": ["test"], "skipped": [], "dedup": False}
        with patch.object(notifications, "send_alert", return_value=sent) as first_push:
            notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
        self.assertEqual(first_push.call_count, 1)

        importlib.reload(notifications)
        notifications.ALERT_DEDUPE_PATH = self.dedupe_path
        notifications.ALERTS_PATH = self.alerts_path

        with patch.object(notifications, "send_alert", return_value=sent) as after_restart:
            result = notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
        after_restart.assert_not_called()
        self.assertEqual(result["skipped"], ["daily_dedup"])

    def test_different_ticker_or_day_still_pushes(self):
        sent = {"sent": ["test"], "skipped": [], "dedup": False}
        with patch.object(notifications, "send_alert", return_value=sent) as push:
            notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
            notifications.notify_crisis("US.LITE", -4.80, "crisis regime")
        self.assertEqual(push.call_count, 2)

        with patch.object(
            notifications,
            "_alert_day_key",
            side_effect=lambda event, level, day=None: f"{event}|2099-01-01|{level}",
        ), patch.object(notifications, "send_alert", return_value=sent) as next_day:
            notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
        self.assertEqual(next_day.call_count, 1)

    def test_escalation_level_is_not_blocked_by_lower_level(self):
        notifications._record_alert_sent("crisis:US.CBRS", "warning")
        sent = {"sent": ["test"], "skipped": [], "dedup": False}
        with patch.object(notifications, "send_alert", return_value=sent) as push:
            notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
        self.assertEqual(push.call_count, 1)

    def test_dedupe_file_survives_corruption(self):
        self.dedupe_path.write_text("{not json", encoding="utf-8")
        sent = {"sent": ["test"], "skipped": [], "dedup": False}
        with patch.object(notifications, "send_alert", return_value=sent) as push:
            notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
        self.assertEqual(push.call_count, 1)
        key = notifications._alert_day_key("crisis:US.CBRS", "crisis")
        data = json.loads(self.dedupe_path.read_text(encoding="utf-8"))
        self.assertIn(key, data)

    def test_no_channel_restart_writes_one_alert_row(self):
        with patch.object(notifications, "_cfg", return_value=""):
            notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
            notifications.notify_crisis("US.CBRS", -5.15, "crisis regime")
        rows = self.alerts_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["level"], "crisis")

    def test_key_uses_et_date(self):
        expected = (
            f"crisis:US.CBRS|{datetime.now(ET).date().isoformat()}|crisis"
        )
        self.assertEqual(
            notifications._alert_day_key("crisis:US.CBRS", "crisis"), expected
        )


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
