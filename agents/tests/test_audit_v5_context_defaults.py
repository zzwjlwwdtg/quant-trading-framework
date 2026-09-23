"""V5-03 audit (2026-09-23): from_snapshot()/is_backtest=True 的默认值
不能在 helper 里静默 fallback 到 live cache — 那会引入 look-ahead 泄漏.

场景: backtest context 只填了 as_of + market; hmm_state / sector_regime_snapshot
/ calibration_snapshot 都是 None (dataclass 默认). 现有 helper 看到 None 就
调 live disk cache. is_backtest=True 应该把 "None 字段" 一律当 "unavailable",
不 fallback.

audit 复现:
- from_snapshot(as_of, market={}) → hmm_state=None → _get_hmm_meta_state()
  读到今天的 hmm_state.json → 泄漏
- 同上 sector_regime_snapshot=None → classify_ticker_sector() 读今日 sector
  data → 泄漏
- 同上 calibration_snapshot=None → 读 confidence_calibration.json → 泄漏
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class V5_03_BacktestContextNoFallbackToLive(unittest.TestCase):
    """is_backtest=True + None field → 一律 unavailable, 不 fallback live."""

    def test_hmm_state_none_in_backtest_does_not_read_live(self):
        import decision_agent as da
        from decision_context import DecisionContext

        ctx = DecisionContext(
            as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
            is_backtest=True,
            hmm_state=None,   # dataclass default
        )
        live_calls = {"n": 0}
        def fake_hmm_load():
            live_calls["n"] += 1
            return {"current_label": "LIVE_BULL_TODAY", "current_prob": 0.9}

        token = da._ACTIVE_CONTEXT.set(ctx)
        try:
            # Patch the actual live loader in hmm_regime module
            with patch("hmm_regime.load", side_effect=fake_hmm_load):
                got = da._get_hmm_meta_state()
        finally:
            da._ACTIVE_CONTEXT.reset(token)

        self.assertIsNone(got,
                          f"V5-03: backtest + hmm_state=None 应返 None, 实际={got}")
        self.assertEqual(live_calls["n"], 0,
                          f"V5-03: backtest 不能调 live hmm loader, 调用次数={live_calls['n']}")

    def test_sector_regime_none_in_backtest_does_not_read_live(self):
        import decision_agent as da
        from decision_context import DecisionContext

        ctx = DecisionContext(
            as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
            is_backtest=True,
            sector_regime_snapshot=None,
        )
        live_calls = {"n": 0}
        def fake_classify(*args, **kw):
            live_calls["n"] += 1
            return "LIVE_SECTOR_TODAY"

        token = da._ACTIVE_CONTEXT.set(ctx)
        try:
            with patch("sector_regime.classify_ticker_sector",
                        side_effect=fake_classify):
                got = da._get_sector_regime("US.MSFT")
        finally:
            da._ACTIVE_CONTEXT.reset(token)

        self.assertIsNone(got,
                          f"V5-03: backtest + sector_regime=None 应返 None, 实际={got}")
        self.assertEqual(live_calls["n"], 0,
                          f"V5-03: backtest 不能调 live sector classifier")

    def test_calibration_none_in_backtest_does_not_read_live(self):
        import decision_agent as da
        from decision_context import DecisionContext

        ctx = DecisionContext(
            as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
            is_backtest=True,
            calibration_snapshot=None,
        )
        # 先把 module cache 清掉, 否则测试 hit cache 不走 IO
        da._CALIB_CACHE["loaded"] = False
        da._CALIB_CACHE["data"] = None

        io_calls = {"n": 0}
        real_open = open
        def fake_open(*args, **kwargs):
            path_str = str(args[0]) if args else ""
            if "confidence_calibration.json" in path_str:
                io_calls["n"] += 1
            return real_open(*args, **kwargs)

        token = da._ACTIVE_CONTEXT.set(ctx)
        try:
            # Instead of watching open, watch Path.read_text
            from pathlib import Path as _P
            real_read = _P.read_text
            def fake_read(self, *a, **kw):
                if "confidence_calibration.json" in str(self):
                    io_calls["n"] += 1
                    return "{}"   # would leak in current buggy path
                return real_read(self, *a, **kw)
            with patch.object(_P, "read_text", fake_read):
                got = da._load_calibration()
        finally:
            da._ACTIVE_CONTEXT.reset(token)

        self.assertIsNone(got,
                          f"V5-03: backtest + calibration=None 应返 None, 实际={got}")
        self.assertEqual(io_calls["n"], 0,
                          f"V5-03: backtest 不能读 live confidence_calibration.json, "
                          f"读取次数={io_calls['n']}")


class V5_03_LiveContextStillFallsBack(unittest.TestCase):
    """is_backtest=False + None field → 仍 fallback live (兼容旧行为)."""

    def test_live_context_hmm_none_still_reads_live(self):
        import decision_agent as da
        from decision_context import DecisionContext

        ctx = DecisionContext(
            as_of=datetime.now(timezone.utc),
            is_backtest=False,
            hmm_state=None,
        )
        live_calls = {"n": 0}
        def fake_hmm_load():
            live_calls["n"] += 1
            return {"current_label": "BULL", "current_prob": 0.8}

        token = da._ACTIVE_CONTEXT.set(ctx)
        try:
            with patch("hmm_regime.load", side_effect=fake_hmm_load):
                da._get_hmm_meta_state()
        finally:
            da._ACTIVE_CONTEXT.reset(token)

        # Live mode 应仍 fallback (只 backtest 才禁)
        self.assertGreaterEqual(live_calls["n"], 1,
                                 f"V5-03: live 模式 hmm=None 应 fallback live, "
                                 f"实际未调 live, calls={live_calls['n']}")


if __name__ == "__main__":
    unittest.main()
