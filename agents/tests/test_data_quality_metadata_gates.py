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


class QuoteTimestampTests(unittest.TestCase):
    """broker 行情时间 (quote_ts) 应优先于信号生成时间 (ts)."""

    def setUp(self):
        self.now = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)

    def _codes(self, result):
        return [i["code"] for i in result["issues"]]

    def test_stale_quote_ts_blocks_buy_even_when_generated_now(self):
        market = {
            "price": 100.0,
            "ts": self.now.isoformat(),
            "quote_ts": (self.now - timedelta(hours=40)).isoformat(),
        }
        r = data_quality.assess_market_snapshot(market, now=self.now)
        self.assertIn("stale_price", self._codes(r))
        self.assertFalse(r["allow_new_risk"])
        self.assertFalse(data_quality.order_data_gate(market, "BUY", now=self.now)["allow_order"])
        sell = data_quality.order_data_gate(market, "SELL", now=self.now)
        self.assertTrue(sell["allow_order"], "保护性退出仍应放行")
        self.assertTrue(sell["risk_reducing_override"])

    def test_fresh_quote_ts_overrides_old_generation_ts(self):
        market = {
            "price": 100.0,
            "ts": (self.now - timedelta(hours=100)).isoformat(),
            "quote_ts": (self.now - timedelta(minutes=2)).isoformat(),
        }
        r = data_quality.assess_market_snapshot(market, now=self.now)
        self.assertNotIn("stale_price", self._codes(r))
        self.assertTrue(r["allow_new_risk"])

    def test_explicit_empty_quote_ts_is_missing_timestamp(self):
        market = {"price": 100.0, "ts": self.now.isoformat(), "quote_ts": ""}
        r = data_quality.assess_market_snapshot(market, now=self.now)
        self.assertIn("missing_timestamp", self._codes(r))
        self.assertFalse(r["allow_new_risk"])

    def test_legacy_snapshot_without_quote_ts_falls_back_to_ts(self):
        market = {"price": 100.0, "ts": (self.now - timedelta(minutes=5)).isoformat()}
        r = data_quality.assess_market_snapshot(market, now=self.now)
        self.assertNotIn("missing_timestamp", self._codes(r))
        self.assertTrue(r["allow_new_risk"])


class QuoteTsHelperTests(unittest.TestCase):
    """moomoo update_time 是 ET 墙钟, 必须带偏移输出."""

    def test_edt_wall_clock_converted_with_offset(self):
        got = data_quality.quote_ts_from_update_time("2026-10-22 12:28:37")
        self.assertEqual(got, "2026-10-22T12:28:37-04:00")
        parsed = datetime.fromisoformat(got)
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 16)

    def test_est_wall_clock_converted_with_offset(self):
        got = data_quality.quote_ts_from_update_time("2026-01-15 09:30:00")
        self.assertEqual(got, "2026-01-15T09:30:00-05:00")
        parsed = datetime.fromisoformat(got)
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 14)

    def test_aware_value_kept_as_is(self):
        got = data_quality.quote_ts_from_update_time("2026-10-22T12:28:37+00:00")
        self.assertEqual(got, "2026-10-22T12:28:37+00:00")

    def test_invalid_values_return_none(self):
        for value in (None, "", "NaT", "nan", "not-a-time"):
            self.assertIsNone(data_quality.quote_ts_from_update_time(value))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
