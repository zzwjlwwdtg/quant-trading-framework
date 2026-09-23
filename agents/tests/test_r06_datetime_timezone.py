"""R06 audit v4 (2026-09-22): timezone-aware datetime + effective_to.

audit v3 明确: 统一解析日期时区; effective_to 边界; date-string lex compare
不足以覆盖 timezone-aware 场景.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from webui import _parse_datetime_utc, _historical_thesis_at


class ParseDatetimeUtcTests(unittest.TestCase):

    def test_date_only_string(self):
        r = _parse_datetime_utc("2026-09-15")
        self.assertIsNotNone(r)
        self.assertEqual(r.year, 2026)
        self.assertEqual(r.tzinfo, timezone.utc)

    def test_iso_with_timezone(self):
        r = _parse_datetime_utc("2026-09-15T14:30:00+08:00")
        self.assertIsNotNone(r)
        # 08:00 offset → UTC 06:30
        self.assertEqual(r.hour, 6)
        self.assertEqual(r.minute, 30)
        self.assertEqual(r.tzinfo, timezone.utc)

    def test_z_suffix(self):
        r = _parse_datetime_utc("2026-09-15T14:30:00Z")
        self.assertIsNotNone(r)
        self.assertEqual(r.hour, 14)
        self.assertEqual(r.tzinfo, timezone.utc)

    def test_naive_datetime_assumed_utc(self):
        # ISO 无 tz suffix → 假设 UTC
        r = _parse_datetime_utc("2026-09-15T14:30:00")
        self.assertIsNotNone(r)
        self.assertEqual(r.tzinfo, timezone.utc)

    def test_datetime_object_with_tz(self):
        dt = datetime(2026, 9, 15, 14, 30, tzinfo=timezone(timedelta(hours=8)))
        r = _parse_datetime_utc(dt)
        self.assertEqual(r.hour, 6)   # 14:30+08 → 06:30 UTC

    def test_invalid_returns_none(self):
        self.assertIsNone(_parse_datetime_utc(""))
        self.assertIsNone(_parse_datetime_utc(None))
        self.assertIsNone(_parse_datetime_utc("garbage"))
        self.assertIsNone(_parse_datetime_utc("not-a-date"))


class TimezoneAwareHistoricalLookupTests(unittest.TestCase):

    def test_utc_and_local_tz_compared_correctly(self):
        # 边界: retired_at 用 Asia/Tokyo (+09:00) 表示 09-15 09:00 = UTC 09-15 00:00
        # 请求 as_of 用 UTC 09-14 23:59 → 应仍返 thesis (未到 retire 时点)
        archive = [
            {"retired_at": "2026-09-15T09:00:00+09:00",   # = 2026-09-15T00:00:00Z
             "thesis": {"version": "V1",
                        "effective_from": "2026-09-01T00:00:00Z"}},
        ]
        # UTC 09-14 23:59 < retired_at UTC 09-15 00:00 → 返 V1
        r = _historical_thesis_at("2026-09-14T23:59:00Z", archive)
        self.assertIsNotNone(r)
        self.assertEqual(r["version"], "V1")

        # UTC 09-15 00:01 > retired_at → V1 已 retire, 返 None (无更晚 thesis)
        r2 = _historical_thesis_at("2026-09-15T00:01:00Z", archive)
        self.assertIsNone(r2)


class EffectiveToTests(unittest.TestCase):

    def test_effective_to_excluded_when_as_of_after(self):
        # thesis 声明 effective_to=2026-08-01, as_of=2026-08-15 → None
        # (thesis 提前失效, 尽管 retired_at 更晚)
        archive = [
            {"retired_at": "2026-09-15T00:00:00Z",
             "thesis": {"version": "V1",
                        "effective_from": "2026-06-01",
                        "effective_to":   "2026-08-01"}},
        ]
        r = _historical_thesis_at("2026-08-15T00:00:00Z", archive)
        self.assertIsNone(r,
                            "R06 v4: effective_to 已过 → 该 thesis 不生效, 应 None")

    def test_effective_to_included_when_as_of_before(self):
        archive = [
            {"retired_at": "2026-09-15T00:00:00Z",
             "thesis": {"version": "V1",
                        "effective_from": "2026-06-01",
                        "effective_to":   "2026-08-01"}},
        ]
        r = _historical_thesis_at("2026-07-15T00:00:00Z", archive)
        self.assertIsNotNone(r)
        self.assertEqual(r["version"], "V1")

    def test_no_effective_to_falls_back_to_retired_at(self):
        # 无 effective_to → 用 retired_at 作 upper bound (原逻辑)
        archive = [
            {"retired_at": "2026-09-15T00:00:00Z",
             "thesis": {"version": "V1", "effective_from": "2026-06-01"}},
        ]
        r = _historical_thesis_at("2026-08-15T00:00:00Z", archive)
        self.assertIsNotNone(r)
        self.assertEqual(r["version"], "V1")


if __name__ == "__main__":
    unittest.main()
