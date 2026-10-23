"""F06 剩余项 (Codex MODEL_AUDIT 2026-09-19 / SYSTEM_REVIEW 2026-09-20 §4).

1. 标签不越过 test_end: 入场 i 的未来价 i+hold 必须仍在该测试段内.
2. 重叠持有期不重复计数: 同一规则持有 hold 天期间不再开新样本.
3. 准入不只看 "≥5 笔 + 胜率≥52%": 至少 20 个独立样本, 胜率单侧 95% Wilson 下界 > 50%,
   平均有效收益 > 0, 折间稳定性 ≥ 0.5.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import research_validation as rv


def _rows(closes, signal=lambda i: True):
    return [{"close": c, "signal": signal(i), "time_key": i} for i, c in enumerate(closes)]


RULE_CHECK = lambda row, rule: row["signal"]


class NoLabelLeakage(unittest.TestCase):
    def test_labels_stay_inside_test_window(self):
        # 先用同参数求出第一折测试段, 段内价格平, 段后暴涨.
        # 若标签越界, 段尾样本会读到暴涨价格 → avg_ret > 0.
        n, hold = 140, 5
        probe = rv.evaluate_rule_walk_forward(
            {"action": "BUY", "hold": hold}, _rows([100.0] * n), RULE_CHECK, train_size=60)
        test_end = probe["folds"][0]["test"][1]
        closes = [100.0] * test_end + [200.0] * (n - test_end)
        res = rv.evaluate_rule_walk_forward(
            {"action": "BUY", "hold": hold}, _rows(closes), RULE_CHECK, train_size=60)
        self.assertGreater(res["folds"][0]["n"], 0)
        self.assertEqual(res["folds"][0]["avg_ret"], 0.0, "label crossed test_end")


class NonOverlappingSamples(unittest.TestCase):
    def test_one_sample_per_holding_period(self):
        hold = 5
        closes = [100 * (1.001 ** i) for i in range(200)]
        res = rv.evaluate_rule_walk_forward(
            {"action": "BUY", "hold": hold}, _rows(closes), RULE_CHECK,
            train_size=60, test_size=40)
        for fold in res["folds"]:
            start, end = fold["test"]
            max_independent = -(-(end - start - hold) // hold)   # ceil
            self.assertLessEqual(fold["n"], max_independent, fold)


class AdmissionBar(unittest.TestCase):
    def test_five_perfect_trades_not_enough(self):
        ok, info = rv._admission({"n": 5, "win_rate": 100.0, "avg_ret": 2.0}, stability=1.0)
        self.assertFalse(ok)
        self.assertIn("n", info["failed"])

    def test_52pct_on_25_trades_not_significant(self):
        ok, info = rv._admission({"n": 25, "win_rate": 52.0, "avg_ret": 0.3}, stability=1.0)
        self.assertFalse(ok)
        self.assertIn("win_rate_lower_bound", info["failed"])

    def test_negative_avg_return_rejected(self):
        ok, info = rv._admission({"n": 40, "win_rate": 80.0, "avg_ret": -0.1}, stability=1.0)
        self.assertFalse(ok)
        self.assertIn("avg_ret", info["failed"])

    def test_strong_evidence_passes(self):
        ok, info = rv._admission({"n": 30, "win_rate": 83.3, "avg_ret": 0.8}, stability=0.75)
        self.assertTrue(ok, info)
        self.assertGreater(info["win_rate_lower_bound"], 50.0)

    def test_result_declares_method(self):
        closes = [100 * (1.002 ** i) for i in range(240)]
        res = rv.evaluate_rule_walk_forward(
            {"action": "BUY", "hold": 3}, _rows(closes, lambda i: i % 4 == 0), RULE_CHECK,
            train_size=80, test_size=40)
        self.assertFalse(res["train_used_for_fitting"])
        self.assertEqual(res["sampling"], "non_overlapping")
        self.assertIn("admission", res)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
