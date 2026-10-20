"""供给冲击日历 (2026-10-05): 增发 / 股东转售 / IPO 解禁 + 指数调仓日."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import supply_calendar as sc


def sub(rows, cik=123):
    keys = ("form", "filingDate", "accessionNumber", "primaryDocument", "primaryDocDescription")
    return {"cik": str(cik), "filings": {"recent": {k: [r.get(k, "") for r in rows] for k in keys}}}


class Parse(unittest.TestCase):
    TODAY = date(2026, 10, 5)

    def test_secondary_and_offering_windows(self):
        s = sub([{"form": "10-K", "filingDate": "2026-02-20"},
                 {"form": "424B7", "filingDate": "2026-10-01", "accessionNumber": "0001-26-1",
                  "primaryDocument": "d1.htm"},
                 {"form": "424B5", "filingDate": "2026-09-30", "primaryDocDescription": "424B5"},
                 {"form": "424B5", "filingDate": "2026-09-29", "primaryDocDescription": "Senior Notes due 2031"},
                 {"form": "424B4", "filingDate": "2026-06-01"}])      # 有 10-K 在前 → 增发, 但已过 30 天
        ev = sc.events_from_submissions(s, self.TODAY)
        types = [(e["type"], e["date"]) for e in ev]
        self.assertIn(("secondary_sale", "2026-10-01"), types)
        self.assertIn(("offering", "2026-09-30"), types)
        self.assertNotIn("2026-09-29", [e["date"] for e in ev])       # 债券排除
        self.assertEqual(len(ev), 2)
        sec = [e for e in ev if e["type"] == "secondary_sale"][0]
        self.assertEqual(sec["window"], ["2026-10-01", "2026-10-05"])  # 申报日起 3 个交易日 (跳过周末)
        self.assertIn("/123/0001261/d1.htm", sec["url"])

    def test_ipo_lockup(self):
        s = sub([{"form": "424B4", "filingDate": "2026-04-10"},
                 {"form": "10-Q", "filingDate": "2026-05-15"}])
        ev = sc.events_from_submissions(s, self.TODAY)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["type"], "lockup_expiry")
        self.assertEqual(ev[0]["date"], "2026-10-07")                 # +180 天
        self.assertEqual(ev[0]["window"], ["2026-10-02", "2026-10-08"])

    def test_future_filings_ignored(self):
        s = sub([{"form": "424B7", "filingDate": "2026-10-06"}])
        self.assertEqual(sc.events_from_submissions(s, self.TODAY), [])

    def test_market_flow_days(self):
        days = sc.market_flow_days(date(2026, 10, 5), horizon_days=120)
        d = {(e["type"], e["date"]) for e in days}
        self.assertIn(("sp500_rebalance_quad_witching", "2026-12-18"), d)
        self.assertIn(("russell_reconstitution", "2026-12-11"), d)
        self.assertIn(("quarter_end", "2026-12-31"), d)


class Guard(unittest.TestCase):
    def _data(self, age=0.1):
        return {"_age_days": age, "tickers": {"CBRS": [{"type": "lockup_expiry", "date": "2026-10-07",
                                                        "window": ["2026-10-02", "2026-10-08"]}]},
                "market_days": [{"type": "sp500_rebalance_quad_witching", "date": "2026-12-18"}]}

    def test_active_events_and_staleness(self):
        self.assertEqual(len(sc.active_events("US.CBRS", date(2026, 10, 5), self._data())), 1)
        self.assertEqual(sc.active_events("US.CBRS", date(2026, 10, 9), self._data()), [])
        self.assertEqual(sc.active_events("US.CBRS", date(2026, 10, 5), self._data(age=5)), [])
        self.assertEqual(len(sc.flow_days_near(date(2026, 12, 17), self._data())), 1)

    def test_decision_guard(self):
        import decision_agent as da
        with patch.object(sc, "load", return_value=self._data()), \
             patch.object(da, "_et_today", return_value=date(2026, 10, 5)):
            out = da._apply_supply_event_guard({"action": "WATCH_BUY", "confidence": 5, "reason": "x"}, "US.CBRS")
            self.assertEqual(out["action"], "HOLD")
            self.assertEqual(out["demoted_from"], "WATCH_BUY")
            self.assertIn("lockup_expiry", out["reason"])
            # 卖出 / 观望不受影响; 其他股票不受影响
            self.assertEqual(da._apply_supply_event_guard({"action": "SELL"}, "US.CBRS")["action"], "SELL")
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.MSFT")["action"],
                             "WATCH_BUY")
        with patch.object(sc, "load", return_value=self._data()), \
             patch.object(da, "_et_today", return_value=date(2026, 12, 18)):
            out = da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.MSFT")
            self.assertEqual(out["action"], "WATCH_BUY")
            self.assertEqual(out["flow_day"], ["sp500_rebalance_quad_witching 2026-12-18"])

    def test_backtest_context_skipped(self):
        import decision_agent as da
        class Ctx:
            is_backtest = True
        with patch.object(sc, "load", side_effect=AssertionError("live calendar read")):
            out = da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.CBRS", Ctx())
        self.assertEqual(out["action"], "WATCH_BUY")

    def test_load_reads_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(json.dumps({"tickers": {}}), encoding="utf-8")
            self.assertLess(sc.load(p)["_age_days"], 1)
            self.assertIsNone(sc.load(Path(td) / "missing.json"))

    def test_watchdog_job_registered(self):
        import _webui_watchdog as wd
        job = wd.JOBS["supply_calendar"]
        self.assertTrue((AGENTS_DIR / job["bat"]).exists())
        self.assertTrue(job["market_quiet"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
