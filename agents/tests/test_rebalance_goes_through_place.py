"""F09 (Codex MODEL_AUDIT 2026-09-19): 再平衡下单走标准 _place 链路.

旧 submit_rebalance_order 直接 ctx.place_order:
- 不过组合风险门 (portfolio what-if) / 执行计划;
- 不写 execution ledger "submitted" → 成交永远不会被对账进统计.
另: _place 的 submitted 事件不带 tag, refresh_execution_ledger 的 REBALANCE 排除
(不进 cohort) 实际从未生效.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paper_trader as pt


def _broker(order_id="R900"):
    b = MagicMock()
    b.place_order.return_value = (pt.RET_OK, pd.DataFrame([{"order_id": order_id}]))
    return b


class RebalanceUsesPlace(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.ledger = self.td / "execution.jsonl"
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(pt, "EXECUTION_LOG_PATH", self.ledger))
        self.stack.enter_context(patch.object(pt, "_log_trade"))
        self.stack.enter_context(patch.dict(sys.modules, {"notifications": MagicMock()}))
        # 2026-10-07 现金纪律另有 test_cash_discipline 覆盖; 这里只测下单链路
        self.stack.enter_context(patch.object(pt, "_fund_buy_qty", side_effect=lambda c, q, p: q))

    def tearDown(self):
        self.stack.close()

    def events(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_live_rebalance_writes_submitted_event_with_tag(self):
        broker = _broker()
        with patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "_ctx_get", return_value=broker), \
             patch.object(pt, "_portfolio_what_if", return_value={"allow_order": True}):
            oid = pt.submit_rebalance_order("SHY", "BUY", 10, 82.0, reason="drift")
        self.assertEqual(oid, "R900")
        sub = [e for e in self.events() if e["event"] == "submitted"]
        self.assertEqual(len(sub), 1)
        self.assertEqual(sub[0]["ticker"], "US.SHY")
        self.assertIn("REBALANCE", sub[0]["tag"])

    def test_portfolio_gate_blocks_rebalance(self):
        broker = _broker()
        with patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "_ctx_get", return_value=broker), \
             patch.object(pt, "is_sim_active_trading", return_value=False), \
             patch.object(pt, "_portfolio_what_if",
                          return_value={"allow_order": False, "breaches": ["gross"]}):
            oid = pt.submit_rebalance_order("SHY", "BUY", 10, 82.0)
        self.assertIsNone(oid)
        broker.place_order.assert_not_called()

    def test_dry_run_returns_dry(self):
        with patch.object(pt, "DRY_RUN", True), \
             patch.object(pt, "_portfolio_what_if", return_value={"allow_order": True}):
            self.assertEqual(pt.submit_rebalance_order("SHY", "SELL", 5, 82.0), "DRY")

    def test_rebalance_limit_price_keeps_no_buffer(self):
        broker = _broker()
        with patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "_ctx_get", return_value=broker), \
             patch.object(pt, "_portfolio_what_if", return_value={"allow_order": True}):
            pt.submit_rebalance_order("SHY", "BUY", 10, 82.0)
        self.assertEqual(broker.place_order.call_args.kwargs["price"], 82.0)


class RebalanceFillsSkipCohort(unittest.TestCase):
    def test_tagged_rebalance_fill_not_fed_to_cohort(self):
        import cohort_tracker as ct
        td = Path(tempfile.mkdtemp())
        ledger = td / "execution.jsonl"
        ledger.write_text(json.dumps({
            "event": "submitted", "order_id": "R901", "ticker": "US.SHY", "side": "BUY",
            "requested_qty": 10, "order_price": 82.0, "reference_price": 82.0,
            "tag": "[REBALANCE drift]"}) + "\n", encoding="utf-8")
        state = {}
        broker = MagicMock()
        broker.order_list_query.return_value = (pt.RET_OK, pd.DataFrame([{
            "order_id": "R901", "qty": 10, "dealt_qty": 10, "dealt_avg_price": 82.0,
            "order_status": "FILLED_ALL"}]))
        with patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "EXECUTION_LOG_PATH", ledger), \
             patch.object(pt, "COHORT_FIRED_LEDGER_PATH", td / "fired.jsonl"), \
             patch.object(pt, "_ctx_get", return_value=broker), \
             patch.object(pt, "_state_load", side_effect=lambda: copy.deepcopy(state)), \
             patch.object(pt, "_state_save", side_effect=lambda v: state.update(v)), \
             patch.object(ct, "on_buy") as on_buy:
            pt.refresh_execution_ledger()
        on_buy.assert_not_called()
        self.assertIn("filled", ledger.read_text(encoding="utf-8"))


class PlaceRecordsTag(unittest.TestCase):
    def test_submitted_event_carries_tag(self):
        td = Path(tempfile.mkdtemp())
        ledger = td / "execution.jsonl"
        with patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "EXECUTION_LOG_PATH", ledger), \
             patch.object(pt, "_ctx_get", return_value=_broker("P1")), \
             patch.object(pt, "_log_trade"), \
             patch.object(pt, "_portfolio_what_if", return_value={"allow_order": True}), \
             patch.dict(sys.modules, {"notifications": MagicMock()}), \
             patch.object(pt, "_fund_buy_qty", side_effect=lambda c, q, p: q):
            pt._place("US.TEST", pt.TrdSide.BUY, 1, 10.0, tag="[PYRAMID L2]")
        ev = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(ev[0]["tag"], "[PYRAMID L2]")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
