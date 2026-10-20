"""现金纪律 (2026-10-07 用户要求: 注意账户现金总值, 不能只买不卖).

规则 A: 买入必须有现金覆盖, 不够先卖 SHY, 仍不够缩量/放弃 (不受 SIM_ACTIVE 豁免).
规则 B: 交易窗口现金 < 0 → 依次卖 SHY、IEI 补回.
仓位基数: 账户净值 total_assets (不是含保证金的 power); 净值 <=0 不回退到 150 万.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import pandas as pd
import paper_trader as pt


def _view(cash, pending_buy=0.0, pending_sell=0.0, codes=()):
    return {"cash": cash, "nav": 1_000_000.0, "pending_buy": pending_buy,
            "pending_sell": pending_sell, "pending_sell_codes": set(codes),
            "available": cash - pending_buy + pending_sell - pt.CASH_FLOOR_USD}


class PlanFundingSells(unittest.TestCase):
    def test_order_ceil_and_shortfall(self):
        h = {"US.SHY": {"qty": 100, "price": 80.0}, "US.IEI": {"qty": 1000, "price": 110.0}}
        sells, rem = pt.plan_funding_sells(8_001.0, h, ("US.SHY", "US.IEI"))
        # SHY 全部 100 股 = 8000, 还差 1 → IEI 向上取整 1 股
        self.assertEqual([(s["code"], s["qty"]) for s in sells], [("US.SHY", 100), ("US.IEI", 1)])
        self.assertEqual(rem, 0.0)

    def test_insufficient_and_zero(self):
        sells, rem = pt.plan_funding_sells(10_000.0, {"US.SHY": {"qty": 10, "price": 80.0}}, ("US.SHY",))
        self.assertEqual(sells[0]["qty"], 10)
        self.assertAlmostEqual(rem, 9_200.0)
        self.assertEqual(pt.plan_funding_sells(0, {"US.SHY": {"qty": 10, "price": 80}}, ("US.SHY",)), ([], 0.0))
        self.assertEqual(pt.plan_funding_sells(50, {}, ("US.SHY",)), ([], 50.0))


class FundBuy(unittest.TestCase):
    def test_enough_cash_no_sells(self):
        with patch.object(pt, "_account_cash_view", return_value=_view(50_000)), \
             patch.object(pt, "_submit_funding_sells") as sub:
            self.assertEqual(pt._fund_buy_qty("US.MSFT", 10, 500.0), 10)
            sub.assert_not_called()

    def test_pending_buys_consume_cash_and_shy_funds_rest(self):
        # 现金 6000 但已有 2000 未成交买单 → 可用 4000; 买 5000 → 卖 SHY 13 股 (ceil(1000/80))
        with patch.object(pt, "_account_cash_view", return_value=_view(6_000, pending_buy=2_000)), \
             patch.object(pt, "_sellable_holdings", return_value={"US.SHY": {"qty": 500, "price": 80.0}}), \
             patch.object(pt, "_submit_funding_sells", side_effect=lambda s, r: [f"{x['code']} -{x['qty']}" for x in s]) as sub:
            self.assertEqual(pt._fund_buy_qty("US.MSFT", 10, 500.0), 10)
            self.assertEqual(sub.call_args[0][0][0]["qty"], 13)

    def test_negative_cash_no_shy_skips(self):
        with patch.object(pt, "_account_cash_view", return_value=_view(-300_000)), \
             patch.object(pt, "_sellable_holdings", return_value={}), \
             patch.object(pt, "_submit_funding_sells", return_value=[]):
            self.assertEqual(pt._fund_buy_qty("US.MSFT", 10, 500.0), 0)

    def test_partial_shy_shrinks_qty(self):
        with patch.object(pt, "_account_cash_view", return_value=_view(1_000)), \
             patch.object(pt, "_sellable_holdings", return_value={"US.SHY": {"qty": 25, "price": 80.0}}), \
             patch.object(pt, "_submit_funding_sells", side_effect=lambda s, r: [f"{x['code']} -{x['qty']}" for x in s]):
            # 1000 + 25*80=2000 → 3000 // 500 = 6 股
            self.assertEqual(pt._fund_buy_qty("US.MSFT", 10, 500.0), 6)

    def test_unknown_cash_fail_closed(self):
        with patch.object(pt, "_account_cash_view", return_value=None):
            self.assertEqual(pt._fund_buy_qty("US.MSFT", 10, 500.0), 0)

    def test_buying_shy_itself_not_funded_by_shy(self):
        with patch.object(pt, "_account_cash_view", return_value=_view(0)), \
             patch.object(pt, "_sellable_holdings", return_value={"US.SHY": {"qty": 500, "price": 80.0}}) as hold, \
             patch.object(pt, "_submit_funding_sells", return_value=[]):
            self.assertEqual(pt._fund_buy_qty("US.SHY", 10, 80.0), 0)
            self.assertEqual(hold.call_args[0][0], ())


class PlaceGate(unittest.TestCase):
    def test_buy_blocked_before_any_order_even_in_sim_active(self):
        with patch.object(pt, "DRY_RUN", False), \
             patch.object(pt, "is_sim_active_trading", return_value=True), \
             patch.object(pt, "_fund_buy_qty", return_value=0), \
             patch.object(pt, "_portfolio_what_if") as gate, \
             patch.object(pt, "_ctx_get") as ctx:
            self.assertIsNone(pt._place("US.MSFT", pt.TrdSide.BUY, 10, 500.0))
            gate.assert_not_called()
            ctx.assert_not_called()

    def test_sell_path_skips_cash_check(self):
        with patch.object(pt, "DRY_RUN", True), \
             patch.object(pt, "_fund_buy_qty") as fund, \
             patch.object(pt, "_portfolio_what_if", return_value={"allow_order": True}), \
             patch.object(pt, "_log_trade"), patch.object(pt, "append_execution_event"):
            pt._place("US.SHY", pt.TrdSide.SELL, 10, 80.0)
            fund.assert_not_called()


class RestoreCash(unittest.TestCase):
    def test_sells_shy_then_iei_and_skips_pending(self):
        with patch.object(pt, "_account_cash_view", return_value=_view(-10_000, codes=("US.SHY",))), \
             patch.object(pt, "_sellable_holdings", return_value={"US.IEI": {"qty": 1000, "price": 110.0}}) as hold, \
             patch.object(pt, "_submit_funding_sells", side_effect=lambda s, r: [f"{x['code']} -{x['qty']}" for x in s]):
            done = pt.restore_cash_floor()
            self.assertEqual(hold.call_args[0][0], ("US.IEI",))   # SHY 已有在途卖单 → 不重复
            self.assertEqual(done, ["US.IEI -91"])                 # ceil(10000/110)

    def test_no_action_when_cash_ok(self):
        with patch.object(pt, "_account_cash_view", return_value=_view(5_000)), \
             patch.object(pt, "_submit_funding_sells") as sub:
            self.assertEqual(pt.restore_cash_floor(), [])
            sub.assert_not_called()

    def test_orchestrator_calls_restore_in_trade_windows(self):
        src = (AGENTS_DIR / "orchestrator.py").read_text(encoding="utf-8")
        body = src[src.index("def run_cycle("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("restore_cash_floor()", body)
        self.assertIn("if window in TRADE_WINDOWS", body)


class SizingBase(unittest.TestCase):
    def _ctx(self, total_assets, power):
        ctx = MagicMock()
        ctx.accinfo_query.return_value = (pt.RET_OK, pd.DataFrame([{"total_assets": total_assets, "power": power}]))
        return ctx

    def test_uses_nav_not_margin_power(self):
        with patch.object(pt, "_power_cache", None), patch.object(pt, "_ctx_get", return_value=self._ctx(1_276_598.0, 941_693.0)):
            self.assertEqual(pt._get_account_power(), 1_276_598.0)

    def test_exhausted_does_not_fall_back_to_1_5m(self):
        with patch.object(pt, "_power_cache", None), patch.object(pt, "_ctx_get", return_value=self._ctx(0.0, 0.0)):
            self.assertEqual(pt._get_account_power(), 0.0)

    def test_query_failure_uses_fallback(self):
        ctx = MagicMock()
        ctx.accinfo_query.side_effect = RuntimeError("OpenD down")
        with patch.object(pt, "_power_cache", None), patch.object(pt, "_ctx_get", return_value=ctx):
            self.assertEqual(pt._get_account_power(), pt.ACCOUNT_POWER_FALLBACK)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
