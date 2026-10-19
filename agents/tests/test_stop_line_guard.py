"""止损线守卫 (2026-10-19): 止损在哪条线卖, 价格收复这条线之前不买回."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stop_line_guard as g  # noqa: E402


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = [patch.object(g, "LINES", self.tmp / "stop_lines.json"), patch.object(g, "BLOCKS", self.tmp / "blocks.jsonl")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_tqqq_1016_case(self):
        g.record("US.TQQQ", 68.50, "software_stop", 67.89)        # 14:58 ET 止损
        ok, why = g.check_buy("US.TQQQ", 67.86, "[WATCH_BUY_PROBE conf=5 win=pre-close core]")   # 15:46 想买回
        self.assertFalse(ok)
        self.assertIn("68.50", why)
        self.assertTrue((self.tmp / "blocks.jsonl").exists())
        self.assertTrue(g.check_buy("TQQQ", 69.0)[0])               # 收复 → 放行 (代码带不带 US. 都认)
        self.assertTrue(g.check_buy("US.TQQQ", 60.0)[0])            # 线已解除

    def test_equal_to_line_is_blocked(self):
        g.record("US.DRAM", 46.8, "software_stop")
        self.assertFalse(g.check_buy("US.DRAM", 46.8)[0])

    def test_rebalance_exempt_and_other_tickers(self):
        g.record("US.SOXL", 122.77, "trailing_stop", 103.59)
        self.assertTrue(g.check_buy("US.SOXL", 100, "[REBALANCE rebalance: 5% → 8%]")[0])
        self.assertTrue(g.check_buy("US.MSFT", 400)[0])
        self.assertFalse(g.check_buy("US.SOXL", 100, "[PYRAMID L2]")[0])

    def test_trailing_line(self):
        self.assertAlmostEqual(g.trailing_line(100, 0.08), 92.0)

    def test_errors_fail_open(self):
        (self.tmp / "stop_lines.json").write_text("{broken", encoding="utf-8")
        self.assertTrue(g.check_buy("US.TQQQ", 1.0)[0])               # 守卫自身出错不卡交易
        g.record("US.X", 0, "software_stop")                           # 非法线忽略
        self.assertTrue(g.check_buy("US.X", 1.0)[0])


class PlaceIntegration(unittest.TestCase):
    """_place 在现金筹措之前拦截, 且只拦买入. 注意: 不真正调用 _place 的下单路径 —
    那会往 signals/trade_log.jsonl 等实盘文件写记录 (2026-10-19 曾因此污染过一次)."""

    def test_blocked_buy_returns_before_funding(self):
        import paper_trader as pt
        with patch.object(g, "check_buy", return_value=(False, "止损线未收复")), \
             patch.object(pt, "_fund_buy_qty", side_effect=AssertionError("不应筹措现金")) as fund, \
             patch.object(pt, "logger"), patch.object(pt, "DRY_RUN", False):
            self.assertIsNone(pt._place("US.TQQQ", pt.TrdSide.BUY, 10, 67.86, tag="[WATCH_BUY_PROBE]"))
            fund.assert_not_called()

    def test_guard_only_on_buy_and_before_funding(self):
        import inspect
        import paper_trader as pt
        src = inspect.getsource(pt._place)
        i_guard, i_fund = src.index("stop_line_guard.check_buy"), src.index("_fund_buy_qty(")
        self.assertLess(i_guard, i_fund)
        self.assertIn("if side == TrdSide.BUY:", src[max(0, i_guard - 200):i_guard])


if __name__ == "__main__":
    unittest.main()
