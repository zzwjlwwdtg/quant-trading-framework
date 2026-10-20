"""2026-10-20: trader_state 与券商持仓对账.

漏记成交 + 现金补足卖出会让 entry_qty / entry_price 漂移 (10/16 TQQQ 286 vs
296, DRAM 346 vs 366, IEI 2198 vs 4005). 修复必须:
- 默认 dry-run, 不写文件; apply 前先备份.
- 持仓数量以券商为准; TP 已触发过的标的不改原始 entry_qty.
- 无保护的持仓补一条 software stop, 但不得高于现价.
- 已平仓标的清掉过期执行元数据, 有未结委托的除外.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paper_trader as pt


def _pos(qty, avg, cost=None, nominal=0.0):
    return {"qty": qty, "can_sell_qty": qty, "avg_cost": avg,
            "cost_price": avg if cost is None else cost,
            "nominal_price": nominal}


class StateReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_path = self.tmp / "trader_state.json"
        self.state = {
            "US.TQQQ": {"entry_qty": 286, "entry_price": 70.14,
                        "entry_high": 71.72, "tp_levels_hit": [],
                        "protective_stop_price": 57.68,
                        "protective_stop_mode": "software"},
            "US.MSFT": {"entry_qty": 32, "entry_price": 504.55,
                        "entry_high": 550.25, "tp_levels_hit": []},
            "US.QRVO": {"entry_qty": 313, "entry_price": 98.48,
                        "entry_high": 120.29, "tp_levels_hit": ["tp15"],
                        "protective_stop_price": 108.27,
                        "protective_stop_mode": "software"},
            "US.MULL": {"last_action": "TRAILING_STOP", "last_side": "BUY",
                        "last_qty": 462, "last_order_id": "474278",
                        "entry_qty": 462, "entry_price": 18.55,
                        "protective_stop_price": 17.0,
                        "protective_stop_mode": "software"},
            "US.ENPH": {"last_seen_in_picks_utc": "2026-06-05T13:28:39Z"},
        }
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def _run(self, positions, pending=(), apply=False, repair_price=True):
        pending_value = None if pending is None else set(pending)
        with patch.object(pt, "STATE_PATH", self.state_path), \
             patch.object(pt, "_broker_position_truth", return_value=positions), \
             patch.object(pt, "_open_order_tickers", return_value=pending_value):
            return pt.reconcile_state_with_broker(
                apply=apply, repair_price=repair_price, reason="test")

    def _saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_dry_run_diff_fixes_quantities_and_arms_stop(self):
        positions = {
            "US.TQQQ": _pos(296, 67.878, nominal=70.64),
            "US.MSFT": _pos(46, 497.072, cost=444.551, nominal=533.52),
            "US.QRVO": _pos(93, 115.34, nominal=114.17),
        }
        rep = self._run(positions)
        self.assertEqual(rep["status"], "dry_run")
        by_ticker = {c["ticker"]: c for c in rep["changes"]}
        self.assertEqual(by_ticker["US.TQQQ"]["fields"]["entry_qty"]["after"], 296)
        self.assertAlmostEqual(
            by_ticker["US.TQQQ"]["fields"]["entry_price"]["after"], 67.878)
        self.assertEqual(by_ticker["US.MSFT"]["fields"]["entry_qty"]["after"], 46)
        self.assertAlmostEqual(
            by_ticker["US.MSFT"]["fields"]["entry_price"]["after"], 497.072)
        # 550.25 × 0.92 = 506.23 比 cost×0.95 更紧, 且低于现价 2% 以上
        self.assertAlmostEqual(
            by_ticker["US.MSFT"]["fields"]["protective_stop"]["after"], 506.23)
        # TP 已触发: 原始 lot 313 保留
        self.assertNotIn("entry_qty", by_ticker["US.QRVO"]["fields"])
        self.assertEqual(by_ticker["US.QRVO"]["position_qty"], 93)
        self.assertIn("US.MULL", by_ticker)
        self.assertIn("cleared", by_ticker["US.MULL"])
        # dry-run 不写文件
        self.assertEqual(self._saved(), self.state)

    def test_apply_writes_backup_and_is_idempotent(self):
        positions = {
            "US.TQQQ": _pos(296, 67.878, nominal=70.64),
            "US.MSFT": _pos(46, 497.072, nominal=533.52),
            "US.QRVO": _pos(93, 115.34, nominal=114.17),
        }
        rep = self._run(positions, apply=True)
        self.assertEqual(rep["status"], "applied")
        self.assertTrue(rep["backup"] and Path(rep["backup"]).exists())
        self.assertEqual(json.loads(Path(rep["backup"]).read_text(encoding="utf-8")),
                         self.state)
        saved = self._saved()
        self.assertEqual(saved["US.TQQQ"]["entry_qty"], 296)
        self.assertAlmostEqual(saved["US.TQQQ"]["entry_price"], 67.878)
        self.assertAlmostEqual(saved["US.MSFT"]["protective_stop_price"], 506.23)
        self.assertEqual(saved["US.MSFT"]["protective_stop_mode"], "software")
        self.assertEqual(saved["US.MULL"]["last_action"], "TRAILING_STOP")
        self.assertEqual(saved["US.MULL"]["last_order_id"], "474278")
        self.assertNotIn("entry_price", saved["US.MULL"])
        self.assertNotIn("protective_stop_price", saved["US.MULL"])
        self.assertEqual(saved["US.ENPH"]["last_seen_in_picks_utc"],
                         "2026-06-05T13:28:39Z")
        again = self._run(positions, apply=True)
        self.assertEqual(again["status"], "no_changes")

    def test_flat_with_open_order_is_not_cleared(self):
        positions = {}
        state = self._saved()
        state["US.NVDA"] = {"last_action": "TRAILING_STOP", "last_order_id": "474784",
                            "entry_qty": 152, "entry_price": 214.4}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        rep = self._run(positions, pending={"US.NVDA"}, apply=True)
        saved = self._saved()
        self.assertIn("last_action", saved["US.NVDA"])
        self.assertIn("last_action", saved["US.MULL"])
        self.assertNotIn("entry_price", saved["US.MULL"])
        self.assertNotIn("US.NVDA", {c["ticker"] for c in rep["changes"]})

    def test_flat_cleanup_keeps_window_marker_and_blocks_rebuy(self):
        window_key = f"{datetime.now(timezone.utc).date().isoformat()}:post-open"
        state = self._saved()
        state["US.TQQQ"] = {
            "last_window_key": window_key, "last_action": "TAKE_PROFIT",
            "last_side": "SELL", "last_qty": 296,
            "last_time_utc": "2026-10-20T03:00:00+00:00",
            "last_order_id": "474999",
            "entry_price": 67.878, "entry_qty": 296, "entry_high": 71.72,
            "protective_stop_price": 60.0, "protective_stop_mode": "software",
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self._run({}, pending=set(), apply=True)
        saved = self._saved()
        tstate = saved["US.TQQQ"]
        self.assertEqual(tstate["last_window_key"], window_key)
        self.assertEqual(tstate["last_action"], "TAKE_PROFIT")
        self.assertEqual(tstate["last_order_id"], "474999")
        self.assertNotIn("entry_price", tstate)
        self.assertNotIn("protective_stop_price", tstate)
        # A restart in the same window must not re-enter after the flat exit.
        with patch.object(pt, "_position_qty", return_value=0), \
             patch.object(pt, "_state_load", return_value=saved), \
             patch.object(pt, "_state_save"), \
             patch.object(pt, "_place") as place:
            pt.execute("US.TQQQ", {"action": "BUY", "confidence": 5},
                       {"price": 70.0}, "post-open")
        place.assert_not_called()

    def test_pending_query_failure_blocks_flat_cleanup(self):
        positions = {}
        rep = self._run(positions, pending=None, apply=True)
        self.assertNotIn("US.MULL", {c["ticker"] for c in rep["changes"]})
        self.assertIn("last_action", self._saved()["US.MULL"])

    def test_price_repair_can_be_disabled_for_live_sync(self):
        positions = {"US.TQQQ": _pos(296, 67.878, nominal=70.64)}
        rep = self._run(positions, repair_price=False)
        by_ticker = {c["ticker"]: c for c in rep["changes"]}
        self.assertEqual(by_ticker["US.TQQQ"]["fields"]["entry_qty"]["after"], 296)
        self.assertNotIn("entry_price", by_ticker["US.TQQQ"]["fields"])

    def test_missing_price_is_filled_even_without_price_repair(self):
        state = self._saved()
        state["US.TQQQ"].pop("entry_price")
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        positions = {"US.TQQQ": _pos(296, 67.878, nominal=70.64)}
        rep = self._run(positions, repair_price=False)
        by_ticker = {c["ticker"]: c for c in rep["changes"]}
        self.assertAlmostEqual(
            by_ticker["US.TQQQ"]["fields"]["entry_price"]["after"], 67.878)

    def test_unavailable_positions_never_write(self):
        with patch.object(pt, "STATE_PATH", self.state_path), \
             patch.object(pt, "_broker_position_truth", return_value=None):
            rep = pt.reconcile_state_with_broker(apply=True, reason="test")
        self.assertEqual(rep["status"], "unavailable")
        self.assertEqual(self._saved(), self.state)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
