"""2026-10-20 P0 守卫: 全零 bear 权重 / 塌缩分位的校准结果不得落盘.

2025-26 上涨窗口里 13 条 bear 规则 5 日命中率全部 <50%, 权重被写成 0,
技术性减仓能力被静默关闭. 在预注册的跨周期研究完成前, 自动校准只允许
保留旧文件并告警, 不允许用退化结果覆盖.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _calibrate_confidence as cal


def _weights(value: float) -> dict:
    return {"trend": {"weight": value}, "ma_stack": {"weight": value}}


def _pcts(values) -> dict:
    return dict(zip(("p20", "p40", "p60", "p80"), values))


def _calib(*, bull=0.6, bear=0.4,
           bull_pcts=(0.5, 1.0, 1.5, 2.0),
           bear_pcts=(0.2, 0.4, 0.6, 0.8),
           per_class=None) -> dict:
    return {
        "ts": "2026-10-20T00:00:00",
        "bull_weights": _weights(bull),
        "bear_weights": _weights(bear),
        "bull_percentiles": _pcts(bull_pcts),
        "bear_percentiles": _pcts(bear_pcts),
        "per_class": per_class or {},
    }


class ValidateCalibration(unittest.TestCase):
    def test_valid_calibration_passes(self):
        self.assertEqual(cal.validate_calibration(_calib()), [])

    def test_all_zero_bear_side_rejected(self):
        problems = cal.validate_calibration(
            _calib(bear=0.0, bear_pcts=(0.0, 0.0, 0.0, 0.0)))
        self.assertTrue(any("default bear" in p and "zero" in p for p in problems))

    def test_collapsed_percentiles_rejected(self):
        problems = cal.validate_calibration(
            _calib(bull_pcts=(1.0, 1.0, 1.0, 1.0)))
        self.assertTrue(any("default bull" in p and "degenerate" in p
                            for p in problems))

    def test_top_percentile_zero_rejected(self):
        problems = cal.validate_calibration(
            _calib(bear_pcts=(0.0, 0.0, 0.0, 0.0)))
        self.assertTrue(any("default bear" in p for p in problems))

    def test_degenerate_per_class_rejected(self):
        bad_class = {
            "equity_leveraged": {
                "bull_weights": _weights(0.6),
                "bear_weights": _weights(0.0),
                "bull_percentiles": _pcts((0.5, 1.0, 1.5, 2.0)),
                "bear_percentiles": _pcts((0.0, 0.0, 0.0, 0.0)),
            }
        }
        problems = cal.validate_calibration(_calib(per_class=bad_class))
        self.assertTrue(any("equity_leveraged bear" in p for p in problems))


class MainWriteGuard(unittest.TestCase):
    def test_rejection_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "confidence_calibration.json"
            path.write_text('{"KEEP": true}', encoding="utf-8")
            with patch.object(cal, "CALIB_PATH", path), \
                 patch.object(cal, "SIGNALS_DIR", tmp), \
                 patch.object(cal, "calibrate", return_value=_calib(
                     bear=0.0, bear_pcts=(0.0, 0.0, 0.0, 0.0))), \
                 patch.object(cal, "_print_report"), \
                 patch.object(cal, "_alert_rejected") as alert, \
                 patch.object(sys, "argv", ["_calibrate_confidence.py"]):
                rc = cal.main()
            self.assertEqual(rc, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), '{"KEEP": true}')
            self.assertTrue(alert.called)

    def test_valid_result_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "confidence_calibration.json"
            good = _calib()
            with patch.object(cal, "CALIB_PATH", path), \
                 patch.object(cal, "SIGNALS_DIR", tmp), \
                 patch.object(cal, "calibrate", return_value=good), \
                 patch.object(cal, "_print_report"), \
                 patch("builtins.print"), \
                 patch.object(sys, "argv", ["_calibrate_confidence.py"]):
                rc = cal.main()
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), good)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
