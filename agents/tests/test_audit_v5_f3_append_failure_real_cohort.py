"""F3-#3 (audit v5 followup, 2026-09-23): fired-ledger append 失败 + state 丢失.

followup probe `fired_append_failure_state_loss` mock 掉了 cohort_tracker.on_buy,
所以只能看到 paper_trader 发了两次回调 (20 股). 真实系统的第二道防线是
cohort_tracker 按 fired_key 幂等. 本测试 **不 mock cohort_tracker**, 用真实
on_buy / on_sell (临时文件), 验证投影最终状态:

- BUY 10@110, append 失败, state.clear, 再 reconcile → cohort qty 仍为 10.
- 已有 cohort 20@100, SELL 10@110 (部分退出), append 失败, state.clear,
  再 reconcile → cohort qty 10, exits 1 条, realized +100 (不是 0 股 / +200).
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

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class F3_AppendFailureStateLossRealCohort(unittest.TestCase):

    def _run(self, side, seed_active=None):
        import pandas as pd
        import paper_trader as pt
        import cohort_tracker as ct

        td = Path(tempfile.mkdtemp(prefix="f3-real-cohort-"))
        exec_log = td / "execution.jsonl"
        exec_log.write_text(json.dumps({
            "event": "submitted", "order_id": "F3R", "ticker": "US.TEST_F3R",
            "side": side, "requested_qty": 10, "order_price": 110.0,
            "reference_price": 110.0, "ts": "2026-09-23T14:00:00Z",
        }) + "\n", encoding="utf-8")
        active = td / "active.json"
        if seed_active is not None:
            active.write_text(json.dumps(seed_active), encoding="utf-8")

        state: dict = {}

        def save(value):
            state.clear()
            state.update(copy.deepcopy(value))

        broker = MagicMock()
        broker.order_list_query.return_value = (pt.RET_OK, pd.DataFrame([{
            "order_id": "F3R", "qty": 10, "dealt_qty": 10,
            "dealt_avg_price": 110.0, "order_status": "FILLED_ALL"}]))

        with ExitStack() as st:
            st.enter_context(patch.object(pt, "DRY_RUN", False))
            st.enter_context(patch.object(pt, "EXECUTION_LOG_PATH", exec_log))
            st.enter_context(patch.object(pt, "COHORT_FIRED_LEDGER_PATH", td / "fired.jsonl"))
            st.enter_context(patch.object(pt, "_ctx_get", return_value=broker))
            st.enter_context(patch.object(pt, "_state_load", side_effect=lambda: copy.deepcopy(state)))
            st.enter_context(patch.object(pt, "_state_save", side_effect=save))
            st.enter_context(patch.object(pt, "_append_cohort_fired_ledger",
                                          side_effect=OSError("injected append failure")))
            st.enter_context(patch.object(ct, "_ACTIVE", active))
            st.enter_context(patch.object(ct, "_LEDGER", td / "cohorts.jsonl"))
            pt.refresh_execution_ledger()
            state.clear()                      # crash: state file lost
            pt.refresh_execution_ledger()      # replay same broker fact
            return ct._load_active().get("US.TEST_F3R")

    def test_buy_replay_after_append_failure_not_double_counted(self):
        cohort = self._run("BUY")
        self.assertIsNotNone(cohort)
        self.assertEqual(cohort["current_qty"], 10)
        self.assertEqual(len(cohort["entries"]), 1)
        self.assertAlmostEqual(cohort["cost_basis_usd"], 1100.0)

    def test_partial_sell_replay_after_append_failure_not_double_counted(self):
        seed = {"US.TEST_F3R": {
            "cohort_id": "US.TEST_F3R_seed", "ticker": "US.TEST_F3R",
            "open_ts": "2026-09-01T00:00:00+00:00",
            "entries": [{"ts": "2026-09-01T00:00:00+00:00", "price": 100.0,
                         "qty": 20, "tag": None, "fired_key": "seed"}],
            "exits": [], "current_qty": 20, "avg_entry_price": 100.0,
            "cost_basis_usd": 2000.0, "realized_pnl_usd": 0.0, "status": "active"}}
        cohort = self._run("SELL", seed_active=seed)
        self.assertIsNotNone(cohort, "partial exit must keep cohort active")
        self.assertEqual(cohort["current_qty"], 10)
        self.assertEqual(len(cohort["exits"]), 1)
        self.assertAlmostEqual(cohort["realized_pnl_usd"], 100.0)


if __name__ == "__main__":
    unittest.main()
