"""F05 regression (audit 2026-09-19): backtest 不应读当前 thesis / LLM.

BACKTEST_MODE=1 应该:
- 跳过 _apply_thesis_filter (blacklist/soft_blacklist 是当前状态, 会 look-ahead 历史)
- 跳过 _llm_call (LLM 结果不可重放, AI 版本没冻结)
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class ThesisFilterBacktestBypassTests(unittest.TestCase):
    """patch.dict scopes BACKTEST_MODE env change per-test, prevents leakage
    that would fail other test files running in the same session."""

    def test_backtest_mode_bypasses_hard_blacklist(self):
        with patch.dict(os.environ, {"BACKTEST_MODE": "1"}):
            from decision_agent import _apply_thesis_filter
            decision = {"action": "BUY", "confidence": 8, "reason": "test"}
            out = _apply_thesis_filter(decision, "US.SOXL")
            self.assertEqual(out["action"], "BUY",
                              "BACKTEST_MODE=1 应绕过 hard blacklist")
            self.assertFalse(out.get("thesis_blocked", False))

    def test_backtest_mode_bypasses_soft_blacklist(self):
        with patch.dict(os.environ, {"BACKTEST_MODE": "1"}):
            from decision_agent import _apply_thesis_filter
            decision = {"action": "WATCH_BUY", "confidence": 3, "reason": "test"}
            out = _apply_thesis_filter(decision, "US.IEI")
            self.assertEqual(out["action"], "WATCH_BUY")
            self.assertFalse(out.get("thesis_soft_blocked", False))

    def test_normal_mode_still_applies_filter(self):
        # patch.dict with clear=False, then pop to simulate 无 BACKTEST_MODE
        env_no_bt = {k: v for k, v in os.environ.items() if k != "BACKTEST_MODE"}
        with patch.dict(os.environ, env_no_bt, clear=True):
            import thesis_config
            thesis_config._CACHE = {"mtime": 0, "data": None}
            from decision_agent import _apply_thesis_filter
            decision = {"action": "BUY", "confidence": 8, "reason": "test"}
            out = _apply_thesis_filter(decision, "US.SOXL")
            self.assertEqual(out["action"], "HOLD",
                              "无 BACKTEST_MODE 时 hard blacklist 仍生效")
            self.assertTrue(out.get("thesis_blocked"))


class LLMCallBacktestBypassTests(unittest.TestCase):

    def test_llm_call_returns_none_in_backtest_mode(self):
        with patch.dict(os.environ, {"BACKTEST_MODE": "1"}):
            from decision_agent import _llm_call
            r = _llm_call("test_sys", {"price": 100}, {}, {}, ("price",), ())
            self.assertIsNone(r, "BACKTEST_MODE=1 时 LLM 必须被禁")


if __name__ == "__main__":
    unittest.main()
