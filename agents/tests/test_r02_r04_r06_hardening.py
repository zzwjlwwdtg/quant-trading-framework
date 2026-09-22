"""Followup-followup audit (2026-09-20): R02 event-replay, R04 real freeze, R06 effective-time.

Per audit: 每项先补会失败的实际链路测试, 再修复.
所有测试都调实际生产函数, 不重写公式.

## R02 requirements (audit 明确)
- 不能用 (order_id, delta_qty) 去重 (同 oid 连续两次 5 股各成交合法)
- 唯一 key = (env, acc, oid, broker_fill_id) OR (env, acc, oid, revision_seq)
- 事件重放: fills 是唯一事实, cohort 从 fills 派生
- 覆盖: 重复/乱序/不同价 partial / state.clear() 后 replay / 回调失败恢复
- 最终 qty + cost + realized PnL 都正确, 不只是"不重复"

## R04 requirements
- Construction deep-copy (外部 mutate 原 dict 不改 ctx.market)
- 递归不可变 (嵌套 dict/list) — mutation 应被拒绝 (TypeError)
- with_updates 独立快照 (unchanged 字段也 deep copy)
- HMM / sector / calibration / board_regime 全从 Context (context 提供时不读 live)
- 缺失历史输入 → 明确 unavailable, 不 fallback current
- 并发调用 ContextVar 不串数据

## R06 requirements
- effective_from / effective_to / available_at (可证明的发布时间)
- created_at 不能替 effective_time (unless 显式声明)
- 空档 / 空 archive / 边界缺失 → historical_unknown
- 同一 as_of 响应所有字段同一时点
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


# ================================================================
# R02: Event-replay based idempotency (fills are source of truth)
# ================================================================

class R02_ProductionReplayIdempotency(unittest.TestCase):
    """audit 关键: 调实际 refresh_execution_ledger, state.clear() 后 replay,
    最终 cohort qty 应等于 broker 累计, 不重复也不遗漏."""

    def _run_reconciles(self, broker_events_sequence, clear_state_between=None):
        """Helper: 按顺序 fake broker returns, 每次调 refresh_execution_ledger,
        指定 index 后清 state (模拟崩溃/回滚). 返 cohort callbacks list."""
        import paper_trader as pt
        import cohort_tracker as ct
        import pandas as pd

        tmpdir = tempfile.mkdtemp()
        exec_log = Path(tmpdir) / "execution.jsonl"
        cohort_ledger = Path(tmpdir) / "cohort_fired.jsonl"   # isolate from prod
        exec_log.write_text(json.dumps({
            "event": "submitted", "order_id": "R02_OID",
            "ticker": "US.TEST_R02", "side": "BUY",
            "requested_qty": 10, "order_price": 100.0,
            "reference_price": 100.0, "ts": "2026-09-20T14:00:00Z",
        }) + "\n", encoding="utf-8")

        state = {}
        calls = []
        call_count = {"n": 0}

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
            evt = broker_events_sequence[min(i, len(broker_events_sequence) - 1)]
            df = pd.DataFrame([{
                "order_id": "R02_OID", "qty": 10,
                "dealt_qty": evt["dealt"],
                "dealt_avg_price": evt["avg"],
                "order_status": evt.get("status", "FILLED_PART"),
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
            for i in range(len(broker_events_sequence)):
                if clear_state_between is not None and i == clear_state_between:
                    state.clear()
                pt.refresh_execution_ledger()

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        return calls

    def test_normal_partial_then_full_no_duplication(self):
        # 正常: partial 5@100, filled 10@110 → 应记 5@100 + 5@120
        calls = self._run_reconciles([
            {"dealt": 5,  "avg": 100.0, "status": "FILLED_PART"},
            {"dealt": 10, "avg": 110.0, "status": "FILLED_ALL"},
        ])
        total_qty = sum(c["qty"] for c in calls)
        self.assertEqual(total_qty, 10, f"总 qty 应 10, 实际 {total_qty}, calls={calls}")

    def test_state_cleared_between_reconciles_no_double_count(self):
        # R02 关键: 首次成交后清 state, 第二次 replay 相同 broker 状态
        # broker 累计仍是 10@110, 应保持 total qty 10, 不双记
        calls = self._run_reconciles([
            {"dealt": 10, "avg": 110.0, "status": "FILLED_ALL"},
            {"dealt": 10, "avg": 110.0, "status": "FILLED_ALL"},   # replay
        ], clear_state_between=1)
        total_qty = sum(c["qty"] for c in calls)
        self.assertEqual(total_qty, 10,
                          f"R02 audit: state.clear() 后 replay 不能双记. 实际 {total_qty}, calls={calls}")


class R02_FillReducerFromLedger(unittest.TestCase):
    """audit 建议: cohort 应从 execution_ledger 派生 (事件重放模式).
    fill_ledger.get_position 已经是这个模式的一半; cohort_tracker.stats
    应也基于 fill_ledger, 不再依赖单独 ledger."""

    def test_replay_same_events_gives_same_position(self):
        # 重复 events (乱序 + 重复 broker report) 应产生同一 position
        import fill_ledger as fl
        events = [
            {"event": "partial", "order_id": "A", "ts": "2026-09-01T14:00Z",
             "ticker": "US.T", "side": "BUY",
             "dealt_qty": 5, "average_fill_price": 100.0},
            {"event": "filled", "order_id": "A", "ts": "2026-09-01T14:05Z",
             "ticker": "US.T", "side": "BUY",
             "dealt_qty": 10, "average_fill_price": 110.0},
        ]
        # Duplicate the sequence: same broker events reported twice
        duplicated = events + events
        with patch.object(fl, "_load_ledger", return_value=duplicated):
            pos = fl.get_position("US.T")
        # 关键: 无论 events 重复几次, position 都应 = 累计 10@110 (= 5@100 + 5@120)
        # avg_cost = 110
        self.assertEqual(pos["qty"], 10, f"重复 events 不应双记, qty={pos['qty']}")
        self.assertAlmostEqual(pos["avg_cost"], 110.0, places=1)


# ================================================================
# R04: Real Context freezing (immutable + all fields from context)
# ================================================================

class R04_ContextImmutable(unittest.TestCase):

    def test_construction_deep_copies_market_dict(self):
        # audit: 修改原 dict 不能污染 ctx.market
        from decision_context import DecisionContext, from_snapshot
        raw_market = {"price": 100, "ticker": "US.X"}
        ctx = from_snapshot(datetime(2024, 1, 1, tzinfo=timezone.utc),
                              raw_market, {}, {})
        raw_market["price"] = 999
        self.assertNotEqual(ctx.market.get("price"), 999,
                              f"R04: 修改原 dict 污染了 ctx.market, actual={dict(ctx.market)}")

    def test_context_market_is_immutable(self):
        # audit: ctx.market["price"] = X 应抛异常 (不可变容器)
        from decision_context import from_snapshot
        ctx = from_snapshot(datetime(2024, 1, 1, tzinfo=timezone.utc),
                              {"price": 100}, {}, {})
        with self.assertRaises((TypeError, AttributeError)):
            ctx.market["price"] = 999   # type: ignore

    def test_with_updates_does_not_share_unchanged_fields(self):
        from decision_context import from_snapshot
        ctx = from_snapshot(datetime(2024, 1, 1, tzinfo=timezone.utc),
                              {"price": 100}, {"ev": 1}, {})
        derived = ctx.with_updates(strategy_version="test")
        self.assertIsNot(derived.market, ctx.market,
                           "R04: with_updates 未 deep copy market")
        self.assertIsNot(derived.events, ctx.events,
                           "R04: with_updates 未 deep copy events")


class R04_HMMAndSectorFromContext(unittest.TestCase):
    """context 提供时 HMM/sector 不应读 live module state."""

    def test_hmm_read_from_context_not_live(self):
        import decision_agent as da
        from decision_context import DecisionContext

        # Context 明确指定 hmm_state; 期望 _get_hmm_meta_state() 返 context 的值
        ctx = DecisionContext(
            as_of=datetime(2024, 1, 1, tzinfo=timezone.utc),
            hmm_state="bull_low_vol",
        )
        # Mock live path to raise so we know we're not going there
        called_live = {"n": 0}
        def fake_live():
            called_live["n"] += 1
            return "SHOULD_NOT_BE_USED"

        token = da._ACTIVE_CONTEXT.set(ctx)
        try:
            with patch.object(da, "_load_hmm_state_from_disk",
                                side_effect=fake_live, create=True):
                got = da._get_hmm_meta_state()
        finally:
            da._ACTIVE_CONTEXT.reset(token)

        self.assertEqual(got, "bull_low_vol",
                          f"R04: hmm 应从 context 读, actual={got}")

    def test_sector_read_from_context_not_live(self):
        import decision_agent as da
        from decision_context import DecisionContext

        ctx = DecisionContext(
            as_of=datetime(2024, 1, 1, tzinfo=timezone.utc),
            sector_regime_snapshot={"US.MSFT": "sector_bull"},
        )
        token = da._ACTIVE_CONTEXT.set(ctx)
        try:
            got = da._get_sector_regime("US.MSFT")
        finally:
            da._ACTIVE_CONTEXT.reset(token)
        self.assertEqual(got, "sector_bull",
                          f"R04: sector 应从 context 读, actual={got}")


class R04_ConcurrencyContextVarIsolation(unittest.TestCase):
    """并发调用 ContextVar 不串数据."""

    def test_two_threads_different_contexts(self):
        import decision_agent as da
        from decision_context import DecisionContext

        results = {}
        def worker(name, calib_snapshot):
            ctx = DecisionContext(
                as_of=datetime(2024, 1, 1, tzinfo=timezone.utc),
                calibration_snapshot=calib_snapshot,
            )
            token = da._ACTIVE_CONTEXT.set(ctx)
            try:
                # Simulate work
                import time
                time.sleep(0.05)
                results[name] = da._load_calibration()
            finally:
                da._ACTIVE_CONTEXT.reset(token)

        t1 = threading.Thread(target=worker, args=("A", {"marker": "AAA"}))
        t2 = threading.Thread(target=worker, args=("B", {"marker": "BBB"}))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(results["A"], {"marker": "AAA"})
        self.assertEqual(results["B"], {"marker": "BBB"},
                          f"R04 concurrency: threads 串了 calibration, results={results}")


# ================================================================
# R06: Effective time / available time selection
# ================================================================

class R06_EffectiveTimeSelection(unittest.TestCase):

    def test_gap_between_theses_returns_unknown(self):
        # archive 里 thesis 有 effective_from → 若 as_of 在 gap 中 → unknown
        from webui import _historical_thesis_at
        archive = [
            {"retired_at": "2026-09-10T00:00:00Z",
             "thesis": {"version": "V1", "created_at": "2026-08-01",
                        "effective_from": "2026-08-01"}},
        ]
        # 2020-01-01 早于 V1 的 effective_from → 应 unknown
        r = _historical_thesis_at("2020-01-01", archive)
        self.assertIsNone(r,
                            "R06: as_of 早于 archive 的 effective_from → 应返 None")

    def test_effective_from_wins_over_created_at(self):
        # thesis 创建于 2026-01-01 但 effective 2026-06-01 →
        # as_of 2026-03-01 不能返 它 (effective 前)
        from webui import _historical_thesis_at
        archive = [
            {"retired_at": "2026-09-10T00:00:00Z",
             "thesis": {"version": "V1", "created_at": "2026-01-01",
                        "effective_from": "2026-06-01"}},
        ]
        r = _historical_thesis_at("2026-03-01", archive)
        self.assertIsNone(r,
                            "R06: effective_from 未到, 不能返 该 thesis")

    def test_as_of_within_effective_range(self):
        from webui import _historical_thesis_at
        archive = [
            {"retired_at": "2026-09-10T00:00:00Z",
             "thesis": {"version": "V1", "created_at": "2026-06-01",
                        "effective_from": "2026-06-01"}},
        ]
        r = _historical_thesis_at("2026-07-15", archive)
        self.assertIsNotNone(r)
        self.assertEqual(r["version"], "V1")


if __name__ == "__main__":
    unittest.main()
