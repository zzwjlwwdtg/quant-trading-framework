"""止损偏离基线的只读影子统计测试。"""
import unittest

from execution_analytics import stop_deviation_baseline


def event(order_id, date, price, **changes):
    row = {
        "order_id": order_id, "ticker": "MSFT",
        "last_fill_ts": date + "T15:00:00+00:00",
        "stop_line": 100, "price": price, "regime": "risk_off",
        "premarket": False, "atr_bucket": "预注册分桶一",
    }
    row.update(changes)
    return row


class StopDeviationBaselineTests(unittest.TestCase):
    def test_only_prior_matching_events_are_used(self):
        target = event("target", "2026-01-01", 98)
        rows = [
            event("a", "2024-01-01", 99),
            event("b", "2024-02-01", 97),
            event("c", "2024-03-01", 95, premarket=True),
            event("d", "2026-02-01", 90),
            event("e", "2024-04-01", 90, ticker="NVDA"),
        ]
        result = stop_deviation_baseline(rows, target)
        self.assertEqual(result["sample_size"], 2)
        self.assertAlmostEqual(result["percentile"], 50)
        self.assertAlmostEqual(result["p50_bps"], 200)
        self.assertAlmostEqual(result["p80_bps"], 260)
        self.assertAlmostEqual(result["p95_bps"], 290)
        self.assertIsNone(result["classification"])
        self.assertFalse(result["publication_authorized"])

    def test_missing_evidence_and_duplicate_ids_fail_closed(self):
        target = event("target", "2026-01-01", 98)
        missing = dict(target, atr_bucket=None)
        self.assertEqual(
            stop_deviation_baseline([], missing)["status"], "证据不足")
        row = event("a", "2024-01-01", 99)
        result = stop_deviation_baseline([row, dict(row)], target)
        self.assertEqual(result["status"], "证据不足")
        self.assertIsNone(result["percentile"])


if __name__ == "__main__":
    unittest.main()
