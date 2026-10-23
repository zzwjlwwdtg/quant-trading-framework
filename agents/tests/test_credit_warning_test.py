"""信用利差预警检验脚本 (2026-10-07) 的信号构造."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _credit_warning_test as ct


class CreditTest(unittest.TestCase):
    def test_tail_flag_keeps_warmup_nan(self):
        x = pd.Series(np.arange(300, dtype=float))
        f = ct.tail_flag(x, True)
        self.assertTrue(f.iloc[:251].isna().all())
        self.assertEqual(f.iloc[299], 1.0)
        self.assertEqual(ct.tail_flag(x, False).iloc[299], 0.0)

    def test_near_high_and_divergence(self):
        idx = pd.bdate_range("2020-01-01", periods=300)
        px = pd.Series(np.r_[np.linspace(100, 200, 280), np.full(20, 150.0)], index=idx)
        nh = ct.near_high(px)
        self.assertTrue(nh.iloc[:251].isna().all())
        self.assertEqual(nh.iloc[279], 1.0)       # 新高
        self.assertEqual(nh.iloc[290], 0.0)       # 跌 25%
        f = pd.Series(1.0, index=idx)
        d = ct.both(f, nh)
        self.assertEqual((d.iloc[279], d.iloc[290]), (1.0, 0.0))
        self.assertTrue(np.isnan(d.iloc[0]))

    def test_evaluate_flag_runs(self):
        idx = pd.bdate_range("1995-01-02", "2012-12-31")
        rng = np.random.default_rng(5)
        px = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx)))), index=idx)
        flag = pd.Series((rng.random(len(idx)) < 0.05).astype(float), index=idx)
        ev = ct.evaluate_flag(flag, px, [("train", "1995-01-01", "2003-12-31"), ("oos", "2004-01-01", "2100-01-01")])
        self.assertIn("maxdd_60d", ev["train"]["high"])
        self.assertIn(ct.ft.verdict(ev), {"no_reliable_signal", "bearish_warning", "contrarian_bullish"})


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
