"""止损后轨迹的描述性统计测试。"""
import unittest

from _trade_postmortem import post_exit_distributions


class PostExitDistributionsTest(unittest.TestCase):
    def test_five_valid_observations_and_missing_values(self):
        tracks = [
            {"bucket": "ATR trailing", "returns_pct": {"T+1": value}}
            for value in (5, 1, 3, 2, 4, None, float("nan"))
        ]
        row = next(
            r for r in post_exit_distributions(tracks)
            if r["bucket"] == "ATR trailing" and r["horizon"] == "T+1"
        )
        self.assertEqual(row["events"], 7)
        self.assertEqual(row["n"], 5)
        self.assertEqual(
            [row["worst"], row["p25"], row["median"], row["p75"], row["best"]],
            [1, 2, 3, 4, 5],
        )

    def test_small_sample_does_not_show_statistics(self):
        tracks = [
            {"bucket": "cohort", "returns_pct": {"T+5": value}}
            for value in (1, 2, 3, 4)
        ]
        row = next(
            r for r in post_exit_distributions(tracks)
            if r["bucket"] == "cohort" and r["horizon"] == "T+5"
        )
        self.assertEqual(row["status"], "样本不足")
        self.assertIsNone(row["median"])
        self.assertIsNone(row["best"])


if __name__ == "__main__":
    unittest.main()
"""止损后收益分布的只读统计测试。"""
import unittest

from _trade_postmortem import post_exit_distributions


class PostExitDistributionTests(unittest.TestCase):
    def test_five_valid_values_show_interpolated_statistics(self):
        tracks = [
            {"bucket": "ATR trailing", "returns_pct": {"T+1": value}}
            for value in (5, 1, 3, 2, 4)
        ]
        rows = post_exit_distributions(tracks)
        row = next(r for r in rows
                   if r["bucket"] == "ATR trailing" and r["horizon"] == "T+1")

        self.assertEqual(row["events"], 5)
        self.assertEqual(row["n"], 5)
        self.assertEqual(row["status"], "可展示")
        self.assertEqual(
            [row["worst"], row["p25"], row["median"], row["p75"], row["best"]],
            [1, 2, 3, 4, 5],
        )

    def test_insufficient_and_invalid_values_are_not_displayed(self):
        tracks = [
            {"bucket": "cohort", "returns_pct": {"T+5": value}}
            for value in (1, 2, 3, 4, None, True, float("nan"), float("inf"))
        ]
        rows = post_exit_distributions(tracks)
        row = next(r for r in rows
                   if r["bucket"] == "cohort" and r["horizon"] == "T+5")

        self.assertEqual(row["events"], 8)
        self.assertEqual(row["n"], 4)
        self.assertEqual(row["status"], "样本不足")
        for key in ("median", "p25", "p75", "best", "worst"):
            self.assertIsNone(row[key])


if __name__ == "__main__":
    unittest.main()
