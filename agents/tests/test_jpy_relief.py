"""JPY appreciation relief shadow signal tests (2026-09-08 Phase A).

锁死:
- _check_jpy_relief 3 tier 分类边界
- 主 signal (_load_asia_repatriation_signal) 行为**不变** — shadow 只 log 不影响
- shadow log 写入
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_rebalance


class JpyReliefClassificationTests(unittest.TestCase):
    """_check_jpy_relief 3 tier 分类边界."""

    def test_strong_relief(self):
        """USDJPY <155 + pullback ≥5% → strong."""
        r = auto_rebalance._check_jpy_relief({
            "usdjpy": 152.0, "usdjpy_60d_high": 163.0,
            "usdjpy_pullback_from_60d_high_pct": -6.75,
        })
        self.assertEqual(r["tier"], "strong")

    def test_partial_relief_pullback_boundary(self):
        """pullback -3% + USDJPY <158 → partial."""
        r = auto_rebalance._check_jpy_relief({
            "usdjpy": 157.0, "usdjpy_60d_high": 161.86,
            "usdjpy_pullback_from_60d_high_pct": -3.0,
        })
        self.assertEqual(r["tier"], "partial")

    def test_partial_boundary_pullback_just_shy_of_5(self):
        """pullback -4.9% + USDJPY 157 → partial (未到 strong 的 -5)."""
        r = auto_rebalance._check_jpy_relief({
            "usdjpy": 157.0, "usdjpy_60d_high": 165.11,
            "usdjpy_pullback_from_60d_high_pct": -4.9,
        })
        self.assertEqual(r["tier"], "partial")

    def test_no_relief_still_high(self):
        """USDJPY 158.5, pullback 大 但 level 仍在干预区 → none.
        strong 需 <155 AND pullback ≥5, partial 需 <158 AND pullback ≥3."""
        r = auto_rebalance._check_jpy_relief({
            "usdjpy": 158.5, "usdjpy_60d_high": 165.0,
            "usdjpy_pullback_from_60d_high_pct": -3.94,
        })
        self.assertEqual(r["tier"], "none")

    def test_no_relief_small_pullback(self):
        r = auto_rebalance._check_jpy_relief({
            "usdjpy": 154.0, "usdjpy_60d_high": 155.5,
            "usdjpy_pullback_from_60d_high_pct": -0.96,
        })
        self.assertEqual(r["tier"], "none")

    def test_no_data_returns_none_tier(self):
        r = auto_rebalance._check_jpy_relief({})
        self.assertEqual(r["tier"], "none")
        self.assertIn("no_data", r["why"])


class MainSignalUnchangedTests(unittest.TestCase):
    """Phase A 严格: shadow log 加入后, _load_asia_repatriation_signal
    主行为**必须不变** (决策不受 relief 影响)."""

    def test_main_signal_still_fires_on_bis_cip_even_with_strong_relief(self):
        """BIS CIP 触发时, 即使有 strong relief (JPY 大跌), 主 signal 仍 True."""
        # 假 cache: hedged 2.4% < JGB 2.67% (BIS CIP fire), 同时 USDJPY 已大幅回落
        fake_cache_data = {
            "data": {
                "macro_context": {
                    "hedged_ust_10y_for_jp": 2.4,
                    "jgb_10y_pct": 2.67,
                    "usdjpy": 152.0,
                    "usdjpy_60d_high": 163.0,
                    "usdjpy_pullback_from_60d_high_pct": -6.75,
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / ".webui_cache"
            cache_dir.mkdir()
            (cache_dir / "bond_monitor_v2.json").write_text(
                json.dumps(fake_cache_data), encoding="utf-8")
            with patch.object(auto_rebalance, "SIGNALS_DIR", str(Path(tmp) / "signals")), \
                 patch.object(auto_rebalance, "_JPY_RELIEF_LOG",
                              Path(tmp) / "shadow.jsonl"):
                (Path(tmp) / "signals").mkdir(exist_ok=True)
                trig, reason = auto_rebalance._load_asia_repatriation_signal()
        self.assertTrue(trig, "P0: relief 不该 override BIS CIP 主信号")
        self.assertIn("BIS CIP", reason)

    def test_main_signal_none_when_no_trigger_regardless_of_relief(self):
        """无 BIS CIP + USDJPY <160 → 主信号 False (不因 relief tier 改变)."""
        fake_cache_data = {
            "data": {
                "macro_context": {
                    "hedged_ust_10y_for_jp": 3.0,  # > jgb, no BIS trigger
                    "jgb_10y_pct": 2.67,
                    "usdjpy": 152.0,
                    "usdjpy_60d_high": 163.0,
                    "usdjpy_pullback_from_60d_high_pct": -6.75,
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / ".webui_cache"
            cache_dir.mkdir()
            (cache_dir / "bond_monitor_v2.json").write_text(
                json.dumps(fake_cache_data), encoding="utf-8")
            with patch.object(auto_rebalance, "SIGNALS_DIR", str(Path(tmp) / "signals")), \
                 patch.object(auto_rebalance, "_JPY_RELIEF_LOG",
                              Path(tmp) / "shadow.jsonl"):
                (Path(tmp) / "signals").mkdir(exist_ok=True)
                trig, reason = auto_rebalance._load_asia_repatriation_signal()
        self.assertFalse(trig)


class ShadowLogWritesTests(unittest.TestCase):

    def test_shadow_log_writes_entry_on_signal_check(self):
        fake_cache_data = {
            "data": {
                "macro_context": {
                    "hedged_ust_10y_for_jp": 2.4,
                    "jgb_10y_pct": 2.67,
                    "usdjpy": 154.0,
                    "usdjpy_60d_high": 163.0,
                    "usdjpy_pullback_from_60d_high_pct": -5.5,
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / ".webui_cache"
            cache_dir.mkdir()
            (cache_dir / "bond_monitor_v2.json").write_text(
                json.dumps(fake_cache_data), encoding="utf-8")
            shadow_log = Path(tmp) / "shadow.jsonl"
            with patch.object(auto_rebalance, "SIGNALS_DIR", str(Path(tmp) / "signals")), \
                 patch.object(auto_rebalance, "_JPY_RELIEF_LOG", shadow_log):
                (Path(tmp) / "signals").mkdir(exist_ok=True)
                auto_rebalance._load_asia_repatriation_signal()
            self.assertTrue(shadow_log.exists(),
                            "Phase A shadow log 未写入")
            entry = json.loads(shadow_log.read_text(encoding="utf-8").strip())
            self.assertEqual(entry["relief_tier"], "strong")
            self.assertEqual(entry["usdjpy"], 154.0)
            self.assertEqual(entry["hedged_ust_10y"], 2.4)


if __name__ == "__main__":
    unittest.main()
