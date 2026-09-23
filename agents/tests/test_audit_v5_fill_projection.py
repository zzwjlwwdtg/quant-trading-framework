"""V5-02 audit (2026-09-23): fill projection idempotency across state.clear().

audit 复现: partial 5 → state.clear() → broker 累计 10.
- 现有行为: seen 为空 → prev_dealt=0 → delta=10, fired_key=oid|10 不同于 oid|5,
  → cohort 记 5 (第一次) + 10 (state.clear 后), 总 15, 应是 10.
- 期望: prev_dealt 从 cohort_fired_ledger 派生 (append-only, 崩溃可存活),
  取该 oid 已 fired 的最大 cum_dealt. state.clear() 后仍能算对 delta.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class V5_02_ProjectionSurvivesStateClear(unittest.TestCase):
    """audit 精确场景: partial 5 → state.clear() → cum 10 → 应只再记 5."""

    def _run_partial_then_clear_then_cum(
            self, first_dealt: float, first_avg: float,
            second_dealt: float, second_avg: float):
        import paper_trader as pt
        import cohort_tracker as ct
        import pandas as pd

        tmpdir = tempfile.mkdtemp()
        exec_log = Path(tmpdir) / "execution.jsonl"
        cohort_ledger = Path(tmpdir) / "cohort_fired.jsonl"
        exec_log.write_text(json.dumps({
            "event": "submitted", "order_id": "V5_02_OID",
            "ticker": "US.TEST_V5_02", "side": "BUY",
            "requested_qty": 10, "order_price": 100.0,
            "reference_price": 100.0, "ts": "2026-09-20T14:00:00Z",
        }) + "\n", encoding="utf-8")

        state = {}
        calls = []
        call_count = {"n": 0}
        broker_events = [
            {"dealt": first_dealt,  "avg": first_avg,  "status": "FILLED_PART"},
            {"dealt": second_dealt, "avg": second_avg, "status": "FILLED_ALL"},
        ]

        def fake_state_load():
            return copy.deepcopy(state)

        def fake_state_save(s):
            nonlocal state
            state = copy.deepcopy(s)

        def fake_on_buy(ticker, price, qty, signal_ctx=None, ts=None, context=None):
            calls.append({"price": price, "qty": qty})
            return {}

        def fake_query(**kw):
            i = call_count["n"]
            call_count["n"] += 1
            evt = broker_events[min(i, len(broker_events) - 1)]
            df = pd.DataFrame([{
                "order_id": "V5_02_OID", "qty": 10,
                "dealt_qty": evt["dealt"],
                "dealt_avg_price": evt["avg"],
                "order_status": evt.get("status"),
            }])
            return (0, df)

        fake_ctx = MagicMock()
        fake_ctx.order_list_query = fake_query

        with patch.object(pt, "EXECUTION_LOG_PATH", exec_log), \
             patch.object(pt, "COHORT_FIRED_LEDGER_PATH", cohort_ledger), \
             patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "_ctx_get", return_value=fake_ctx), \
             patch.object(pt, "_state_load", side_effect=fake_state_load), \
             patch.object(pt, "_state_save", side_effect=fake_state_save), \
             patch.object(pt, "RET_OK", 0), \
             patch.object(pt, "TRD_ENV", "SIMULATE"), \
             patch.object(pt, "ACC_ID", 12345), \
             patch.object(ct, "on_buy", side_effect=fake_on_buy):
            # First reconcile: partial 5
            pt.refresh_execution_ledger()
            # SIMULATE STATE.CLEAR (crash / rollback): seen is wiped
            state.clear()
            # Second reconcile: broker now says cum 10
            pt.refresh_execution_ledger()

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        return calls

    def test_partial_then_state_clear_then_cum_dealt(self):
        # audit V5-02: partial 5@100, state.clear(), cum 10@110
        # 第 1 次: 记 5@100 (cohort_fired = oid|5)
        # 第 2 次: prev_dealt 应从 cohort_fired_ledger 派生 = 5, delta=5, 记 5
        # 总 qty = 10 (不是 15)
        calls = self._run_partial_then_clear_then_cum(5, 100.0, 10, 110.0)
        total_qty = sum(c["qty"] for c in calls)
        self.assertEqual(total_qty, 10,
                          f"V5-02: partial 5 → state.clear() → cum 10 应总 10, 实际 {total_qty}, calls={calls}")

    def test_recovered_cost_basis_matches_delta_price(self):
        # F3 followup (2026-09-23): partial 5@100 → state.clear() → cum 10@110
        # 第 1 次 callback: qty=5 @ price=100
        # 第 2 次 (state.clear 后): 应为 qty=5 @ price=120 (增量成本 = (10*110-5*100)/5 = 120)
        # 之前只恢复 prev_dealt 不恢复 prev_cash → 第 2 次 delta_price fallback 到
        # 聚合均价 110 → 成本错记.
        calls = self._run_partial_then_clear_then_cum(5, 100.0, 10, 110.0)
        self.assertEqual(len(calls), 2, f"应有 2 次 callback, actual {calls}")
        self.assertEqual(calls[0]["qty"], 5)
        self.assertAlmostEqual(calls[0]["price"], 100.0, places=1)
        self.assertEqual(calls[1]["qty"], 5)
        self.assertAlmostEqual(calls[1]["price"], 120.0, places=1,
                                msg=f"F3: recovered delta_price 应 120, actual={calls[1]}")

    def test_partial_then_state_clear_then_same_cum_no_double(self):
        # partial 5@100, state.clear(), 相同 5@100 report (幂等 replay)
        # cohort_fired ledger 已含 oid|5 → 不再 fire.
        # 总 qty = 5.
        calls = self._run_partial_then_clear_then_cum(5, 100.0, 5, 100.0)
        total_qty = sum(c["qty"] for c in calls)
        self.assertEqual(total_qty, 5,
                          f"V5-02: 重复 same cum 应幂等, 实际 {total_qty}, calls={calls}")


class F3_CallbackFailureRetry(unittest.TestCase):
    """F3-#2 audit followup (2026-09-23): callback 首次失败 → 下次 reconcile
    同 signature 应能 retry, 而非 seen 已前移导致跳过."""

    def test_callback_failure_first_attempt_retries_next_reconcile(self):
        import paper_trader as pt
        import cohort_tracker as ct
        import pandas as pd

        tmpdir = tempfile.mkdtemp()
        exec_log = Path(tmpdir) / "execution.jsonl"
        cohort_ledger = Path(tmpdir) / "cohort_fired.jsonl"
        exec_log.write_text(json.dumps({
            "event": "submitted", "order_id": "F3_OID",
            "ticker": "US.TEST_F3", "side": "BUY",
            "requested_qty": 10, "order_price": 100.0,
            "reference_price": 100.0, "ts": "2026-09-20T14:00:00Z",
        }) + "\n", encoding="utf-8")

        state = {}
        callbacks = []
        attempts = {"n": 0}
        broker_reports = [
            {"dealt": 10, "avg": 110.0, "status": "FILLED_ALL"},
            {"dealt": 10, "avg": 110.0, "status": "FILLED_ALL"},   # same
        ]
        call_count = {"n": 0}

        def fake_state_load():
            return copy.deepcopy(state)
        def fake_state_save(s):
            nonlocal state
            state = copy.deepcopy(s)
        def fake_on_buy(ticker, price, qty, signal_ctx=None, ts=None, context=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("injected: downstream cohort failure attempt 1")
            callbacks.append({"price": price, "qty": qty})
        def fake_query(**kw):
            i = call_count["n"]
            call_count["n"] += 1
            evt = broker_reports[min(i, len(broker_reports) - 1)]
            return (0, pd.DataFrame([{
                "order_id": "F3_OID", "qty": 10,
                "dealt_qty": evt["dealt"],
                "dealt_avg_price": evt["avg"],
                "order_status": evt["status"],
            }]))

        fake_ctx = MagicMock()
        fake_ctx.order_list_query = fake_query
        with patch.object(pt, "EXECUTION_LOG_PATH", exec_log), \
             patch.object(pt, "COHORT_FIRED_LEDGER_PATH", cohort_ledger), \
             patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "_ctx_get", return_value=fake_ctx), \
             patch.object(pt, "_state_load", side_effect=fake_state_load), \
             patch.object(pt, "_state_save", side_effect=fake_state_save), \
             patch.object(pt, "RET_OK", 0), \
             patch.object(pt, "TRD_ENV", "SIMULATE"), \
             patch.object(pt, "ACC_ID", 12345), \
             patch.object(ct, "on_buy", side_effect=fake_on_buy):
            pt.refresh_execution_ledger()   # R1: callback raises
            pt.refresh_execution_ledger()   # R2: retry, succeeds

        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(attempts["n"], 2,
                          f"F3-#2: 第一次 callback 失败, 第二次应 retry. attempts={attempts['n']}")
        total_qty = sum(c["qty"] for c in callbacks)
        self.assertEqual(total_qty, 10,
                          f"F3-#2: 重试成功后 total qty 应 10, actual={total_qty}, calls={callbacks}")


class F3_CohortTrackerFiredKeyIdempotent(unittest.TestCase):
    """F3-#3 second-line-of-defense: cohort_tracker.on_buy 收到相同 fired_key
    时应幂等, 不重复入账. 用于 fired_ledger append 失败 + state.clear 场景.
    """

    def test_on_buy_dedups_by_fired_key(self):
        import cohort_tracker as ct
        tmpdir = tempfile.mkdtemp()
        ledger = Path(tmpdir) / "ledger.jsonl"
        active = Path(tmpdir) / "active.json"
        try:
            with patch.object(ct, "_LEDGER", ledger), \
                 patch.object(ct, "_ACTIVE", active):
                r1 = ct.on_buy("US.TEST_IDEM", 100.0, 10,
                                signal_ctx={"tag": "L1", "fired_key": "K1"},
                                ts="2026-09-20T14:00:00Z")
                # Same fired_key second time → should NOT double-add
                r2 = ct.on_buy("US.TEST_IDEM", 100.0, 10,
                                signal_ctx={"tag": "L1", "fired_key": "K1"},
                                ts="2026-09-20T14:01:00Z")
                active_cohort = ct.active_cohort("US.TEST_IDEM")
        finally:
            import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(active_cohort["current_qty"], 10,
                          f"F3-#3: same fired_key 重复调用应 idempotent (qty 保持 10). actual={active_cohort}")


if __name__ == "__main__":
    unittest.main()
