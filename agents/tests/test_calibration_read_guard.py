"""2026-10-20 review: warn on read when live calibration disables sells.

The writer guard only rejects future degenerate files; the already-live file
has all-zero bear weights, so the read path must surface it without changing
any weight.  Replacement requires the pre-registered bear-market study.
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

import decision_agent as da


def _calibration(bear_weight: float) -> dict:
    def side(weight):
        return {"trend_down": {"fires": 10, "hits": 6, "hit_rate": 0.6,
                               "weight": weight}}

    return {
        "bull_weights": side(1.0),
        "bull_percentiles": {"p20": 1, "p40": 2, "p60": 3, "p80": 4},
        "bear_weights": side(bear_weight),
        "bear_percentiles": {"p20": 1, "p40": 2, "p60": 3, "p80": 4},
        "per_class": {},
    }


class CalibrationReadGuard(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "confidence_calibration.json"
        self.orig_cache = dict(da._CALIB_CACHE)
        da._CALIB_CACHE.update(loaded=False, data=None)

    def tearDown(self):
        da._CALIB_CACHE.clear()
        da._CALIB_CACHE.update(self.orig_cache)

    def _load(self, cal):
        self.path.write_text(json.dumps(cal), encoding="utf-8")
        with patch.object(da, "_CALIB_PATH", self.path):
            with self.assertLogs("agents", level="WARNING") as logs:
                data = da._load_calibration()
        return data, logs.output

    def test_all_zero_bear_weights_warn_on_read(self):
        data, output = self._load(_calibration(0.0))
        self.assertIsNotNone(data)
        self.assertIn("all bear weights are zero", "\n".join(output))

    def test_nonzero_bear_weight_is_silent(self):
        self.path.write_text(json.dumps(_calibration(0.8)), encoding="utf-8")
        with patch.object(da, "_CALIB_PATH", self.path), \
             self.assertNoLogs("agents", level="WARNING"):
            self.assertIsNotNone(da._load_calibration())

    def test_per_class_zero_bear_weights_are_named(self):
        cal = _calibration(0.8)
        cal["per_class"] = {"equity_leveraged": {
            "bear_weights": _calibration(0.0)["bear_weights"]}}
        _, output = self._load(cal)
        self.assertIn("equity_leveraged", "\n".join(output))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
