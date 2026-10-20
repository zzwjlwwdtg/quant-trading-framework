"""Synthetic fixtures only; neither trading nor a profitability backtest."""
import sys
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_stock as M

NOW = datetime(2026, 10, 20, 6, tzinfo=timezone.utc)  # CN 14:00, US 02:00 EDT


def bars(zone="Asia/Shanghai", end="2026-10-20", count=100):
    dates = pd.bdate_range(end=end, periods=count, tz=zone)
    prices = np.linspace(80, 100, count) + np.sin(np.arange(count))
    return pd.DataFrame({"Open": prices, "High": prices+2, "Low": prices-2,
                         "Close": prices, "Volume": [1000]*count}, index=dates)


def minutes():
    dates = pd.date_range("2026-10-20 13:57", periods=4, freq="min", tz="Asia/Shanghai")
    return pd.DataFrame({"Close": [100, 101, 102, 999], "Volume": [10., 20., 30., 999.]}, index=dates)


class Stock(unittest.TestCase):
    def test_symbol_allowlist_no_paths_or_mismatched_exchange(self):
        for raw, canonical in [("600519", "600519.SS"), ("000001.SZ", "000001.SZ"),
                               ("688001.SH", "688001.SS"), ("aapl", "AAPL"), ("US.BRK.B", "BRK-B")]:
            self.assertEqual(M.symbol(raw), canonical)
        for raw in ["../secrets", "600519.SZ", "123456", "00700.HK", "AAPL?token=x", "AAPL NVDA", "123456.SS"]:
            with self.assertRaises(ValueError):
                M.symbol(raw)

    def test_quote_excludes_forming_minute_and_keeps_units_previous_session(self):
        sample = bars()
        result = M.analyze("600519.SS", sample, minutes(), {}, NOW)
        q = result["quote"]
        self.assertEqual(q["price"], 102)
        self.assertEqual(q["volume"], 60)
        self.assertEqual(q["volume_unit"], "SHARES")
        self.assertEqual(q["as_of"], "2026-10-20T14:00:00+08:00")
        self.assertEqual(q["age_seconds"], 0)
        self.assertAlmostEqual(q["change_pct"], (102/sample.Close.iloc[-2]-1)*100, places=3)
        self.assertEqual(result["daily"]["as_of"], "2026-10-19")
        self.assertEqual(result["daily"]["volume_ratio"], 1)

    def test_no_quote_for_only_future_or_unfinished_minutes(self):
        self.assertIsNone(M.analyze("600519.SS", bars(), minutes().tail(1), {}, NOW)["quote"])
        for bad in [np.inf, np.nan, -1]:
            intra = minutes(); intra.iloc[0, 1] = bad
            self.assertIsNone(M.analyze("600519.SS", bars(), intra, {}, NOW)["quote"]["volume"])

    def test_corporate_action_abstains_from_raw_percent_and_adjusts_history(self):
        sample = bars(); sample["Adj Close"] = sample.Close/2
        sample["Dividends"] = 0.0; sample.iloc[-1, sample.columns.get_loc("Dividends")] = 1
        result = M.analyze("600519.SS", sample, minutes(), {}, NOW)
        self.assertIsNone(result["quote"]["change_pct"])
        self.assertIsNone(result["quote"]["previous_close"])
        self.assertAlmostEqual(result["daily"]["adjusted_close"], sample.Close.iloc[-2]/2)

    def test_prior20_mean_excludes_current_and_only_completed_daily(self):
        sample = bars(); sample.iloc[-1, sample.columns.get_loc("Volume")] = 2000
        result = M.analyze("600519.SS", sample, None, {}, NOW.replace(hour=8))
        self.assertEqual(result["daily"]["as_of"], "2026-10-20")
        self.assertEqual(result["daily"]["mean_volume_20_prior"], 1000)
        self.assertEqual(result["daily"]["volume_ratio"], 2)
        self.assertEqual(result["daily"]["volume_state"], "EXPANDED")

    def test_us_timezone_and_dst_close_not_china_clock(self):
        sample = bars("America/New_York")
        before = M.analyze("AAPL", sample, None, {}, NOW.replace(hour=20, minute=14))
        after = M.analyze("AAPL", sample, None, {}, NOW.replace(hour=20, minute=15))
        self.assertEqual(before["daily"]["as_of"], "2026-10-19")
        self.assertEqual(after["daily"]["as_of"], "2026-10-20")

    def test_repaired_bars_disable_volume_divergence_and_candidate(self):
        sample = bars(); sample["Repaired?"] = False; sample.iloc[-2, -1] = True
        result = M.analyze("600519.SS", sample, None, {}, NOW)["daily"]
        self.assertEqual(result["repaired_bars_60"], 1)
        self.assertIsNone(result["volume_ratio"])
        self.assertEqual(result["top_divergence"]["state"], "INSUFFICIENT")
        self.assertEqual(result["buy_signal"], "NONE")

    def test_invalid_history_abstains_instead_of_fabricating_signals(self):
        for value in [np.nan, np.inf, -1, 0]:
            sample = bars(); sample.iloc[-2, sample.columns.get_loc("Close")] = value
            self.assertIsNone(M.analyze("600519.SS", sample, None, {}, NOW)["daily"])
        sample = bars(); sample.index = sample.index.tz_localize(None)
        self.assertEqual(M.analyze("600519.SS", sample, None, {}, NOW)["status"], "UNAVAILABLE")
        self.assertIsNone(M.analyze("600519.SS", bars(count=30), None, {}, NOW)["daily"])
        self.assertIsNone(M.analyze("600519.SS", bars(end="2026-10-01"), None, {}, NOW)["daily"])
        self.assertIsNone(M.analyze("600519.SS", bars(end="2026-10-21"), None, {}, NOW)["daily"])

    def test_confirmed_divergence_uses_two_closed_right_bars(self):
        frame = bars(end="2026-10-19", count=60).rename(columns={"High": "high"})
        frame["high"] = 100.; frame["macd_dif"] = 1.
        frame.iloc[45, frame.columns.get_loc("high")] = 110
        frame.iloc[56, frame.columns.get_loc("high")] = 112
        frame.iloc[45, frame.columns.get_loc("macd_dif")] = 2
        result = M.divergence(frame)
        self.assertEqual(result["state"], "DETECTED")
        self.assertEqual(result["confirmed_on"], frame.index[58].date().isoformat())
        self.assertEqual(M.divergence(frame.iloc[:58])["state"], "INSUFFICIENT")
        frame.iloc[56, frame.columns.get_loc("macd_dif")] = 3
        self.assertEqual(M.divergence(frame)["state"], "NOT_DETECTED")

    def test_candidate_is_conjunction_not_macd_alone(self):
        sample = bars(); sample.loc[sample.index[-2], ["Open", "High", "Low", "Close", "Volume"]] = [150, 152, 148, 150, 2000]
        result = M.analyze("600519.SS", sample, None, {}, NOW)["daily"]
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["buy_signal"], "WATCH_ONLY")
        sample.loc[sample.index[-2], "Volume"] = 1000
        self.assertEqual(M.analyze("600519.SS", sample, None, {}, NOW)["daily"]["buy_signal"], "NONE")

    def test_pinned_provider_and_intraday_failure_preserves_daily(self):
        with patch.object(M, "version", return_value="1.2.0"), patch("yfinance.Ticker") as ticker:
            ticker.return_value.history.side_effect = [bars(end="2026-10-19"), OSError("secret")]
            with patch.object(M, "analyze", return_value={}) as analyze:
                M.collect("600519.SS")
                self.assertIsNone(analyze.call_args.args[2])
            self.assertTrue(ticker.return_value.history.call_args_list[0].kwargs["repair"])
        with patch.object(M, "version", return_value="9.9"), patch("yfinance.Ticker") as ticker:
            self.assertEqual(M.collect("AAPL")["status"], "UNAVAILABLE")
            ticker.assert_not_called()

    def test_bounded_cache_reaps_other_symbols_and_failures_have_cooldown(self):
        with patch.object(M, "_cache", M.OrderedDict()), patch.object(M, "_jobs", {}), patch.object(M._pool, "submit") as submit:
            a, b, c = Future(), Future(), Future()
            submit.side_effect = [a, b, c]
            self.assertEqual(M.read("AAPL")["status"], "PENDING")
            self.assertEqual(M.read("NVDA")["status"], "PENDING")
            self.assertEqual(M.read("MSFT")["status"], "BUSY")
            a.set_exception(OSError("must not leak")); b.set_result(M.empty("NVDA"))
            self.assertEqual(M.read("MSFT")["status"], "PENDING")
            self.assertEqual(set(M._jobs), {"MSFT"})
            self.assertEqual(M.read("AAPL")["status"], "UNAVAILABLE")
            self.assertEqual(submit.call_count, 3)
            result = M.read("NVDA"); result["source"] = "tampered"
            self.assertNotEqual(M.read("NVDA")["source"], "tampered")

    def test_cache_lru_is_bounded(self):
        cache = M.OrderedDict((f"X{i}", (M.time.monotonic(), M.empty(f"X{i}"))) for i in range(40))
        with patch.object(M, "_cache", cache), patch.object(M, "_jobs", {}), patch.object(M._pool, "submit", return_value=Future()):
            M.read("AAPL")
            self.assertEqual(len(cache), 32)


if __name__ == "__main__":
    unittest.main()
