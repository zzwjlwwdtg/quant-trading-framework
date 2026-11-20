"""止损复盘覆盖率统计：只使用构造的离线材料。"""
import unittest
from unittest.mock import patch

from trade_postmortem import (
    replay_trailing_stop_coverage,
    trailing_stop_coverage_attribution,
)


class TrailingStopCoverageAttributionTests(unittest.TestCase):
    def test_recent_selection_and_missing_verdict_association(self):
        rows = [
            {
                "order_id": "old", "sort_ts": "2026-01-01T00:00:00+00:00",
                "cost": {"missing": ["bid"]},
                "trailing_stop_measurement": {"missing": []},
                "hindsight_verdict": "合理",
            },
            {
                "order_id": "new", "sort_ts": "2026-01-03T00:00:00+00:00",
                "cost": {"missing": ["bid", "bid"]},
                "trailing_stop_measurement": {"missing": ["trigger"]},
                "hindsight_verdict": "难判断",
            },
            {
                "order_id": "middle", "sort_ts": "2026-01-02T00:00:00+00:00",
                "cost": {"missing": ["bid"]},
                "trailing_stop_measurement": {"missing": []},
                "hindsight_verdict": "unknown",
            },
        ]
        result = trailing_stop_coverage_attribution(rows, expected=2)
        self.assertEqual([r["order_id"] for r in result["records"]],
                         ["new", "middle"])
        self.assertEqual(result["missing_field_counts"]["cost.bid"], 2)
        group = result["missing_field_associations"]["cost.bid"]
        self.assertEqual((group["orders_with_missing"], group["judged"],
                          group["hard_to_judge"]), (2, 1, 1))
        self.assertEqual(group["hard_to_judge_rate"], 1.0)
        self.assertTrue(result["selection_verified"])
        self.assertEqual(result["records"][0]["primary_cause"], "unknown")
        self.assertTrue(all(v == "unknown" for v in
                            result["records"][0]["components"].values()))

    def test_unknown_time_and_unrecognized_missing_metadata(self):
        result = trailing_stop_coverage_attribution([{
            "order_id": "x", "sort_ts": None,
            "cost": {}, "trailing_stop_measurement": {},
        }], expected=1)
        self.assertFalse(result["selection_verified"])
        self.assertIn("cost.missing_metadata_unknown",
                      result["missing_field_counts"])
        self.assertIsNone(result["hindsight"]["hard_to_judge_rate"])

    def test_source_errors_prevent_verified_selection(self):
        audit = {
            "trailing_stop_records": [{
                "order_id": "x", "sort_ts": "2026-01-01T00:00:00+00:00",
                "cost": {"missing": []},
                "trailing_stop_measurement": {"missing": []},
            }],
            "source_errors": ["missing_file"],
            "unknown_order_times": [],
        }
        with patch("trade_postmortem.replay_execution_audit", return_value=audit):
            result = replay_trailing_stop_coverage(expected=1)
        self.assertFalse(result["selection_verified"])
        self.assertEqual(result["source_errors"], ["missing_file"])

    def test_invalid_sample_size(self):
        with self.assertRaises(ValueError):
            trailing_stop_coverage_attribution([], expected=0)


if __name__ == "__main__":
    unittest.main()
