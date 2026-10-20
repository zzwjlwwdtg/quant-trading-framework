"""2026-10-20 audit P1-1/P1-2: 收盘报告触发 + 休市/提前收盘门禁.

收盘报告窗口 16:00-16:14 没有对应的 DAILY_WINDOWS_ET 窗口, _tick 必须在窗口
门禁之前判断报告; 报告只有在成功生成后才写持久化标记, 失败要能重试.
休市日不跑任何窗口/报告; 提前收盘日 13:00 后不跑 pre-close, 收盘报告改在 13:00.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import orchestrator

ET = ZoneInfo("America/New_York")


def _fixed(moment: datetime):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)
    return FixedDateTime


class ReportSchedule(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_path = self.tmp / "report_state.json"
        self._report_patch = patch.object(
            orchestrator, "_REPORT_STATE_PATH", self.state_path)
        self._report_patch.start()
        self._prev_report = dict(orchestrator._last_report_session)
        orchestrator._last_report_session.update({"open": None, "close": None})
        self._prev_windows = dict(orchestrator._last_window_run)
        for name in orchestrator._last_window_run:
            orchestrator._last_window_run[name] = None

    def tearDown(self):
        self._report_patch.stop()
        orchestrator._last_report_session.clear()
        orchestrator._last_report_session.update(self._prev_report)
        orchestrator._last_window_run.clear()
        orchestrator._last_window_run.update(self._prev_windows)

    def test_holiday_has_no_windows_or_reports(self):
        with patch.object(orchestrator, "datetime",
                          _fixed(datetime(2026, 11, 26, 10, 5, tzinfo=ET))):
            self.assertIsNone(orchestrator._in_daily_window())
            self.assertIsNone(orchestrator._report_trigger())
            self.assertEqual(orchestrator._session(), "closed(holiday)")

    def test_early_close_skips_pre_close_and_reports_at_1300(self):
        with patch.object(orchestrator, "datetime",
                          _fixed(datetime(2026, 11, 27, 12, 5, tzinfo=ET))):
            self.assertEqual(orchestrator._in_daily_window(), "midday")
            self.assertEqual(orchestrator._session(), "open")
        with patch.object(orchestrator, "datetime",
                          _fixed(datetime(2026, 11, 27, 13, 5, tzinfo=ET))):
            self.assertIsNone(orchestrator._in_daily_window())
            self.assertEqual(orchestrator._report_trigger(), "close")
            self.assertEqual(orchestrator._session(), "after-hours")
        with patch.object(orchestrator, "datetime",
                          _fixed(datetime(2026, 11, 27, 15, 50, tzinfo=ET))):
            self.assertIsNone(orchestrator._in_daily_window())

    def test_early_close_report_window_ends_at_1314(self):
        with patch.object(orchestrator, "datetime",
                          _fixed(datetime(2026, 11, 27, 13, 14, tzinfo=ET))):
            self.assertEqual(orchestrator._report_trigger(), "close")
        with patch.object(orchestrator, "datetime",
                          _fixed(datetime(2026, 11, 27, 13, 15, tzinfo=ET))):
            self.assertIsNone(orchestrator._report_trigger())

    def test_close_report_runs_once_outside_trading_window(self):
        moment = datetime(2026, 8, 3, 16, 1, tzinfo=ET)
        with patch.object(orchestrator, "datetime", _fixed(moment)), \
             patch.object(orchestrator, "_run_report", return_value=True) as run:
            orchestrator._tick()
            run.assert_called_once_with("close")
            self.assertEqual(
                json.loads(self.state_path.read_text(encoding="utf-8"))["close"],
                "2026-08-03")
            orchestrator._tick()
            self.assertEqual(run.call_count, 1)

    def test_failed_close_report_is_not_marked_and_retries(self):
        moment = datetime(2026, 8, 3, 16, 1, tzinfo=ET)
        with patch.object(orchestrator, "datetime", _fixed(moment)), \
             patch.object(orchestrator, "_run_report", return_value=False) as run:
            orchestrator._tick()
            orchestrator._tick()
            self.assertEqual(run.call_count, 2)
            self.assertFalse(self.state_path.exists())

    def test_open_report_trigger_at_930(self):
        moment = datetime(2026, 8, 3, 9, 31, tzinfo=ET)
        with patch.object(orchestrator, "datetime", _fixed(moment)), \
             patch.object(orchestrator, "_run_report", return_value=True) as run:
            self.assertEqual(orchestrator._maybe_run_report(), "open")
            run.assert_called_once_with("open")
            self.assertEqual(
                json.loads(self.state_path.read_text(encoding="utf-8"))["open"],
                "2026-08-03")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
