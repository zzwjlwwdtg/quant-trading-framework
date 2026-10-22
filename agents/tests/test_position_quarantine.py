"""2026-10-22: delisted ticker placeholder quarantine (QRVO -> US.0000).

The broker keeps a delisted security as an unknown code with qty and zero
price. It must not become a normal trading state, must not arm a stop, and
must not enter the scan/universe. The broker position itself is untouched;
only the local state and read paths are quarantined.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paper_trader as pt
from moomoo import RET_OK


def _pos(qty, avg, stock_name="", nominal=0.0):
    return {"qty": qty, "can_sell_qty": qty, "avg_cost": avg,
            "cost_price": avg, "nominal_price": nominal,
            "stock_name": stock_name}


class _FakeCtx:
    def __init__(self, rows):
        self._rows = rows

    def position_list_query(self, **kwargs):
        return RET_OK, pd.DataFrame(self._rows)


class QuarantinePredicate(unittest.TestCase):
    def test_unknown_placeholder_is_detected(self):
        self.assertTrue(pt._is_unknown_broker_code("US.0000"))
        self.assertTrue(pt._is_unknown_broker_code("US.QRVO", "未知股票"))
        self.assertFalse(pt._is_unknown_broker_code("US.TQQQ", "三倍做多纳指ETF"))
        self.assertFalse(pt._is_unknown_broker_code("", ""))

    def test_quarantine_flag(self):
        self.assertTrue(pt._is_quarantined({"quarantined": True}))
        self.assertTrue(pt._is_quarantined({"pending_corporate_action": True}))
        self.assertFalse(pt._is_quarantined({}))
        self.assertFalse(pt._is_quarantined(None))


class ReconcileQuarantine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_path = self.tmp / "trader_state.json"
        self.state = {
            "US.0000": {
                "entry_qty": 93, "entry_price": 115.34, "entry_high": 115.34,
                "protective_stop_price": 109.57,
                "protective_stop_mode": "software",
                "protective_stop_order_id": None,
            },
            "US.TQQQ": {"entry_qty": 286, "entry_price": 67.878,
                        "entry_high": 71.72, "tp_levels_hit": []},
        }
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def _run(self, positions, pending=()):
        pending_value = None if pending is None else set(pending)
        with patch.object(pt, "STATE_PATH", self.state_path), \
             patch.object(pt, "_broker_position_truth", return_value=positions), \
             patch.object(pt, "_open_order_tickers", return_value=pending_value):
            return pt.reconcile_state_with_broker(apply=True, reason="test")

    def _saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_unknown_position_is_quarantined_without_stop(self):
        positions = {
            "US.0000": _pos(93, 115.34, stock_name="未知股票"),
            "US.TQQQ": _pos(296, 67.878, nominal=72.61),
        }
        rep = self._run(positions)
        self.assertEqual(rep["status"], "applied")
        saved = self._saved()
        q = saved["US.0000"]
        self.assertTrue(q["quarantined"])
        self.assertTrue(q["pending_corporate_action"])
        self.assertEqual(q["protective_stop_mode"], "quarantined")
        self.assertNotIn("protective_stop_price", q)
        self.assertNotIn("protective_stop_order_id", q)
        self.assertEqual(q["entry_qty"], 93)
        # Normal reconciliation still runs for the other ticker.
        self.assertEqual(saved["US.TQQQ"]["entry_qty"], 296)

        again = self._run(positions)
        self.assertEqual(again["status"], "no_changes")

    def test_existing_quarantine_details_survive_stop_cleanup(self):
        state = self._saved()
        state["US.0000"].update({
            "quarantined": True,
            "pending_corporate_action": True,
            "quarantine_reason": "QRVO delisted 2026-10-05 after Skyworks merger",
            "quarantined_at_utc": "2026-10-22T04:50:00+00:00",
            "estimated_consideration_usd": 10281.0,
        })
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        rep = self._run({"US.0000": _pos(93, 115.34, stock_name="未知股票")})
        saved = self._saved()["US.0000"]
        self.assertEqual(saved["quarantine_reason"],
                         "QRVO delisted 2026-10-05 after Skyworks merger")
        self.assertEqual(saved["estimated_consideration_usd"], 10281.0)
        self.assertNotIn("protective_stop_price", saved)
        self.assertEqual(saved["protective_stop_mode"], "quarantined")
        self.assertEqual(rep["status"], "applied")


class ScanExclusion(unittest.TestCase):
    def test_satellite_positions_skip_quarantined_and_unknown(self):
        rows = [
            {"code": "US.0000", "can_sell_qty": 93, "nominal_price": 0,
             "stock_name": "未知股票"},
            {"code": "US.DEF", "can_sell_qty": 5, "nominal_price": 20,
             "stock_name": "DEF Corp"},
            {"code": "US.ABC", "can_sell_qty": 10, "nominal_price": 50,
             "stock_name": "ABC Corp"},
        ]
        state = {"US.DEF": {"quarantined": True}}
        with patch.object(pt, "_ctx_get", return_value=_FakeCtx(rows)), \
             patch.object(pt, "_state_load", return_value=state):
            out = pt._list_satellite_positions()
        self.assertEqual([p["code"] for p in out], ["US.ABC"])

    def test_get_active_tickers_excludes_unknown_position(self):
        rows = [{"code": "US.0000", "can_sell_qty": 93, "nominal_price": 0,
                 "stock_name": "未知股票"}]
        with patch.object(pt, "_ctx_get", return_value=_FakeCtx(rows)), \
             patch.object(pt, "_state_load", return_value={}), \
             patch.object(pt, "_universe_load", return_value={"picks": []}):
            active = pt.get_active_tickers()
        self.assertNotIn("US.0000", active)


class StopMonitorQuarantine(unittest.TestCase):
    def test_quarantined_position_is_not_sold(self):
        state = {"US.0000": {
            "entry_qty": 93,
            "protective_stop_mode": "software",
            "protective_stop_price": 109.57,
            "quarantined": True,
        }}
        with patch.object(pt, "_state_load", return_value=state), \
             patch.object(pt, "_position_snapshot",
                          return_value={"position_qty": 93,
                                        "can_sell_qty": 93,
                                        "nominal_price": 80.0}), \
             patch.object(pt, "_place") as place, \
             patch.object(pt, "_state_save") as save:
            triggered = pt._monitor_software_stops_unlocked()
        self.assertEqual(triggered, [])
        place.assert_not_called()
        save.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
