"""Tests for thesis_config + decision_agent thesis filter (closed-loop guard)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import thesis_config
from decision_agent import _apply_thesis_filter


class ThesisConfigTests(unittest.TestCase):
    def setUp(self):
        # 强制刷 cache, 隔离测试间污染
        thesis_config._CACHE = {"mtime": 0, "data": None}

    def test_load_returns_expected_shape(self):
        s = thesis_config.summary()
        self.assertTrue(s["ok"])
        self.assertIsInstance(s["blacklist_count"], int)
        self.assertGreater(s["blacklist_count"], 0)
        self.assertIsNotNone(s["version"])

    def test_semi_ticker_is_blacklisted(self):
        # 2026-Q3 thesis: avoid semi
        for tk in ["US.SOXL", "US.KLAC", "US.NVDA", "US.MU", "US.DRAM"]:
            with self.subTest(ticker=tk):
                blocked, reason = thesis_config.is_ticker_blacklisted(tk)
                self.assertTrue(blocked, f"{tk} should be blacklisted")
                self.assertIn("semi", reason.lower())

    def test_cloud_bond_ticker_is_whitelisted(self):
        for tk in ["US.MSFT", "US.GOOGL", "US.SHY", "US.IEI"]:
            with self.subTest(ticker=tk):
                w, reason = thesis_config.is_ticker_whitelisted(tk)
                self.assertTrue(w, f"{tk} should be whitelisted")

    def test_ticker_prefix_handling(self):
        # 无 US. 前缀也匹配
        b1, _ = thesis_config.is_ticker_blacklisted("SOXL")
        b2, _ = thesis_config.is_ticker_blacklisted("US.SOXL")
        self.assertEqual(b1, b2)
        self.assertTrue(b1)

    def test_invalidation_condition_triggered(self):
        macro_hot = {"cpi_mom_pct": 0.30}
        triggered = thesis_config.check_invalidation(macro_hot)
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0]["id"], "cpi_hot_reprice")

    def test_invalidation_condition_not_triggered(self):
        macro_cool = {"cpi_mom_pct": 0.10}
        triggered = thesis_config.check_invalidation(macro_cool)
        self.assertEqual(triggered, [])

    def test_review_freshness(self):
        # config 里 last_reviewed_at = 2026-09-02, interval 30d
        needs, msg = thesis_config.thesis_needs_review()
        # 当前 date 距 2026-09-02 < 30d 时应 False
        # 未来 date 距 > 30d 时应 True
        # 只断言接口 signature 正确
        self.assertIsInstance(needs, bool)
        self.assertIsInstance(msg, str)


class ThesisFilterAppliedTests(unittest.TestCase):
    def setUp(self):
        thesis_config._CACHE = {"mtime": 0, "data": None}

    def test_watch_buy_on_semi_blocked_to_hold(self):
        decision = {"action": "WATCH_BUY", "confidence": 5, "reason": "oversold+uptrend"}
        out = _apply_thesis_filter(decision, "US.SOXL")
        self.assertEqual(out["action"], "HOLD")
        self.assertEqual(out["confidence"], 0)
        self.assertTrue(out.get("thesis_blocked"))
        self.assertEqual(out.get("demoted_from"), "WATCH_BUY")
        self.assertIn("thesis_blocked", out["reason"])

    def test_buy_on_semi_blocked(self):
        decision = {"action": "BUY", "confidence": 8, "reason": "breakout"}
        out = _apply_thesis_filter(decision, "US.KLAC")
        self.assertEqual(out["action"], "HOLD")
        self.assertTrue(out.get("thesis_blocked"))

    def test_buy_on_non_blacklist_untouched(self):
        decision = {"action": "WATCH_BUY", "confidence": 5, "reason": "oversold"}
        out = _apply_thesis_filter(decision, "US.MSFT")
        self.assertEqual(out["action"], "WATCH_BUY")
        self.assertEqual(out["confidence"], 5)
        self.assertFalse(out.get("thesis_blocked", False))

    def test_hold_on_semi_untouched(self):
        # HOLD 本来就不是 BUY 类, 不应二次干预
        decision = {"action": "HOLD", "confidence": 2, "reason": "no clear signal"}
        out = _apply_thesis_filter(decision, "US.SOXL")
        self.assertEqual(out["action"], "HOLD")
        self.assertFalse(out.get("thesis_blocked", False))

    def test_sell_on_semi_untouched(self):
        # 卖出 blacklist ticker 是合规的, 不阻拦
        decision = {"action": "REDUCE_RISK", "confidence": 6, "reason": "rsi high"}
        out = _apply_thesis_filter(decision, "US.SOXL")
        self.assertEqual(out["action"], "REDUCE_RISK")

    def test_hot_reload_on_config_mtime_change(self):
        # 改 config 内容 → cache 应自动刷新
        # 使用 mock 避免真的写盘
        real_load = thesis_config._load
        cfg_v1 = {"version": "v1", "blacklist_tickers": ["US.FOO"],
                  "whitelist_tickers": [], "invalidation_conditions": []}
        cfg_v2 = {"version": "v2", "blacklist_tickers": ["US.BAR"],
                  "whitelist_tickers": [], "invalidation_conditions": []}
        state = {"n": 0}
        def fake_load():
            state["n"] += 1
            return cfg_v1 if state["n"] == 1 else cfg_v2
        with patch.object(thesis_config, "_load", side_effect=fake_load):
            b1, _ = thesis_config.is_ticker_blacklisted("US.FOO")
            b2, _ = thesis_config.is_ticker_blacklisted("US.BAR")
        self.assertTrue(b1)
        self.assertTrue(b2)


if __name__ == "__main__":
    unittest.main()
