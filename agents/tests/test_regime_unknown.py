"""F08 regression (audit 2026-09-19): 数据缺失时 regime 应返 'unknown', 不假装 neutral.

之前 bug: get_today_regime() 无 state / 过期时 fallback 'neutral', downstream
无法区分"数据缺失"和"真实中性" → 关键路径无法采取更保守的默认策略.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import regime_today


class GetTodayRegimeFallbackTests(unittest.TestCase):

    def test_missing_state_returns_unknown_not_neutral(self):
        with patch.object(regime_today, "_load_state", return_value=None):
            r = regime_today.get_today_regime()
            self.assertEqual(r, "unknown",
                              "F08 fix: 数据缺失应返 'unknown', 别当真中性")

    def test_explicit_default_neutral_kept_for_legacy_callers(self):
        # 旧代码若显式想要 'neutral' 兜底可以显式传参保持行为
        with patch.object(regime_today, "_load_state", return_value=None):
            r = regime_today.get_today_regime(default="neutral")
            self.assertEqual(r, "neutral")

    def test_real_neutral_state_still_returns_neutral(self):
        with patch.object(regime_today, "_load_state",
                            return_value={"regime": "neutral", "date": "2026-09-19"}):
            r = regime_today.get_today_regime()
            self.assertEqual(r, "neutral",
                              "真 neutral state 仍返 neutral, 不改变")

    def test_bull_regime_unaffected(self):
        with patch.object(regime_today, "_load_state",
                            return_value={"regime": "bull_trending", "date": "2026-09-19"}):
            self.assertEqual(regime_today.get_today_regime(), "bull_trending")

    def test_empty_regime_string_returns_default(self):
        # state 存在但 regime 字段空 / None → 也走 default
        with patch.object(regime_today, "_load_state",
                            return_value={"regime": None, "date": "2026-09-19"}):
            self.assertEqual(regime_today.get_today_regime(), "unknown")
        with patch.object(regime_today, "_load_state",
                            return_value={"regime": "", "date": "2026-09-19"}):
            self.assertEqual(regime_today.get_today_regime(), "unknown")


class UnknownLabelExposedTests(unittest.TestCase):

    def test_unknown_label_defined(self):
        self.assertIn("unknown", regime_today._REGIME_LABEL,
                        "unknown label 必须在 _REGIME_LABEL 里, dashboard 才能展示")
        self.assertIn("数据缺失", regime_today._REGIME_LABEL["unknown"],
                        "unknown label 应明确说明是数据缺失不是真中性")


if __name__ == "__main__":
    unittest.main()
