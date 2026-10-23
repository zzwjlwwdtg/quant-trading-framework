"""F05 (Codex MODEL_AUDIT 2026-09-19): 主回测执行纪律与实盘一致.

锁死:
1. 止损/止盈先于置信度过滤: 持仓期间低置信度日 (或决策异常) 也要执行 trailing stop.
2. 信号不能用产生它的收盘价成交: 今日收盘信号 → 次日开盘成交.
3. 加仓规则与实盘 paper_trader._pyramid_add_qty 一致 (50% 当前仓, 金额/40% 上限).
4. 结果声明回测假设 (固定宏观, 有限 regime, 无事件).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import backtest_engine
import decision_agent


def _frame(opens, closes):
    dates = pd.date_range("2026-07-27", periods=len(closes), freq="B")
    n = len(closes)
    return pd.DataFrame({
        "open": opens, "close": closes,
        "ma50": [100.0] * n, "rsi_14": [40.0] * n, "ma20": [100.0] * n,
        "bb_pct": [0.5] * n, "cci_20": [0.0] * n,
    }, index=dates)


def _run(frame, decisions):
    with patch.object(backtest_engine, "load_history", return_value=frame.copy()), \
         patch.object(backtest_engine, "add_indicators", side_effect=lambda v: v), \
         patch.object(backtest_engine, "build_mkt", return_value={"ticker": "US.GLD"}), \
         patch.object(decision_agent, "_conf_scale", return_value=5), \
         patch.object(decision_agent, "get_decision", side_effect=decisions):
        return backtest_engine.run_mid(tickers=["GLD"], days=len(frame))


class NextOpenExecution(unittest.TestCase):
    def test_buy_signal_fills_at_next_open_not_signal_close(self):
        f = _frame(opens=[100, 105, 106], closes=[100, 106, 107])
        r = _run(f, [{"action": "BUY", "confidence": 3},
                     {"action": "HOLD", "confidence": 3},
                     {"action": "HOLD", "confidence": 3}])
        buys = [t for t in r["history"] if t["side"] == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["date"], "2026-07-28")
        self.assertEqual(buys[0]["price"], 105.0)

    def test_signal_on_last_day_is_not_filled(self):
        f = _frame(opens=[100, 100], closes=[100, 100])
        r = _run(f, [{"action": "HOLD", "confidence": 3},
                     {"action": "BUY", "confidence": 3}])
        self.assertEqual(r["history"], [])


class StopsBeforeConfidenceFilter(unittest.TestCase):
    def test_trailing_stop_runs_on_low_confidence_day(self):
        # buy @ open 100 (day2); day3 close 80 (-20% > 8% trailing) with conf 0
        f = _frame(opens=[100, 100, 99, 80], closes=[100, 100, 80, 80])
        r = _run(f, [{"action": "BUY", "confidence": 3},
                     {"action": "HOLD", "confidence": 3},
                     {"action": "HOLD", "confidence": 0},
                     {"action": "HOLD", "confidence": 0}])
        sells = [t for t in r["history"] if t["side"] == "SELL"]
        self.assertEqual(len(sells), 1, r["history"])
        self.assertIn("TRAIL-STOP", sells[0]["reason"])
        self.assertEqual(sells[0]["date"], "2026-07-29")

    def test_trailing_stop_runs_when_decision_errors(self):
        f = _frame(opens=[100, 100, 99, 80], closes=[100, 100, 80, 80])
        decisions = [{"action": "BUY", "confidence": 3},
                     {"action": "HOLD", "confidence": 3},
                     RuntimeError("provider down"),
                     {"action": "HOLD", "confidence": 3}]
        r = _run(f, decisions)
        stops = [t for t in r["history"] if "TRAIL-STOP" in t["reason"]]
        self.assertEqual([t["date"] for t in stops], ["2026-07-29"], r["history"])


class PyramidMatchesLive(unittest.TestCase):
    def test_same_as_paper_trader_rule(self):
        import paper_trader as pt
        grid = [(36, 249.77, 742_000.0, 1_000_000.0), (100, 100.0, 2_000.0, 1e6),
                (300, 100.0, 1e9, 100_000.0), (200, 100.0, 1e9, 50_000.0),
                (0, 100.0, 1e6, 1e6), (7, 3.5, 10.0, 1e6)]
        for args in grid:
            with self.subTest(args=args):
                self.assertEqual(backtest_engine._pyramid_add_qty(*args),
                                 pt._pyramid_add_qty(*args))


class AssumptionsDeclared(unittest.TestCase):
    def test_result_lists_assumptions(self):
        f = _frame(opens=[100, 100], closes=[100, 100])
        r = _run(f, [{"action": "HOLD", "confidence": 3}] * 2)
        a = r.get("assumptions") or {}
        for key in ("macro", "regime", "events", "signal_fill", "stop_fill"):
            self.assertIn(key, a)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
