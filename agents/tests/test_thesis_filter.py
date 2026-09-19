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

    def test_expanded_blacklist_2026_09_18(self):
        # 2026-09-18 复盘后追加: QRVO/SWKS/MPWR/STM (漏网的 semi in universe)
        for tk in ["US.QRVO", "US.SWKS", "US.MPWR", "US.STM"]:
            with self.subTest(ticker=tk):
                blocked, _ = thesis_config.is_ticker_blacklisted(tk)
                self.assertTrue(blocked, f"{tk} should be blacklisted after 2026-09-18 expansion")

    def test_cloud_bond_ticker_is_whitelisted(self):
        # 2026-09-11 后 whitelist 收缩: bond (SHY/IEI) 和高 beta 云 (NBIS) 因 CPI hot 移除
        for tk in ["US.MSFT", "US.GOOGL", "US.GLD", "US.XLV"]:
            with self.subTest(ticker=tk):
                w, reason = thesis_config.is_ticker_whitelisted(tk)
                self.assertTrue(w, f"{tk} should be whitelisted")

    def test_bond_and_high_beta_cloud_removed_from_whitelist(self):
        # regression: CPI hot reprice 后 SHY/IEI/NBIS 应该不在 whitelist
        for tk in ["US.SHY", "US.IEI", "US.NBIS"]:
            with self.subTest(ticker=tk):
                w, _ = thesis_config.is_ticker_whitelisted(tk)
                self.assertFalse(w, f"{tk} should NOT be whitelisted after 2026-09-11 CPI reprice")

    def test_ticker_prefix_handling(self):
        # 无 US. 前缀也匹配
        b1, _ = thesis_config.is_ticker_blacklisted("SOXL")
        b2, _ = thesis_config.is_ticker_blacklisted("US.SOXL")
        self.assertEqual(b1, b2)
        self.assertTrue(b1)

    def test_invalidation_condition_triggered(self):
        macro_hot = {"cpi_mom_pct": 0.30, "us2y_60d_delta_bps": 10}
        triggered = thesis_config.check_invalidation(macro_hot)
        ids = {t["id"] for t in triggered}
        self.assertIn("cpi_hot_reprice", ids)
        self.assertNotIn("continued_hike_regime", ids,
                          "10bps 60d 变化在阈值 25 以下, 不该触发")

    def test_invalidation_condition_not_triggered(self):
        macro_cool = {"cpi_mom_pct": 0.10, "us2y_60d_delta_bps": 5}
        triggered = thesis_config.check_invalidation(macro_cool)
        self.assertEqual(triggered, [])

    def test_continued_hike_regime_triggers_on_2y_repricing(self):
        # 2026-09-17 加入 (Sep FOMC 加息 +25bps 后): 2Y 60d ≥ 25bps
        # → 市场对连续加息定价 → duration 敏感策略打脸
        macro = {"cpi_mom_pct": 0.10, "us2y_60d_delta_bps": 40}
        triggered = thesis_config.check_invalidation(macro)
        ids = {t["id"] for t in triggered}
        self.assertIn("continued_hike_regime", ids)
        # CPI 冷 → cpi_hot_reprice 不触发
        self.assertNotIn("cpi_hot_reprice", ids)

    def test_review_freshness(self):
        # 只断言接口 signature 正确 (date-dependent 不硬编)
        needs, msg = thesis_config.thesis_needs_review()
        self.assertIsInstance(needs, bool)
        self.assertIsInstance(msg, str)


class ThesisArchiveTests(unittest.TestCase):
    """Regression: 历史 thesis 被证伪后必须保留在 archive, 不能 lost."""

    def setUp(self):
        thesis_config._CACHE = {"mtime": 0, "data": None}

    def test_next_conjecture_exposed(self):
        c = thesis_config.next_thesis_conjecture()
        self.assertIsNotNone(c, "next_thesis_conjecture 缺失 (2026-09-17 加入)")
        self.assertIn("candidates", c)
        self.assertGreaterEqual(len(c["candidates"]), 2,
                                 "至少 2 个候选便于对比 (higher_for_longer / recession_first)")

    def test_archive_has_retired_theses(self):
        arch = thesis_config.list_retired_theses()
        self.assertGreaterEqual(len(arch), 2,
                                 "至少 2 条历史 (2026-Q3, 2026-Q3.1) — 不能丢")
        for e in arch:
            self.assertIn("retired_at", e)
            self.assertIn("retired_reason", e)
            self.assertIn("thesis", e)
            self.assertIn("version", e["thesis"])
        # 顺序: 老版本在前
        versions = [e["thesis"]["version"] for e in arch]
        self.assertIn("2026-Q3", versions)
        self.assertIn("2026-Q3.1_cpi_reprice", versions)

    def test_summary_includes_archive_and_conjecture_flags(self):
        s = thesis_config.summary()
        self.assertTrue(s["ok"])
        self.assertTrue(s.get("has_next_conjecture"))
        self.assertGreaterEqual(s.get("archived_count", 0), 2)

    def test_archive_thesis_for_promotion_appends_current(self):
        # 隔离: 用 tempfile 假 archive path
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.jsonl',
                                          encoding='utf-8') as f:
            tmp_path = Path(f.name)
        try:
            with patch.object(thesis_config, "_ARCHIVE_PATH", tmp_path):
                new_thesis = {"version": "test_v_next"}
                thesis_config.archive_thesis_for_promotion(
                    new_thesis,
                    retired_reason="unit test",
                    invalidation_evidence=[{"id": "test_trigger", "actual": 99}],
                )
                lines = tmp_path.read_text(encoding="utf-8").splitlines()
                # tempfile was empty, now should have 1 line
                self.assertEqual(len(lines), 1)
                entry = json.loads(lines[0])
                self.assertEqual(entry["promoted_to_version"], "test_v_next")
                self.assertEqual(entry["retired_reason"], "unit test")
                self.assertEqual(len(entry["invalidation_evidence"]), 1)
                # 保存的 thesis 是当前 config (不是新的)
                cur = thesis_config._load()
                self.assertEqual(entry["thesis"]["version"], cur["version"])
        finally:
            tmp_path.unlink(missing_ok=True)


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

    def test_soft_blacklist_low_confidence_buy_blocked(self):
        # 2026-09-18 加: whitelist 移除的 ticker (IEI/SHY/NBIS) 需要 conf ≥ 7
        # conf 5 (常规 WATCH_BUY) 应被 soft-block 到 HOLD
        decision = {"action": "WATCH_BUY", "confidence": 5, "reason": "trend up"}
        out = _apply_thesis_filter(decision, "US.IEI")
        self.assertEqual(out["action"], "HOLD")
        self.assertTrue(out.get("thesis_soft_blocked"))
        self.assertFalse(out.get("thesis_blocked", False),
                          "IEI 不在 hard blacklist, 只在 soft")
        self.assertEqual(out.get("min_confidence_required"), 7)
        self.assertIn("soft_blocked", out["reason"])

    def test_soft_blacklist_high_confidence_buy_allowed(self):
        # conf 8 ≥ 7 → 允许穿透
        decision = {"action": "BUY", "confidence": 8, "reason": "strong breakout"}
        out = _apply_thesis_filter(decision, "US.NBIS")
        self.assertEqual(out["action"], "BUY")
        self.assertEqual(out["confidence"], 8)
        self.assertFalse(out.get("thesis_soft_blocked", False),
                          "conf 8 ≥ min 7, 不该 block")

    def test_soft_blacklist_shy_iei_nbis_all_covered(self):
        # regression: 60d 复盘发现 IEI/SHY/NBIS 移除后仍被 BUY, 现在应全部 soft-blocked
        for tk in ["US.SHY", "US.IEI", "US.NBIS"]:
            with self.subTest(ticker=tk):
                soft, reason, meta = thesis_config.is_ticker_soft_blacklisted(tk)
                self.assertTrue(soft, f"{tk} should be soft-blacklisted")
                self.assertGreaterEqual(meta.get("min_confidence", 0), 7,
                                          f"{tk} min_confidence should be ≥ 7")

    def test_hard_blocked_takes_priority_over_soft(self):
        # 若同时 hard + soft, hard 优先 (虽然 config 里应该互斥)
        decision = {"action": "BUY", "confidence": 9, "reason": "breakout"}
        out = _apply_thesis_filter(decision, "US.SOXL")  # SOXL 只在 hard
        self.assertEqual(out["action"], "HOLD")
        self.assertTrue(out.get("thesis_blocked"))

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
