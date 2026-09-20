"""R04 real Context freezing e2e (2026-09-20 followup audit).

audit 关键 test: 同一历史 Context, 改变当前 HMM/regime cache, decision 应不变.
如果结果变 → context 未真冻结, look-ahead 仍存.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class BoardRegimeFrozenTests(unittest.TestCase):
    """R04: context.board_regime 优先 live regime_today.get_today_regime.
    改 live cache 不改结果."""

    def test_context_board_regime_wins_over_live(self):
        from decision_agent import get_decision
        from decision_context import DecisionContext

        # Context 明确 bull_trending; live cache mock 成 crisis
        ctx = DecisionContext(
            as_of=datetime.now(timezone.utc),
            thesis_snapshot={},   # 无 thesis 干扰
            board_regime="bull_trending",
            is_backtest=True,
        )
        fake_market = {"ticker": "TQQQ", "price": 70.0, "pct_chg": 1.5,
                        "rsi_14": 55, "trend": "up", "ma_stack": "bull",
                        "vol_ratio": 1.0, "cum_5d_pct": 3.0}
        fake_events = {"days_to_event": 99, "breaking_news": False,
                        "risk_level": "moderate"}

        # Live regime mock 到 crisis (完全相反)
        with patch("regime_today.get_today_regime", return_value="crisis"):
            r = get_decision(fake_market, fake_events, macro={"vix": 18},
                              context=ctx)
        # 关键: regime 应来自 context (bull_trending), 不是 live (crisis)
        self.assertEqual(r.get("regime"), "bull_trending",
                          f"R04: context.board_regime 应优先, 实际返 {r.get('regime')}")

    def test_no_context_falls_back_to_live_regime(self):
        # 兼容: 无 context 时读 live
        from decision_agent import get_decision
        fake_market = {"ticker": "TQQQ", "price": 70.0, "pct_chg": 1.5,
                        "rsi_14": 55, "trend": "up", "ma_stack": "bull",
                        "vol_ratio": 1.0}
        fake_events = {"days_to_event": 99, "breaking_news": False,
                        "risk_level": "moderate"}
        with patch("regime_today.get_today_regime", return_value="bull_extended"):
            r = get_decision(fake_market, fake_events, macro={"vix": 18})
        self.assertEqual(r.get("regime"), "bull_extended",
                          "无 context 时应 fallback live regime_today")


class CalibrationSnapshotFrozenTests(unittest.TestCase):
    """R04: context.calibration_snapshot 通过 contextvar 到达 _load_calibration.
    改 live _CALIB_CACHE 不改结果."""

    def test_context_calibration_snapshot_overrides_live_cache(self):
        # Custom calib snapshot that's clearly distinct from live
        # p20/p40/p60/p80: 用低阈值让 raw_score=2 落到 top 分位
        custom_calib = {
            "bull_percentiles": {"p20": 0.1, "p40": 0.5, "p60": 1.0,
                                   "p80": 1.5},
            "bear_percentiles": {"p20": 0.1, "p40": 0.5, "p60": 1.0,
                                   "p80": 1.5},
        }
        import decision_agent
        from decision_context import DecisionContext

        # Live cache 假装是不同数据
        live_cache_before = decision_agent._CALIB_CACHE
        decision_agent._CALIB_CACHE = {
            "loaded": True,
            "data": {"bull_percentiles": {"p20": 100, "p40": 200,
                                            "p60": 300, "p80": 400}},
        }
        try:
            ctx = DecisionContext(
                as_of=datetime.now(timezone.utc),
                calibration_snapshot=custom_calib,
                is_backtest=True,
            )
            # 设 active context, 再调 _load_calibration
            token = decision_agent._ACTIVE_CONTEXT.set(ctx)
            try:
                got = decision_agent._load_calibration()
            finally:
                decision_agent._ACTIVE_CONTEXT.reset(token)
            self.assertEqual(got, custom_calib,
                              "R04: context.calibration_snapshot 应替代 live cache")
        finally:
            decision_agent._CALIB_CACHE = live_cache_before

    def test_context_empty_calibration_returns_none(self):
        # calibration_snapshot={} 明确表示"无校准" (backtest 场景)
        import decision_agent
        from decision_context import DecisionContext
        ctx = DecisionContext(
            as_of=datetime.now(timezone.utc),
            calibration_snapshot={},   # 空 dict = 明确无校准
            is_backtest=True,
        )
        token = decision_agent._ACTIVE_CONTEXT.set(ctx)
        try:
            got = decision_agent._load_calibration()
        finally:
            decision_agent._ACTIVE_CONTEXT.reset(token)
        self.assertIsNone(got,
                          "空 dict calibration_snapshot 应视为无校准 (backtest)")

    def test_no_context_uses_live_cache(self):
        import decision_agent
        # 无 active context → 走 live _CALIB_CACHE
        # 只验证不 crash 且返回 cache 结果类型
        got = decision_agent._load_calibration()
        # 可能是 None 或 dict — 不 crash 即通过
        self.assertTrue(got is None or isinstance(got, dict))


class HmmChangeDoesNotAffectContextTests(unittest.TestCase):
    """R04 audit 明确要求的 e2e: 相同历史 Context, 改今天 HMM, 结果应不变."""

    def test_same_context_same_result_regardless_of_live_hmm(self):
        from decision_agent import get_decision
        from decision_context import DecisionContext

        ctx = DecisionContext(
            as_of=datetime(2024, 3, 15, tzinfo=timezone.utc),
            thesis_snapshot={},
            board_regime="neutral",
            calibration_snapshot={},   # 无校准 (确定性 fallback)
            is_backtest=True,
        )
        fake_market = {"ticker": "TQQQ", "price": 70.0, "pct_chg": 0.5,
                        "rsi_14": 55, "trend": "up", "ma_stack": "bull",
                        "vol_ratio": 1.0, "cum_5d_pct": 2.0}
        fake_events = {"days_to_event": 99, "breaking_news": False,
                        "risk_level": "moderate"}

        # 第 1 次: live HMM = "bull_trending"
        with patch("regime_today.get_today_regime", return_value="bull_trending"):
            r1 = get_decision(fake_market, fake_events, macro={"vix": 18},
                                context=ctx)
        # 第 2 次: 换 live HMM = "crisis" (极端反向)
        with patch("regime_today.get_today_regime", return_value="crisis"):
            r2 = get_decision(fake_market, fake_events, macro={"vix": 18},
                                context=ctx)
        # 关键断言: action / regime 应完全相同
        self.assertEqual(r1.get("regime"), r2.get("regime"),
                          f"R04 e2e: 改 live HMM 后 regime 变了 (r1={r1.get('regime')}, r2={r2.get('regime')})")
        self.assertEqual(r1.get("action"), r2.get("action"),
                          f"R04 e2e: 改 live HMM 后 action 变了 (r1={r1.get('action')}, r2={r2.get('action')})")


if __name__ == "__main__":
    unittest.main()
