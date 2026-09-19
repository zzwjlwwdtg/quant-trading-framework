"""WP04 深度重构 (2026-09-20): DecisionContext frozen snapshot 单测.

锁死:
- Dataclass 冻结 (无法就地改)
- with_updates 返新对象
- is_ticker_blacklisted / soft: snapshot 优先, fallback live
- backtest 用空 snapshot ({}) → 不 block 任何 ticker (无 look-ahead)
- live builder 读当前 thesis
- 迁移期兼容: _apply_thesis_filter 无 context 走 live, 有 context 走 snapshot
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import decision_context as dc
from decision_context import DecisionContext, from_live_now, from_snapshot


class DataclassImmutabilityTests(unittest.TestCase):

    def test_context_is_frozen(self):
        c = DecisionContext(as_of=datetime.now(timezone.utc))
        with self.assertRaises(FrozenInstanceError):
            c.as_of = datetime.now(timezone.utc)   # type: ignore

    def test_with_updates_returns_new_object(self):
        c = DecisionContext(as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
                             board_regime="neutral")
        c2 = c.with_updates(board_regime="bull_trending")
        self.assertEqual(c.board_regime, "neutral")
        self.assertEqual(c2.board_regime, "bull_trending")
        self.assertIsNot(c, c2)

    def test_default_fields_populated(self):
        c = DecisionContext(as_of=datetime.now(timezone.utc))
        self.assertEqual(c.market, {})
        self.assertEqual(c.events, {})
        self.assertIsNone(c.thesis_snapshot)
        self.assertEqual(c.strategy_version, "unknown")
        self.assertEqual(c.builder, "explicit")
        self.assertFalse(c.is_backtest)


class ThesisSnapshotReadTests(unittest.TestCase):
    """核心 WP04 invariant: context.is_ticker_blacklisted 只查 snapshot,
    不 leak 到 live thesis_config."""

    def test_snapshot_with_blacklist_blocks_matching_ticker(self):
        snap = {
            "blacklist_tickers": ["US.SOXL", "US.KLAC"],
            "blacklist_reason": "test_snapshot_block",
        }
        c = DecisionContext(as_of=datetime.now(timezone.utc),
                              thesis_snapshot=snap)
        blocked, reason = c.is_ticker_blacklisted("US.SOXL")
        self.assertTrue(blocked)
        self.assertEqual(reason, "test_snapshot_block")

    def test_empty_snapshot_blocks_nothing(self):
        # 关键: backtest 用 thesis_snapshot={} → 无 look-ahead
        c = DecisionContext(as_of=datetime.now(timezone.utc),
                              thesis_snapshot={})
        blocked, _ = c.is_ticker_blacklisted("US.SOXL")
        self.assertFalse(blocked,
                          "empty snapshot 不能 block ticker (backtest 无 look-ahead)")

    def test_none_snapshot_falls_back_to_live(self):
        # thesis_snapshot=None → 兼容旧路径, 走 live thesis_config
        # US.SOXL 在 live blacklist 里 → 应 block
        c = DecisionContext(as_of=datetime.now(timezone.utc),
                              thesis_snapshot=None)
        blocked, _ = c.is_ticker_blacklisted("US.SOXL")
        self.assertTrue(blocked, "None snapshot → fallback live, SOXL 应 blocked")

    def test_ticker_normalization_in_snapshot_lookup(self):
        snap = {"blacklist_tickers": ["US.SOXL"]}
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot=snap)
        # 裸 SOXL 应匹配 US.SOXL
        blocked, _ = c.is_ticker_blacklisted("SOXL")
        self.assertTrue(blocked)


class SoftBlacklistSnapshotTests(unittest.TestCase):

    def test_soft_blacklist_from_snapshot(self):
        snap = {
            "soft_blacklist": {
                "US.IEI": {"min_confidence": 7, "since": "2026-09-11",
                            "reason": "test soft block"},
            },
        }
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot=snap)
        blocked, reason, meta = c.is_ticker_soft_blacklisted("US.IEI")
        self.assertTrue(blocked)
        self.assertEqual(meta["min_confidence"], 7)

    def test_empty_snapshot_soft_returns_none(self):
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot={})
        blocked, _, _ = c.is_ticker_soft_blacklisted("US.IEI")
        self.assertFalse(blocked)

    def test_malformed_soft_falls_back_gracefully(self):
        snap = {"soft_blacklist": ["not_a_dict"]}   # 手误
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot=snap)
        blocked, _, _ = c.is_ticker_soft_blacklisted("US.IEI")
        self.assertFalse(blocked, "malformed snapshot 应 fail-safe")

    def test_soft_entry_non_dict_still_blocks_with_default_min_conf(self):
        # entry 是字符串而非 dict, 但 key 匹配 → 仍视为 blocked (fail-closed for intent)
        snap = {"soft_blacklist": {"US.IEI": "some string"}}
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot=snap)
        blocked, _, meta = c.is_ticker_soft_blacklisted("US.IEI")
        self.assertTrue(blocked)
        self.assertEqual(meta["min_confidence"], 7)


class BuilderTests(unittest.TestCase):

    def test_from_snapshot_marks_backtest(self):
        as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
        c = from_snapshot(as_of=as_of, market={"price": 100})
        self.assertTrue(c.is_backtest)
        self.assertEqual(c.builder, "snapshot")
        self.assertEqual(c.data_version, "20260601")

    def test_from_snapshot_default_thesis_is_empty(self):
        # 默认无 thesis (backtest 应有意豁免)
        c = from_snapshot(as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
                            market={"price": 100})
        self.assertEqual(c.thesis_snapshot, {})

    def test_from_live_now_reads_current_thesis(self):
        c = from_live_now("US.SOXL")
        self.assertFalse(c.is_backtest)
        self.assertEqual(c.builder, "live")
        self.assertIsNotNone(c.thesis_snapshot)
        # thesis_snapshot 应含 blacklist_tickers key (从 live thesis_config 读)
        self.assertIn("blacklist_tickers", c.thesis_snapshot or {})


class ContextMigrationCompatibilityTests(unittest.TestCase):
    """迁移期兼容: _apply_thesis_filter 无 context 走 live, 有 context 走 snapshot."""

    def test_no_context_kwarg_still_uses_live_thesis(self):
        # 旧 caller 不传 context, 应保持原行为 (US.SOXL blocked)
        from decision_agent import _apply_thesis_filter
        decision = {"action": "BUY", "confidence": 8, "reason": "test"}
        out = _apply_thesis_filter(decision, "US.SOXL")
        self.assertEqual(out["action"], "HOLD")
        self.assertTrue(out.get("thesis_blocked"))

    def test_context_with_empty_thesis_bypasses_live_block(self):
        # 新 caller 传 empty thesis snapshot → 即使 live 里 SOXL blacklisted, 也不 block
        from decision_agent import _apply_thesis_filter
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot={})
        decision = {"action": "BUY", "confidence": 8, "reason": "test"}
        out = _apply_thesis_filter(decision, "US.SOXL", context=c)
        self.assertEqual(out["action"], "BUY",
                          "context with empty thesis 应绕过 live block (backtest 场景)")

    def test_context_with_custom_thesis_uses_it(self):
        # context 传自定义 blacklist → 只按 context 的走
        from decision_agent import _apply_thesis_filter
        snap = {"blacklist_tickers": ["US.CUSTOM"], "blacklist_reason": "custom"}
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot=snap)
        # US.SOXL 在 live blacklist, 但不在 custom → 应放行
        decision = {"action": "BUY", "confidence": 8, "reason": "test"}
        out = _apply_thesis_filter(decision, "US.SOXL", context=c)
        self.assertEqual(out["action"], "BUY")
        # US.CUSTOM 在 custom blacklist → 应 block
        out2 = _apply_thesis_filter({"action": "BUY", "confidence": 8}, "US.CUSTOM", context=c)
        self.assertEqual(out2["action"], "HOLD")
        self.assertTrue(out2.get("thesis_blocked"))

    def test_context_soft_blacklist_from_snapshot(self):
        from decision_agent import _apply_thesis_filter
        snap = {"soft_blacklist": {"US.SNAP": {"min_confidence": 7}}}
        c = DecisionContext(as_of=datetime.now(timezone.utc), thesis_snapshot=snap)
        # conf 3 < effective 4 → soft blocked
        decision = {"action": "WATCH_BUY", "confidence": 3, "reason": "trend"}
        out = _apply_thesis_filter(decision, "US.SNAP", context=c)
        self.assertEqual(out["action"], "HOLD")
        self.assertTrue(out.get("thesis_soft_blocked"))


if __name__ == "__main__":
    unittest.main()
