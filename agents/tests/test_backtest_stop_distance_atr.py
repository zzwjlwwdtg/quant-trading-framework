"""EVO-20261014-1: ATR 候选 (ATR(14/20) × k) vs trail_8 固定, 预注册回测

单测只覆盖纯函数 + ATR 分支的模拟语义, 不触发 yfinance 真实下载.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


def _load_module():
    """脚本名以下划线开头, 用 importlib 直接按路径加载.

    脚本 top-level 调 `sys.stdout.reconfigure(...)`; 在 pytest capture 下
    stdout 可能是无 reconfigure 的 wrapper, 临时打个 shim 避免 AttributeError.
    """
    path = AGENTS_DIR / "_backtest_stop_distance.py"
    stdout = sys.stdout
    if not hasattr(stdout, "reconfigure"):
        stdout.reconfigure = lambda **_kw: None  # type: ignore[attr-defined]
    try:
        spec = importlib.util.spec_from_file_location("_backtest_stop_distance", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        # 清掉 shim, 不污染其他测试
        if getattr(stdout, "reconfigure", None) is not None and \
           getattr(type(stdout), "reconfigure", None) is None:
            try:
                del stdout.reconfigure  # type: ignore[attr-defined]
            except Exception:
                pass
    return mod


BSD = _load_module()


class WilderATR(unittest.TestCase):
    """Wilder ATR 平滑的数值行为."""

    def test_short_sequence_yields_none(self):
        atr = BSD._wilder_atr([1.0, 2.0], [0.0, 1.0], [1.0, 2.0], period=5)
        self.assertEqual(atr, [None, None])

    def test_seed_equals_simple_mean_of_first_n_trs(self):
        # 平稳序列: H-L=1.0 each, prev_close 恒定 → TR=1.0, ATR 应恒为 1.0
        highs  = [10.0] + [11.0] * 10
        lows   = [9.0]  + [10.0] * 10
        closes = [10.0] + [10.5] * 10
        atr = BSD._wilder_atr(highs, lows, closes, period=5)
        # 前 4 根 None, seed @ idx=4
        self.assertIsNone(atr[0])
        self.assertIsNone(atr[3])
        self.assertAlmostEqual(atr[4], 1.0, places=6)
        # 平稳 → 稳定在 1.0
        self.assertAlmostEqual(atr[-1], 1.0, places=6)

    def test_atr_rises_when_volatility_jumps(self):
        # 前 10 根低波, 后 10 根高波, ATR 应单调上升
        highs  = [100 + (i % 2) for i in range(10)] + [100 + 5*(i % 2) for i in range(10)]
        lows   = [100 - (i % 2) for i in range(10)] + [100 - 5*(i % 2) for i in range(10)]
        closes = [100.0] * 20
        atr = BSD._wilder_atr(highs, lows, closes, period=5)
        early = atr[9]
        late  = atr[-1]
        self.assertIsNotNone(early)
        self.assertIsNotNone(late)
        self.assertGreater(late, early * 1.5)


class SimulateStopATR(unittest.TestCase):
    """`_simulate_stop(kind='atr')` 的触发语义."""

    def _bars(self, highs, lows, closes):
        return pd.DataFrame({"High": highs, "Low": lows, "Close": closes,
                             "Open": closes})

    def test_atr_stop_triggers_when_low_breaches(self):
        # buy @ 100, ATR=1.0, k=2 → stop = rolling_high - 2.0
        # day0: H=100, L=99.5 (未破 98); day1: H=100, L=97.9 → 破 98 → 触发
        bars = self._bars(highs=[100.0, 100.0, 100.0],
                          lows =[99.5,  97.9,  97.0],
                          closes=[100.0, 97.9, 97.5])
        exit_px, exit_i, reason = BSD._simulate_stop(
            bars, buy_price=100.0, ticker="US.TQQQ",
            kind="atr", param=(14, 2.0), atr_at_entry=1.0)
        self.assertEqual(exit_i, 1)
        self.assertIn("stop@", reason)
        self.assertAlmostEqual(exit_px, 98.0, places=6)

    def test_atr_stop_follows_rolling_high(self):
        # buy @ 100, ATR=1.0, k=2 (stop width 2.0)
        # day0: H=110 L=109 (未破 108) → rolling_high 升到 110, stop=108
        # day1: H=109 L=107.5 → rolling_high 仍 110, stop=108, L=107.5 < 108 触发 @ 108
        bars = self._bars(highs=[110.0, 109.0], lows=[109.0, 107.5], closes=[109.5, 108.0])
        exit_px, exit_i, reason = BSD._simulate_stop(
            bars, buy_price=100.0, ticker="US.TQQQ",
            kind="atr", param=(14, 2.0), atr_at_entry=1.0)
        self.assertEqual(exit_i, 1)
        self.assertAlmostEqual(exit_px, 108.0, places=6)

    def test_atr_stop_falls_through_to_hold_end(self):
        # 价格一路上涨, 不该触发
        n = 10
        bars = self._bars(highs=[100 + i for i in range(n)],
                           lows =[99 + i  for i in range(n)],
                           closes=[100 + i for i in range(n)])
        exit_px, exit_i, reason = BSD._simulate_stop(
            bars, buy_price=100.0, ticker="US.TQQQ",
            kind="atr", param=(20, 2.0), atr_at_entry=1.0)
        self.assertEqual(reason, "hold_end")
        self.assertEqual(exit_px, 100.0 + (n - 1))

    def test_missing_atr_at_entry_degrades_safely(self):
        bars = self._bars(highs=[100.0, 95.0], lows=[99.0, 90.0], closes=[100.0, 92.0])
        exit_px, exit_i, reason = BSD._simulate_stop(
            bars, buy_price=100.0, ticker="US.TQQQ",
            kind="atr", param=(14, 2.0), atr_at_entry=None)
        self.assertEqual(reason, "atr_missing")

    def test_atr_candidate_wider_stop_survives_noise(self):
        # 同一波动: ATR=1.0. k=1.5 立刻被 day0 L=98 触发, k=3.0 不触发
        bars = self._bars(highs=[100.0, 101.0, 102.0],
                           lows =[98.0,  99.0,  100.0],
                           closes=[100.0, 101.0, 102.0])
        _, _, reason_tight = BSD._simulate_stop(
            bars, 100.0, "US.TQQQ", "atr", (14, 1.5), atr_at_entry=1.0)
        _, _, reason_wide = BSD._simulate_stop(
            bars, 100.0, "US.TQQQ", "atr", (14, 3.0), atr_at_entry=1.0)
        self.assertIn("stop@", reason_tight)
        self.assertEqual(reason_wide, "hold_end")


class SummarizePolicy(unittest.TestCase):
    """`_summarize_policy` 和 `_loss_stats` 的指标口径."""

    def test_empty_sample(self):
        m = BSD._summarize_policy([])
        self.assertEqual(m["n"], 0)
        self.assertEqual(m["avg_loss"], 0.0)

    def test_loss_stats_only_counts_negatives(self):
        avg, med, n = BSD._loss_stats([5.0, -2.0, -4.0, -6.0, 10.0])
        self.assertEqual(n, 3)
        self.assertAlmostEqual(avg, -4.0, places=6)
        self.assertEqual(med, -4.0)

    def test_summary_cagr_positive_for_winning_series(self):
        m = BSD._summarize_policy([5.0, 3.0, 4.0, 2.0, 6.0])
        self.assertEqual(m["n"], 5)
        self.assertGreater(m["cagr"], 0)
        self.assertEqual(m["n_loss"], 0)

    def test_cagr_clipped_to_handle_extreme_loss(self):
        # 包含 -100% 不应炸 log
        m = BSD._summarize_policy([-100.0, -50.0, -10.0])
        self.assertLess(m["cagr"], 0)
        self.assertEqual(m["n_loss"], 3)


class EvaluateATRPass(unittest.TestCase):
    """预注册 pass gate 的四条判定, 边界 case 必须精确."""

    def _base(self, **overrides):
        """baseline 指标: 中性的 pnl 分布, n=100."""
        m = {"n": 100, "avg_pnl": 1.0, "cagr": 10.0,
             "avg_loss": -5.0, "median_loss": -4.0, "n_loss": 40}
        m.update(overrides)
        return m

    def test_strict_match_passes(self):
        base = self._base()
        cand = self._base()
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertTrue(ok, fails)
        self.assertEqual(fails, [])

    def test_cagr_within_50bp_tolerance(self):
        base = self._base(cagr=10.0)
        cand = self._base(cagr=9.6)   # -40bp, still OK
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertTrue(ok, fails)

    def test_cagr_dropping_more_than_50bp_fails(self):
        base = self._base(cagr=10.0)
        cand = self._base(cagr=9.4)   # -60bp
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertFalse(ok)
        self.assertTrue(any("cagr_drop" in f for f in fails))

    def test_worse_avg_loss_fails(self):
        base = self._base(avg_loss=-5.0)
        cand = self._base(avg_loss=-6.0)   # 更负
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertFalse(ok)
        self.assertTrue(any("avg_loss_worse" in f for f in fails))

    def test_worse_median_loss_fails(self):
        base = self._base(median_loss=-4.0)
        cand = self._base(median_loss=-5.0)
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertFalse(ok)
        self.assertTrue(any("median_loss_worse" in f for f in fails))

    def test_trade_count_blowup_fails(self):
        base = self._base(n=100)
        cand = self._base(n=140)   # 1.4× > 1.3×
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertFalse(ok)
        self.assertTrue(any("trade_count_ratio" in f for f in fails))

    def test_trade_count_exact_ratio_passes(self):
        base = self._base(n=100)
        cand = self._base(n=130)   # 1.3× = cap
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertTrue(ok, fails)

    def test_better_candidate_passes(self):
        base = self._base(cagr=5.0, avg_loss=-5.0, median_loss=-4.0, n=100)
        cand = self._base(cagr=12.0, avg_loss=-3.0, median_loss=-2.0, n=100)
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertTrue(ok, fails)

    def test_empty_sample_rejects(self):
        base = self._base(n=0)
        cand = self._base()
        ok, fails = BSD._evaluate_atr_pass(base, cand)
        self.assertFalse(ok)
        self.assertIn("empty_sample", fails)


class GenerateEntries(unittest.TestCase):
    """`_generate_entries` 的 warmup / spacing / 日期区间."""

    def _bars(self, n=200):
        idx = pd.bdate_range("2024-01-02", periods=n)
        df = pd.DataFrame({"Open":  [100.0 + i*0.1 for i in range(n)],
                           "High":  [101.0 + i*0.1 for i in range(n)],
                           "Low":   [ 99.0 + i*0.1 for i in range(n)],
                           "Close": [100.0 + i*0.1 for i in range(n)]}, index=idx)
        return df

    def test_entries_respect_warmup_and_hold_tail(self):
        bars = self._bars(n=200)
        entries = BSD._generate_entries(bars,
                                         start=date(2024, 1, 1),
                                         end=date(2027, 1, 1),
                                         spacing=20, warmup=60)
        self.assertTrue(all(e["idx"] >= 60 for e in entries))
        self.assertTrue(all(e["idx"] <= 200 - BSD.HOLD_DAYS - 1 for e in entries))

    def test_entries_respect_date_window(self):
        bars = self._bars(n=400)
        entries = BSD._generate_entries(bars,
                                         start=date(2025, 1, 1),
                                         end=date(2025, 6, 30),
                                         spacing=20, warmup=60)
        for e in entries:
            self.assertGreaterEqual(e["entry_date"], date(2025, 1, 1))
            self.assertLessEqual(e["entry_date"], date(2025, 6, 30))

    def test_spacing_is_uniform(self):
        bars = self._bars(n=400)
        entries = BSD._generate_entries(bars,
                                         start=date(2024, 1, 1),
                                         end=date(2027, 1, 1),
                                         spacing=20, warmup=60)
        if len(entries) >= 2:
            diffs = [entries[i+1]["idx"] - entries[i]["idx"]
                     for i in range(len(entries) - 1)]
            self.assertTrue(all(d == 20 for d in diffs))

    def test_empty_bars_returns_empty(self):
        self.assertEqual(BSD._generate_entries(None, date(2024,1,1), date(2024,12,31)), [])


class ATRPoliciesWiring(unittest.TestCase):
    """提案里的候选集合必须完整登记, 不缺项."""

    def test_eight_atr_candidates_registered(self):
        atrs = {p[0]: p for p in BSD.STOP_POLICIES if p[1] == "atr"}
        expected = {
            "atr14_x1.5", "atr14_x2.0", "atr14_x2.5", "atr14_x3.0",
            "atr20_x1.5", "atr20_x2.0", "atr20_x2.5", "atr20_x3.0",
        }
        self.assertEqual(set(atrs), expected)
        # 每个 param 都是 (period, k) 二元组, period ∈ {14, 20}, k ∈ {1.5..3.0}
        for name, (period, k) in {n: p[2] for n, p in atrs.items()}.items():
            self.assertIn(period, (14, 20))
            self.assertIn(k, (1.5, 2.0, 2.5, 3.0))

    def test_pass_criteria_matches_proposal(self):
        self.assertEqual(BSD.PASS_CRITERIA_ATR["cagr_min_delta_pp"], -0.50)
        self.assertEqual(BSD.PASS_CRITERIA_ATR["trade_count_max_ratio"], 1.3)
        self.assertEqual(BSD.TRAIN_START.isoformat(), "2024-01-01")
        self.assertEqual(BSD.TRAIN_END.isoformat(),   "2026-06-30")
        self.assertEqual(BSD.OOS_START.isoformat(),   "2026-07-01")

    def test_baseline_is_trail_8(self):
        self.assertEqual(BSD.BASELINE_NAME, "trail_8")


if __name__ == "__main__":
    unittest.main()
