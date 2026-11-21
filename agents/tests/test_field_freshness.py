"""字段时间戳诊断的只读测试。"""
import unittest
from datetime import datetime, timezone

from data_quality import assess_field_freshness


class FieldFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)

    def test_fresh_and_stale(self):
        fresh = assess_field_freshness("2026-11-21T11:00:00+00:00", now=self.now)
        stale = assess_field_freshness("2026-11-19T23:00:00+00:00", now=self.now)
        self.assertEqual(fresh["data_quality_tag"], "ok")
        self.assertEqual(fresh["age_seconds"], 3600)
        self.assertEqual(stale["data_quality_tag"], "critical")
        self.assertEqual(stale["reason"], "stale")

    def test_missing_and_future_are_unknown(self):
        missing = assess_field_freshness(None, now=self.now)
        future = assess_field_freshness("2026-11-21T13:00:00+00:00", now=self.now)
        self.assertEqual(missing["data_quality_tag"], "unknown")
        self.assertIsNone(missing["age_seconds"])
        self.assertEqual(future["data_quality_tag"], "unknown")
        self.assertEqual(future["reason"], "future_timestamp")
