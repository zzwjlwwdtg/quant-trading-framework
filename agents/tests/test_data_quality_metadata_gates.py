"""F03 regression (audit 2026-09-19): 未来/缺失时间戳应阻止新开仓, 但保护性
退出可豁免. 之前 missing_timestamp = warning, future_ts 被 max(0,...) 截为 0 →
两种情况 BUY 都放行.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import data_quality


class MissingTimestampTests(unittest.TestCase):

    def test_missing_ts_is_critical_not_warning(self):
        # 无 ts / 无 quote_ts 字段, 有价格
        market = {"price": 100.0}
        r = data_quality.assess_market_snapshot(market)
        self.assertEqual(r["status"], "critical",
                          "missing_timestamp 应升级到 critical 以阻止新开仓")
        self.assertFalse(r["allow_new_risk"])
        codes = [i["code"] for i in r["issues"]]
        self.assertIn("missing_timestamp", codes)

    def test_missing_ts_blocks_buy_via_order_gate(self):
        # order_data_gate: BUY 应被 block
        market = {"price": 100.0}
        r = data_quality.order_data_gate(market, "BUY")
        self.assertFalse(r["allow_order"])

    def test_missing_ts_still_allows_sell_via_order_gate(self):
        # 保护性退出豁免: SELL/REDUCE 应放行 (即使数据 stale)
        market = {"price": 100.0}
        r = data_quality.order_data_gate(market, "SELL")
        self.assertTrue(r["allow_order"], "保护性退出不应被 data quality 卡住")
        self.assertTrue(r["risk_reducing_override"])


class FutureTimestampTests(unittest.TestCase):

    def test_future_ts_is_critical(self):
        # 2099 年时间戳
        market = {"price": 100.0, "ts": "2099-01-01T00:00:00+00:00"}
        r = data_quality.assess_market_snapshot(market)
        self.assertEqual(r["status"], "critical",
                          "未来时间戳应 critical, 不该被 max(0,...) 假装成新鲜")
        codes = [i["code"] for i in r["issues"]]
        self.assertIn("future_timestamp", codes)
        self.assertFalse(r["allow_new_risk"])

    def test_future_ts_blocks_buy(self):
        market = {"price": 100.0, "ts": "2099-01-01T00:00:00+00:00"}
        r = data_quality.order_data_gate(market, "BUY")
        self.assertFalse(r["allow_order"])

    def test_future_ts_still_allows_sell(self):
        market = {"price": 100.0, "ts": "2099-01-01T00:00:00+00:00"}
        r = data_quality.order_data_gate(market, "SELL")
        self.assertTrue(r["allow_order"])

    def test_small_clock_skew_ts_not_flagged_as_future(self):
        # 时钟微偏 (30 秒) 不该 trigger — 用 3 分钟 (0.05h) 容差
        now = datetime.now(timezone.utc).astimezone()
        slightly_future = (now + timedelta(seconds=30)).isoformat()
        market = {"price": 100.0, "ts": slightly_future, "rsi_14": 50, "ma20": 100, "ma50": 100, "vol_ratio": 1.0}
        r = data_quality.assess_market_snapshot(market, now=now)
        codes = [i["code"] for i in r["issues"]]
        self.assertNotIn("future_timestamp", codes,
                          "30秒时钟偏差不应 trigger future_timestamp critical")


class StalePriceStillBlockedTests(unittest.TestCase):
    """确保原 stale_price critical 逻辑没被 F03 fix 打断."""

    def test_stale_price_still_critical(self):
        # 100 小时前的时间戳, 默认 max_age=36h
        now = datetime.now(timezone.utc).astimezone()
        old = (now - timedelta(hours=100)).isoformat()
        market = {"price": 100.0, "ts": old}
        r = data_quality.assess_market_snapshot(market, now=now)
        self.assertEqual(r["status"], "critical")
        codes = [i["code"] for i in r["issues"]]
        self.assertIn("stale_price", codes)


if __name__ == "__main__":
    unittest.main()
