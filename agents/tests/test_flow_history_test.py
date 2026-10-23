"""预注册检验脚本的分析函数 (2026-10-07)."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _flow_history_test as ft


class FlowTest(unittest.TestCase):
    def test_parse_cboe(self):
        raw = "Disclaimer line\nMore text\nDATE,CALL,PUT,TOTAL,P/C Ratio\n11/1/2006,10,12,22,1.2\n11/2/2006,10,8,18,0.8\n11/2/2006,10,9,19,0.9\n"
        s = ft.parse_cboe_csv(raw)
        self.assertEqual(list(s.values), [1.2, 0.9])

    def test_forward_outcomes_start_next_day(self):
        px = pd.Series(np.arange(100, 130, dtype=float), index=pd.bdate_range("2020-01-01", periods=30))
        o = ft.forward_outcomes(px)
        self.assertAlmostEqual(o["ret_5d"].iloc[0], 106 / 101 - 1)   # 入场 t+1 收盘
        self.assertTrue(np.isnan(o["maxdd_20d"].iloc[-1]))
        self.assertEqual(o["maxdd_20d"].iloc[0], 0.0)                # 单调上涨无回撤

    def test_rank_uses_past_only(self):
        s = pd.Series(np.arange(300, dtype=float))
        r = ft.rolling_rank(s, 252)
        self.assertTrue(r.iloc[:251].isna().all())
        self.assertEqual(r.iloc[260], 1.0)

    def test_episodes(self):
        f = pd.Series([1, 1, 0, 0, 1] + [0] * 20 + [1], dtype=bool)
        self.assertEqual(ft.count_episodes(f, gap=10), 2)

    def test_rotation_detects_planted_effect(self):
        rng = np.random.default_rng(0)
        n = 3000
        flag = np.zeros(n, bool); flag[rng.choice(n, 150, replace=False)] = True
        y = rng.normal(0, 1, n); y[flag] -= 1.0
        diff, p = ft.rotation_pvalue(flag, y, n_perm=500)
        self.assertLess(diff, -0.8); self.assertLess(p, 0.01)
        diff2, p2 = ft.rotation_pvalue(flag, rng.normal(0, 1, n), n_perm=500)
        self.assertGreater(p2, 0.05)

    def test_verdict(self):
        def per(dd, ddp, r, rp, ep=20):
            return {"high": {"episodes": ep, "maxdd_20d": {"diff": dd, "p": ddp}, "ret_20d": {"diff": r, "p": rp}}}
        self.assertEqual(ft.verdict({"train": per(-0.01, 0.01, 0, 0.5), "oos": per(-0.005, 0.3, 0, 0.5)}), "bearish_warning")
        self.assertEqual(ft.verdict({"train": per(0, 0.5, 0.01, 0.02), "oos": per(0, 0.5, 0.004, 0.4)}), "contrarian_bullish")
        self.assertEqual(ft.verdict({"train": per(-0.01, 0.01, 0, 0.5), "oos": per(0.002, 0.3, 0, 0.5)}), "no_reliable_signal")
        self.assertEqual(ft.verdict({"train": per(-0.01, 0.01, 0, 0.5, ep=5), "oos": per(-0.01, 0.3, 0, 0.5)}), "insufficient_episodes")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
