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
        # F02 fix (2026-09-19): min_confidence 是 canonical 10-scale, 在 TECHNICAL_ONLY=1
        # (scale=5) 下 effective_min = round(7*5/10) = 4. conf 3 < 4 应 block.
        import os
        os.environ["TECHNICAL_ONLY"] = "1"
        decision = {"action": "WATCH_BUY", "confidence": 3, "reason": "weak trend"}
        out = _apply_thesis_filter(decision, "US.IEI")
        self.assertEqual(out["action"], "HOLD")
        self.assertTrue(out.get("thesis_soft_blocked"))
        self.assertFalse(out.get("thesis_blocked", False),
                          "IEI 不在 hard blacklist, 只在 soft")
        # effective (in current scale) 应显示 4 (7*5/10 rounded)
        self.assertEqual(out.get("min_confidence_required"), 4)
        self.assertEqual(out.get("min_confidence_canonical"), 7)
        self.assertEqual(out.get("confidence_scale"), 5)
        self.assertIn("soft_blocked", out["reason"])

    def test_soft_blacklist_high_confidence_buy_allowed(self):
        # F02 fix: 在 TECHNICAL_ONLY=1 (scale=5) 下, conf 4 ≥ effective 4 → 穿透
        import os
        os.environ["TECHNICAL_ONLY"] = "1"
        decision = {"action": "BUY", "confidence": 4, "reason": "strong breakout"}
        out = _apply_thesis_filter(decision, "US.NBIS")
        self.assertEqual(out["action"], "BUY")
        self.assertEqual(out["confidence"], 4)
        self.assertFalse(out.get("thesis_soft_blocked", False),
                          "conf 4 ≥ effective min 4 (canonical 7/10 * scale 5/10), 应穿透")

    def test_soft_blacklist_effective_min_scales_with_confidence_scale(self):
        # F02 regression: 若某天切到 TECHNICAL_ONLY=0 (10-scale), effective_min 应变 7
        # 相同 canonical 7/10 → 10-scale 下需 conf ≥ 7 才穿透
        import os
        os.environ["TECHNICAL_ONLY"] = "0"
        try:
            decision = {"action": "BUY", "confidence": 6, "reason": "trend"}
            out = _apply_thesis_filter(decision, "US.IEI")
            self.assertEqual(out["action"], "HOLD",
                              "10-scale 下 conf 6 < effective 7 应 block")
            self.assertEqual(out.get("min_confidence_required"), 7)
            self.assertEqual(out.get("confidence_scale"), 10)
        finally:
            os.environ["TECHNICAL_ONLY"] = "1"   # 恢复默认

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


class TopPicksSoftBlacklistTests(unittest.TestCase):
    """P1 coupling fix (2026-09-19): top_picks 需与 decision_agent._apply_thesis_filter
    对齐, 否则前后台不一致 (top_picks 推荐 IEI conf=5 但 decision_agent 会 HOLD)."""

    def setUp(self):
        thesis_config._CACHE = {"mtime": 0, "data": None}

    def test_soft_blocked_low_conf_excluded_from_scoring(self):
        # F02 fix: conf 3 < effective 4 (canonical 7/10, scale 5) → excluded
        import os, top_picks
        os.environ["TECHNICAL_ONLY"] = "1"
        sig = {
            "market":   {"ticker": "US.IEI"},
            "decision": {"action": "WATCH_BUY", "confidence": 3, "regime": "neutral_chop"},
        }
        r = top_picks._score_signal(sig)
        self.assertEqual(r["score"], -999.0)
        self.assertTrue(r.get("excluded"))
        self.assertTrue(r.get("soft_blocked"))
        self.assertEqual(r.get("min_confidence_required"), 4)
        self.assertEqual(r.get("min_confidence_canonical"), 7)
        self.assertIn("soft-blocked", r["why"][0])

    def test_soft_blocked_high_conf_scored_normally(self):
        # F02 fix: conf 4 ≥ effective 4 → 穿透
        import os, top_picks
        os.environ["TECHNICAL_ONLY"] = "1"
        sig = {
            "market":   {"ticker": "US.NBIS"},
            "decision": {"action": "BUY", "confidence": 4, "regime": "bull_trending"},
        }
        r = top_picks._score_signal(sig)
        self.assertGreater(r["score"], 0, "conf 4 应通过 soft 过滤并得正 score")
        self.assertFalse(r.get("soft_blocked", False))
        self.assertFalse(r.get("excluded", False))

    def test_non_soft_blocked_ticker_untouched(self):
        # MSFT 在 whitelist, 不应受 soft 影响
        import top_picks
        sig = {
            "market":   {"ticker": "US.MSFT"},
            "decision": {"action": "WATCH_BUY", "confidence": 5, "regime": "bull_trending"},
        }
        r = top_picks._score_signal(sig)
        self.assertGreater(r["score"], 0)
        self.assertFalse(r.get("soft_blocked", False))


class ThesisFilterExecutionGuardTests(unittest.TestCase):
    """P2 integration: 确保 soft-blocked 信号经 _apply_thesis_filter 后, action 已经
    不在 BUY_ACTIONS 里, 上游 orchestrator 就不会调 _place → 也就不会进 cohort_tracker.

    这是防 regression: 若某天有人重构 decision 链路把 filter 放在 place 之后,
    cohort_tracker 会记录 ghost trade (系统实际拒绝的但 log 里假装成交了).
    """

    def setUp(self):
        thesis_config._CACHE = {"mtime": 0, "data": None}

    def test_soft_blocked_action_not_in_buy_actions_after_filter(self):
        # F02 fix: TECHNICAL_ONLY=1 (scale 5), effective_min=4 (from canonical 7/10),
        # conf 3 < 4 应 HOLD
        import os
        os.environ["TECHNICAL_ONLY"] = "1"
        from trading_contracts import BUY_ACTIONS
        decision = {"action": "WATCH_BUY", "confidence": 3, "reason": "trend"}
        out = _apply_thesis_filter(decision, "US.IEI")
        self.assertNotIn(out["action"], BUY_ACTIONS,
                          "soft-blocked signal must not stay in BUY_ACTIONS or _place will fire")
        self.assertEqual(out["action"], "HOLD")

    def test_hard_blocked_action_not_in_buy_actions_after_filter(self):
        from trading_contracts import BUY_ACTIONS
        decision = {"action": "BUY", "confidence": 9, "reason": "breakout"}
        out = _apply_thesis_filter(decision, "US.SOXL")
        self.assertNotIn(out["action"], BUY_ACTIONS)
        self.assertEqual(out["action"], "HOLD")

    def test_soft_blocked_high_conf_stays_in_buy_actions(self):
        # conf ≥ min 时穿透 → action 保留在 BUY_ACTIONS, _place 会跑, cohort 也会记
        from trading_contracts import BUY_ACTIONS
        decision = {"action": "BUY", "confidence": 8, "reason": "strong"}
        out = _apply_thesis_filter(decision, "US.NBIS")
        self.assertIn(out["action"], BUY_ACTIONS,
                        "conf 8 ≥ min 7 应穿透 soft filter, action 保留 BUY")


class SchemaValidationTests(unittest.TestCase):
    """P2 defensive: config 手误 (soft_blacklist 写成 list, entry 缺 min_confidence)
    应 fail-safe, 不 crash."""

    def setUp(self):
        thesis_config._CACHE = {"mtime": 0, "data": None}

    def test_soft_blacklist_as_list_treated_as_empty(self):
        bad_cfg = {"soft_blacklist": ["US.FOO"]}   # 手误: 应是 dict
        with patch.object(thesis_config, "_load", return_value=bad_cfg):
            blocked, _, _ = thesis_config.is_ticker_soft_blacklisted("US.FOO")
            self.assertFalse(blocked, "malformed soft_blacklist 应 fail-safe 而非 crash")

    def test_entry_missing_min_confidence_uses_default_7(self):
        bad_cfg = {"soft_blacklist": {"US.FOO": {"since": "2026-09-01"}}}   # 缺 min_confidence
        with patch.object(thesis_config, "_load", return_value=bad_cfg):
            blocked, _, meta = thesis_config.is_ticker_soft_blacklisted("US.FOO")
            self.assertTrue(blocked)
            self.assertEqual(meta["min_confidence"], 7)

    def test_entry_min_confidence_non_numeric_uses_default(self):
        bad_cfg = {"soft_blacklist": {"US.FOO": {"min_confidence": "high"}}}
        with patch.object(thesis_config, "_load", return_value=bad_cfg):
            blocked, _, meta = thesis_config.is_ticker_soft_blacklisted("US.FOO")
            self.assertTrue(blocked)
            self.assertEqual(meta["min_confidence"], 7)

    def test_entry_as_non_dict_still_blocks_with_default(self):
        # entry 不是 dict (可能是字符串), 仍视为 blocked 用默认 min_conf
        bad_cfg = {"soft_blacklist": {"US.FOO": "some string"}}
        with patch.object(thesis_config, "_load", return_value=bad_cfg):
            blocked, _, meta = thesis_config.is_ticker_soft_blacklisted("US.FOO")
            self.assertTrue(blocked)
            self.assertEqual(meta["min_confidence"], 7)


if __name__ == "__main__":
    unittest.main()
