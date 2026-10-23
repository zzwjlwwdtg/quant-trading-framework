"""Fixed public research rules; all prices are synthetic fixtures, never track records."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_outlook as M

NOW = datetime(2026, 10, 20, 11, tzinfo=timezone(timedelta(hours=9)))


def bars(end=110, dates=None):
    dates = dates if dates is not None else pd.bdate_range(end="2026-10-19", periods=35)
    return pd.DataFrame({"Close": np.linspace(100, end, len(dates)), "Volume": [100] * len(dates)}, index=dates)


def frames(region="CN", rising=True):
    spec = M.UNIVERSES[region]
    result = {s: bars(110 if rising else 80) for s in spec["indices"]}
    result.update({s: bars(140 if n < 2 else 101) for n, s in enumerate(spec["sectors"])})
    return result


class Outlook(unittest.TestCase):
    def test_history_reuses_pinned_library_repair_only_for_china(self):
        with patch("importlib.metadata.version", return_value="1.2.0"), patch("yfinance.Ticker") as ticker:
            for symbol, repair in [("510300.SS", True), ("159915.SZ", True), ("SPY", False)]:
                M.history(symbol)
                ticker.assert_called_with(symbol)
                ticker.return_value.history.assert_called_with(period="3mo", interval="1d", auto_adjust=True, repair=repair, timeout=8)

    def test_reconstructed_daily_prices_keep_provenance_and_never_fake_volume_confirmation(self):
        sample = bars()
        sample["Repaired?"] = False
        sample.loc[sample.index[-1], "Repaired?"] = True
        row = M.observe(sample, NOW, "CN")
        self.assertEqual(row["as_of"], "2026-10-19")
        self.assertEqual(row["repaired_bars_21d"], 1)
        self.assertIsNone(row["volume_ratio"])
        sample["Repaired?"] = False
        self.assertEqual(M.observe(sample, NOW, "CN")["volume_ratio"], 1)

    def test_fixed_market_tone_and_candidates_with_correct_baselines(self):
        for region, benchmark in [("CN", "510300.SS"), ("US", "SPY")]:
            result = M.build(region, frames(region), NOW)
            self.assertEqual((result["regime"], result["benchmark"], result["status"]), ("RISK_ON", benchmark, "OK"))
            self.assertEqual(len(result["directions"]), 2)
            for row in result["directions"]:
                self.assertGreater(row["excess_20d_pp"], 2)
                self.assertFalse(row["catalyst_verified"])
                self.assertEqual(row["status"], "WATCH_ONLY")
            self.assertEqual(M.build(region, frames(region, False), NOW)["regime"], "RISK_OFF")

    def test_missing_broad_price_or_unmatched_session_abstains_not_generic_advice(self):
        sample = frames()
        sample.pop("159915.SZ")
        result = M.build("CN", sample, NOW)
        self.assertEqual(result["regime"], "INSUFFICIENT")
        self.assertEqual(result["directions"], [])
        sample = frames()
        # Same final date but a missing intermediate session is not a comparable 20-day window.
        sample["512480.SS"] = bars(150, pd.bdate_range(end="2026-10-19", periods=36).delete(20))
        result = M.build("CN", sample, NOW)
        self.assertNotIn("512480.SS", [r["symbol"] for r in result["directions"]])
        self.assertEqual(M.build("CN", {}, NOW)["regime"], "INSUFFICIENT")

    def test_unfinished_today_future_bad_prices_and_old_data_are_not_current(self):
        dates = pd.bdate_range(end="2026-10-20", periods=35)
        sample = bars(120, dates)
        self.assertEqual(M.observe(sample, NOW, "CN")["as_of"], "2026-10-19")
        self.assertEqual(M.observe(sample, NOW.replace(hour=17), "CN")["as_of"], "2026-10-20")
        for value in [np.nan, np.inf, -2, 0]:
            sample = bars(); sample.iloc[-1, 0] = value
            self.assertIsNone(M.observe(sample, NOW, "CN"))
        self.assertIsNone(M.observe(bars(), NOW + timedelta(days=7), "CN"))

    def test_empty_sector_pool_not_forced_and_volume_is_not_money_flow(self):
        sample = {s: bars() for s in M.UNIVERSES["CN"]["indices"]}
        sample.update({s: bars(80) for s in M.UNIVERSES["CN"]["sectors"]})
        result = M.build("CN", sample, NOW)
        self.assertEqual(result["directions"], [])
        self.assertIn("不是资金净流入", "".join(result["limitations"]))
        sample["510300.SS"]["Volume"] = np.nan
        self.assertIsNone(M.build("CN", sample, NOW)["indices"][0]["volume_ratio"])

    def test_lagged_price_is_background_only_and_never_active_potential_candidates(self):
        result = M.build("CN", frames(), NOW + timedelta(days=1))
        self.assertEqual(result["freshness"], "LAGGED_BACKGROUND")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["directions"], [])

    def test_refresh_append_observations_only_on_change_and_cache_read_never_fetches(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "outlook.json"
            data = {**frames("CN"), **frames("US")}
            calls = []
            def loader(symbol):
                calls.append(symbol)
                return data[symbol]
            M.refresh(cache=cache, load=loader, now=NOW)
            self.assertEqual(len(calls), 18)
            journal = cache.with_suffix(".history.jsonl")
            self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 2)
            M.refresh(cache=cache, load=loader, now=NOW + timedelta(minutes=1))
            self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 2)
            before = cache.read_bytes()
            with patch.object(M, "history", side_effect=AssertionError("GET must not fetch")):
                result = M.read(cache=cache, now=NOW + timedelta(minutes=2))
            self.assertEqual(result["status"], "OK")
            self.assertEqual(cache.read_bytes(), before)
            stale = M.read(cache=cache, now=NOW + timedelta(hours=1))
            self.assertEqual((stale["status"], stale["directions"]), ("STALE", []))
            with self.assertRaises(ValueError):
                M.read("account", cache=cache, now=NOW)

    def test_missing_corrupt_partial_and_lock_are_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "outlook.json"
            self.assertEqual(M.read(cache=cache, now=NOW)["status"], "UNAVAILABLE")
            cache.write_text("[]", encoding="utf-8")
            self.assertEqual(M.read(cache=cache, now=NOW)["directions"], [])
            M.refresh(cache=cache, load=lambda s: None, now=NOW)
            self.assertEqual(M.read(cache=cache, now=NOW)["regime"], "INSUFFICIENT")
            cache.with_suffix(".lock").touch()
            with patch.object(M, "history", side_effect=AssertionError("locked")):
                self.assertEqual(M.refresh(cache=cache), {"status": "BUSY"})


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
