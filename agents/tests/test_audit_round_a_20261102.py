"""2026-11-02 A 组: 审计 P2-7 / P2-8 / P2-13 / P2-20 与止损线检查改用实时价."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import atomic_io
import decision_agent as da
import news_analyzer
import paper_trader as pt


class UncertainGuardKeepsContext(unittest.TestCase):
    def test_demoted_hold_keeps_regime_engine_confluence(self):
        res = {"action": "BUY", "confidence": 5, "regime": "crisis", "engine": "rules",
               "confluence": {"bull": 2}, "stop_ref": 10.0, "score_breakdown": {"x": 1}}
        with patch.object(da, "_is_technical_only", return_value=False):
            out = da._apply_uncertain_guard(res, ev=3)
        self.assertEqual(out["action"], "HOLD")
        self.assertEqual(out["regime"], "crisis")
        self.assertEqual(out["engine"], "rules")
        self.assertEqual(out["confluence"], {"bull": 2})
        self.assertIsNone(out["stop_ref"])
        self.assertEqual(out["demoted_from"], "BUY")


class StateSideTests(unittest.TestCase):
    def test_trailing_stop_and_tp_record_sell_side(self):
        src = Path(pt.__file__).read_text(encoding="utf-8")
        self.assertIn('"last_action":"TRAILING_STOP","last_side":"SELL"', src)
        self.assertIn('"last_action":f"TP_{label}","last_side":"SELL"', src)

    def test_stop_line_final_check_uses_pre_buffer_price(self):
        src = Path(pt.__file__).read_text(encoding="utf-8")
        self.assertIn("_chk_price = min(ref_price, order_price)", src)
        self.assertIn("stop_line_guard.check_buy(code, _chk_price, tag)", src)


class AtomicReplaceRetry(unittest.TestCase):
    def test_transient_permission_error_is_retried(self):
        d = Path(tempfile.mkdtemp())
        real = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("WinError 32")
            return real(a, b)
        with patch.object(atomic_io.os, "replace", side_effect=flaky), \
             patch.object(atomic_io.time, "sleep"):
            atomic_io.atomic_write_json(d / "x.json", {"a": 1})
        self.assertEqual(calls["n"], 3)
        self.assertIn('"a": 1', (d / "x.json").read_text(encoding="utf-8"))

    def test_persistent_error_still_raises(self):
        d = Path(tempfile.mkdtemp())
        with patch.object(atomic_io.os, "replace", side_effect=PermissionError("locked")), \
             patch.object(atomic_io.time, "sleep"):
            with self.assertRaises(PermissionError):
                atomic_io.atomic_write_json(d / "y.json", {"a": 1})
        self.assertEqual([p for p in d.iterdir() if p.suffix == ".tmp"], [])


class IsLandedParse(unittest.TestCase):
    def test_string_false_is_false(self):
        for v in ("false", "False", "no", "0", "", None, 0):
            self.assertFalse(news_analyzer._as_bool(v), v)
        for v in (True, "true", "YES", 1, "1"):
            self.assertTrue(news_analyzer._as_bool(v), v)


if __name__ == "__main__":
    raise SystemExit("use: python -m unittest discover -s tests -t .")
