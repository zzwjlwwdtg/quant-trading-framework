"""F06 regression (audit 2026-09-19): REDUCE 方向命中 vs 收益口径要一致.

之前 bug: 合成单调下跌序列上 REDUCE 方向 100% 命中, 但 avg_ret 用 raw price
change → 负值 → 出现"胜率 100%, 收益极负"的矛盾, 且仍通过准入.

Fix: reduce 动作用 effective_ret = -raw_ret (avoided loss), 与 win 定义对齐.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import research_validation as rv


def _always_true(row, rule):
    return True


def _monotonic_decline(n: int = 200) -> pd.DataFrame:
    return pd.DataFrame({
        "close":    [100 - i * 0.3 for i in range(n)],
        "time_key": pd.date_range("2024-01-01", periods=n, freq="D"),
    })


def _monotonic_rise(n: int = 200) -> pd.DataFrame:
    return pd.DataFrame({
        "close":    [100 + i * 0.3 for i in range(n)],
        "time_key": pd.date_range("2024-01-01", periods=n, freq="D"),
    })


class ReduceOnDeclineTests(unittest.TestCase):

    def test_reduce_on_declining_series_positive_avg_return(self):
        # REDUCE 信号在下跌序列: 方向命中率高 → effective avg_ret 也应为正
        rule = {"action": "REDUCE", "hold": 5}
        rows = _monotonic_decline()
        r = rv.evaluate_rule_walk_forward(rule, rows, _always_true)
        oos = r.get("oos_all") or r.get("all_oos") or []
        if not oos:
            # 某些版本可能只暴露 folds summary
            folds = r.get("folds", [])
            self.assertTrue(len(folds) > 0)
            for f in folds:
                if f.get("n", 0) == 0:
                    continue
                self.assertGreater(f["win_rate"], 90.0,
                                     "下跌序列 REDUCE 命中率应 > 90%")
                self.assertGreater(f["avg_ret"], 0,
                                     "F06 fix: REDUCE 命中 → effective avg_ret 应为正 (避免的损失)")
        else:
            wins = sum(1 for t in oos if t["win"])
            self.assertGreater(wins / len(oos), 0.9)
            avg = sum(t["ret"] for t in oos) / len(oos)
            self.assertGreater(avg, 0,
                                 "F06 fix: REDUCE 命中 → effective avg 应为正")


class BuyOnRiseTests(unittest.TestCase):

    def test_buy_on_rising_series_positive_avg_return(self):
        # 对照: BUY 信号在上涨序列, 命中率高 + avg_ret 正 (不变)
        rule = {"action": "WATCH_BUY", "hold": 5}
        rows = _monotonic_rise()
        r = rv.evaluate_rule_walk_forward(rule, rows, _always_true)
        folds = r.get("folds", [])
        self.assertTrue(len(folds) > 0)
        for f in folds:
            if f.get("n", 0) == 0:
                continue
            self.assertGreater(f["win_rate"], 90.0)
            self.assertGreater(f["avg_ret"], 0)


class BuyOnDeclineTests(unittest.TestCase):

    def test_buy_on_declining_series_low_win_rate(self):
        # BUY 信号在下跌序列: 命中率 0% + avg_ret 负 (原始 raw)
        rule = {"action": "BUY", "hold": 5}
        rows = _monotonic_decline()
        r = rv.evaluate_rule_walk_forward(rule, rows, _always_true)
        folds = r.get("folds", [])
        for f in folds:
            if f.get("n", 0) == 0:
                continue
            self.assertLess(f["win_rate"], 10.0)
            self.assertLess(f["avg_ret"], 0,
                              "BUY 在下跌序列: 未命中 → avg_ret 应为负 (未做符号翻转)")


class WatchBuyProbeCoveredAsBullTests(unittest.TestCase):

    def test_watch_buy_probe_treated_as_bull(self):
        # audit F06 specific: "在单调上涨序列上, WATCH_BUY_PROBE 被当作非看多"
        rule = {"action": "WATCH_BUY_PROBE", "hold": 5}
        rows = _monotonic_rise()
        r = rv.evaluate_rule_walk_forward(rule, rows, _always_true)
        folds = r.get("folds", [])
        for f in folds:
            if f.get("n", 0) == 0:
                continue
            self.assertGreater(f["win_rate"], 90.0,
                                 "WATCH_BUY_PROBE 应视为 bull, 上涨序列命中率高")


if __name__ == "__main__":
    unittest.main()
