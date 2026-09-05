"""Tests for overnight_signal classifier — 6 类边界 + shadow log 写入."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import overnight_signal
from overnight_signal import classify, log_shadow, CLASSIFICATION_META


class ClassifyBoundaryTests(unittest.TestCase):
    """按 6 类分类, 每类至少 2 case (触发 + 边界不触发)."""

    def test_panic_gap_down_triggers_below_minus3(self):
        r = classify(pre_pct=-3.5, action="HOLD")
        self.assertEqual(r["classification"], "panic_gap_down")
        # 独立于 action, 即使不是 BUY 也算
        r2 = classify(pre_pct=-3.01, action="")
        self.assertEqual(r2["classification"], "panic_gap_down")

    def test_panic_gap_down_boundary_not_triggered(self):
        # -3.0 = 边界, 不触发 (严格 <)
        r = classify(pre_pct=-2.99, action="HOLD")
        self.assertNotEqual(r["classification"], "panic_gap_down")

    def test_chase_risk_triggers_above_plus2_with_buy(self):
        r = classify(pre_pct=2.5, action="WATCH_BUY")
        self.assertEqual(r["classification"], "chase_risk")
        r2 = classify(pre_pct=3.0, action="BUY")
        self.assertEqual(r2["classification"], "chase_risk")

    def test_chase_risk_no_buy_action_falls_through(self):
        # 盘前 +2.5% 但 action=HOLD, 不算 chase_risk (无 BUY 意图哪来追高)
        r = classify(pre_pct=2.5, action="HOLD")
        self.assertNotEqual(r["classification"], "chase_risk")

    def test_buy_dip_triggers_in_range(self):
        r = classify(pre_pct=-1.5, action="WATCH_BUY", rsi=45)
        self.assertEqual(r["classification"], "buy_dip")

    def test_buy_dip_boundary_upper(self):
        # -0.5 = 边界上限
        r = classify(pre_pct=-0.5, action="BUY", rsi=50)
        self.assertEqual(r["classification"], "buy_dip")

    def test_buy_dip_boundary_lower(self):
        # -3 = 边界下限
        r = classify(pre_pct=-3.0, action="BUY", rsi=50)
        self.assertEqual(r["classification"], "buy_dip")

    def test_buy_dip_rsi_too_high_falls_through(self):
        # RSI ≥60 → 不算逢低 (已经超买位)
        r = classify(pre_pct=-1.5, action="WATCH_BUY", rsi=65)
        self.assertNotEqual(r["classification"], "buy_dip")

    def test_buy_dip_no_buy_action(self):
        r = classify(pre_pct=-1.5, action="HOLD", rsi=45)
        self.assertNotEqual(r["classification"], "buy_dip")

    def test_momentum_triggers_with_high_volume(self):
        r = classify(pre_pct=1.0, action="WATCH_BUY",
                     pre_vol=5_000_000, pre_vol_avg=1_000_000)
        self.assertEqual(r["classification"], "momentum")

    def test_momentum_volume_none_still_ok(self):
        # 无量数据时仍归 momentum (avoid over-filtering)
        r = classify(pre_pct=1.0, action="WATCH_BUY")
        self.assertEqual(r["classification"], "momentum")

    def test_momentum_low_volume_falls_through(self):
        r = classify(pre_pct=1.0, action="WATCH_BUY",
                     pre_vol=500_000, pre_vol_avg=1_000_000)
        self.assertNotEqual(r["classification"], "momentum")

    def test_reversal_setup_triggers(self):
        # 昨盘后 +1.5%, 隔夜 -0.8%, 盘前 -0.3% → reversal_setup
        r = classify(after_pct=1.5, overnight_pct=-0.8, pre_pct=-0.3)
        self.assertEqual(r["classification"], "reversal_setup")

    def test_reversal_setup_needs_all_three_fields(self):
        # 缺任一都不触发
        r = classify(after_pct=1.5, overnight_pct=-0.8)
        self.assertNotEqual(r["classification"], "reversal_setup")

    def test_neutral_fallback(self):
        # 盘前 0, 无 setup
        r = classify(pre_pct=0.1, action="HOLD")
        self.assertEqual(r["classification"], "neutral")

    def test_no_data_returns_neutral_no_data(self):
        r = classify()
        self.assertEqual(r["classification"], "neutral")
        self.assertFalse(r["has_data"])

    def test_all_classifications_have_meta(self):
        # 每个 classification 都必须在 CLASSIFICATION_META
        for cls in ["buy_dip", "momentum", "chase_risk",
                    "panic_gap_down", "reversal_setup", "neutral"]:
            self.assertIn(cls, CLASSIFICATION_META)
            self.assertIn("zh", CLASSIFICATION_META[cls])
            self.assertIn("action_hint", CLASSIFICATION_META[cls])

    def test_priority_panic_over_chase(self):
        # 极端 case: 盘前 -3.5% + WATCH_BUY. 应该 panic_gap_down 优先, 不 chase
        r = classify(pre_pct=-3.5, action="WATCH_BUY")
        self.assertEqual(r["classification"], "panic_gap_down")


class ShadowLogTests(unittest.TestCase):

    def test_log_shadow_writes_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_log = Path(tmp) / "overnight_signal_log.jsonl"
            with patch.object(overnight_signal, "_SHADOW_LOG", fake_log):
                info = {"classification": "buy_dip",
                        "why": ["test"], "confidence": 4,
                        "has_data": True}
                log_shadow("US.TQQQ", info, market_snapshot={"price": 100, "pre_pct": -1.5})
            self.assertTrue(fake_log.exists())
            content = fake_log.read_text(encoding="utf-8")
            self.assertIn("US.TQQQ", content)
            self.assertIn("buy_dip", content)
            # 有效 JSON
            entry = json.loads(content.strip())
            self.assertEqual(entry["ticker"], "US.TQQQ")
            self.assertEqual(entry["snapshot"]["price"], 100)

    def test_log_shadow_skips_when_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_log = Path(tmp) / "overnight_signal_log.jsonl"
            with patch.object(overnight_signal, "_SHADOW_LOG", fake_log):
                info = {"classification": "neutral", "has_data": False}
                log_shadow("US.TQQQ", info)
            # has_data=False 时不写 log (无信息量, 省空间)
            self.assertFalse(fake_log.exists())


if __name__ == "__main__":
    unittest.main()
