"""供给冲击日历 (2026-10-05): 增发 / 股东转售 / IPO 解禁 + 指数调仓日."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
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


class Classify(unittest.TestCase):
    def test_cover_text_debt_equity_atm_unknown(self):
        debt = ("<html><body>Prospectus Supplement. $1,000,000,000 aggregate principal amount of "
                "senior notes due 2032. Interest rate 5.25% per annum.</body></html>")
        equity = ("<html><body>Prospectus Supplement. 5,000,000 shares of our common stock. "
                  "The selling stockholders may offer shares.</body></html>")
        atm = ("<html><body>At-the-market offering. We entered into an equity distribution "
               "agreement. Shares of our common stock may be sold.</body></html>")
        self.assertEqual(sc.classify_cover_text(debt), "debt")
        self.assertEqual(sc.classify_cover_text(equity), "equity")
        self.assertEqual(sc.classify_cover_text(atm), "atm")
        self.assertEqual(sc.classify_cover_text("<html>Nothing relevant here</html>"), "unknown")

    def test_description_debt_fast_path_skips_document(self):
        calls = []
        kind = sc.classify_filing(
            {"form": "424B5", "primaryDocDescription": "Senior Notes due 2031",
             "primaryDocument": "d.htm", "accessionNumber": "1-2-3"},
            1, "ua", get_document=lambda url, ua: calls.append(url) or "equity")
        self.assertEqual(kind, "debt")
        self.assertEqual(calls, [])

    def test_fetch_failure_is_unknown_not_equity(self):
        kind = sc.classify_filing(
            {"form": "424B5", "primaryDocDescription": "424B5",
             "primaryDocument": "d.htm", "accessionNumber": "1-2-3"},
            1, "ua", get_document=lambda url, ua: None)
        self.assertEqual(kind, "unknown")

    def test_classifier_filters_debt_and_keeps_equity(self):
        s = sub([{"form": "10-K", "filingDate": "2026-01-10"},
                 {"form": "424B5", "filingDate": "2026-09-30", "accessionNumber": "debt-1",
                  "primaryDocument": "d.htm", "primaryDocDescription": "424B5"},
                 {"form": "424B5", "filingDate": "2026-09-29", "accessionNumber": "eq-1",
                  "primaryDocument": "d.htm", "primaryDocDescription": "424B5"}])
        stats: dict = {}
        ev = sc.events_from_submissions(
            s, date(2026, 10, 5),
            classify=lambda row, cik: "debt" if row["accessionNumber"] == "debt-1" else "equity",
            stats=stats)
        self.assertEqual([e["date"] for e in ev], ["2026-09-29"])
        self.assertEqual(ev[0]["classification"], "equity")
        self.assertEqual((stats["debt"], stats["equity"]), (1, 1))

    def test_424b7_never_sent_to_classifier(self):
        s = sub([{"form": "424B7", "filingDate": "2026-10-01", "accessionNumber": "1",
                  "primaryDocument": "d.htm"}])
        ev = sc.events_from_submissions(
            s, date(2026, 10, 5), classify=lambda row, cik: (_ for _ in ()).throw(AssertionError()))
        self.assertEqual([e["type"] for e in ev], ["secondary_sale"])


class DocumentCache(unittest.TestCase):
    def test_same_filename_different_issuer_not_shared(self):
        urls = [
            "https://www.sec.gov/Archives/edgar/data/1/acc1/424b5.htm",
            "https://www.sec.gov/Archives/edgar/data/2/acc2/424b5.htm",
        ]
        bodies = {urls[0]: b"<html>senior notes due 2030</html>",
                  urls[1]: b"<html>shares of common stock</html>"}

        class Resp:
            def __init__(self, body): self.body = body
            def read(self, n): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with tempfile.TemporaryDirectory() as td, \
             patch.object(sc, "DOC_CACHE", Path(td)), \
             patch("urllib.request.urlopen", lambda req, timeout=0: Resp(bodies[req.full_url])):
            first = sc._get_document(urls[0], "ua")
            second = sc._get_document(urls[1], "ua")
        self.assertEqual(sc.classify_cover_text(first), "debt")
        self.assertEqual(sc.classify_cover_text(second), "equity")


class Health(unittest.TestCase):
    TODAY = date(2026, 10, 5)

    def _data(self, age=0.1, status=None, run_status="ok"):
        return {"_age_days": age, "generated_at": "2026-10-05T00:00:00+00:00",
                "run_status": run_status,
                "status": {"CBRS": "ok", "NVDA": "ok", "MSFT": "ok", "DRAM": "ok"} if status is None else status,
                "tickers": {}, "market_days": [], "counts": {"expected": 4}}

    def test_ok_partial_and_failed(self):
        self.assertEqual(sc.health(self._data())["state"], "ok")
        self.assertFalse(sc.health(self._data())["block_equity"])
        partial = sc.health(self._data(status={"NVDA": "error: timeout", "MSFT": "ok", "DRAM": "ok"},
                                       run_status="partial"))
        self.assertEqual(partial["state"], "partial")
        self.assertFalse(partial["block_equity"])
        failed = sc.health(self._data(status={"NVDA": "error: x", "MSFT": "error: y"},
                                      run_status="failed"))
        self.assertEqual(failed["state"], "run_failed")
        self.assertTrue(failed["block_equity"])

    def test_missing_stale_no_status(self):
        self.assertTrue(sc.health({})["block_equity"])
        self.assertEqual(sc.health(self._data(age=5))["state"], "stale")
        self.assertEqual(sc.health({"_age_days": 0, "tickers": {}})["state"], "no_status")

    def test_no_equity_data_blocks(self):
        h = sc.health(self._data(status={"DRAM": "no_cik", "GLD": "ok"}, run_status="ok"))
        self.assertEqual(h["state"], "no_equity_data")
        self.assertTrue(h["block_equity"])

    def test_ticker_health(self):
        data = self._data(status={"NVDA": "error: x", "MSFT": "ok", "DRAM": "ok"})
        h = sc.health(data)
        self.assertEqual(sc.ticker_health("US.NVDA", data, h), "ticker_error")
        self.assertEqual(sc.ticker_health("US.MSFT", data, h), "ok")
        self.assertEqual(sc.ticker_health("US.DRAM", data, h), "not_equity")
        self.assertEqual(sc.ticker_health("US.NEWCO", data, h), "ticker_missing")
        self.assertEqual(sc.ticker_health("US.GLD", data, h), "not_equity")

    def test_is_equity_uses_registry_for_etfs(self):
        self.assertTrue(sc.is_equity("US.NVDA"))
        self.assertTrue(sc.is_equity("US.NEWCO"))       # 未注册 → 保守视为股票
        self.assertFalse(sc.is_equity("US.DRAM"))       # registry: sector_etf
        self.assertFalse(sc.is_equity("US.XLV"))
        self.assertFalse(sc.is_equity("US.GLD"))


class Load(unittest.TestCase):
    def setUp(self):
        self.old_out, self.old_cache = sc.OUT_PATH, dict(sc._CACHE)
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name) / "calendar.json"
        sc.OUT_PATH = self.p
        sc._CACHE.update(mtime=None, data=None)

    def tearDown(self):
        sc.OUT_PATH = self.old_out
        sc._CACHE.update(self.old_cache)
        self.td.cleanup()

    def _write(self, generated_at=None):
        self.p.write_text(json.dumps({
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tickers": {}, "status": {}, "market_days": []}), encoding="utf-8")

    def test_cached_age_is_recomputed(self):
        self._write()
        first = sc.load()
        self.assertLess(first["_age_days"], 0.01)
        with patch("supply_calendar.time.time", return_value=time.time() + 5 * 86400):
            second = sc.load()
        self.assertGreater(second["_age_days"], 4.9)    # 旧实现会冻结在 0

    def test_generated_at_older_than_mtime_is_stale(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
        self._write(old)
        self.assertGreater(sc.load()["_age_days"], 9)

    def test_missing_file(self):
        self.assertIsNone(sc.load())

    def test_load_reads_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(json.dumps({"tickers": {}}), encoding="utf-8")
            self.assertLess(sc.load(p)["_age_days"], 1)
            self.assertIsNone(sc.load(Path(td) / "missing.json"))


class Guard(unittest.TestCase):
    TODAY = date(2026, 10, 5)

    def _data(self, age=0.1, status=None, run_status="ok"):
        return {"_age_days": age, "generated_at": "2026-10-05T00:00:00+00:00",
                "tickers": {"CBRS": [{"type": "lockup_expiry", "date": "2026-10-07",
                                      "window": ["2026-10-02", "2026-10-08"]}]},
                "status": {"CBRS": "ok", "MSFT": "ok", "NVDA": "ok", "DRAM": "ok"} if status is None else status,
                "run_status": run_status,
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

    def test_missing_calendar_blocks_equity_only(self):
        import decision_agent as da
        with patch.object(sc, "load", return_value=None), \
             patch.object(sc, "notify_equity_block"):
            nvda = da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.NVDA")
            self.assertEqual(nvda["action"], "HOLD")
            self.assertEqual(nvda["supply_block"]["kind"], "missing")
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.DRAM")["action"],
                             "WATCH_BUY")
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.GLD")["action"],
                             "WATCH_BUY")
            self.assertEqual(da._apply_supply_event_guard({"action": "SELL"}, "US.NVDA")["action"], "SELL")

    def test_stale_calendar_blocks_equity(self):
        import decision_agent as da
        with patch.object(sc, "load", return_value=self._data(age=5)), \
             patch.object(sc, "notify_equity_block"):
            out = da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.MSFT")
            self.assertEqual(out["action"], "HOLD")
            self.assertEqual(out["supply_block"]["kind"], "stale")

    def test_per_ticker_error_blocks_only_that_ticker(self):
        import decision_agent as da
        data = self._data(status={"NVDA": "error: timeout", "MSFT": "ok", "DRAM": "ok"},
                          run_status="partial")
        with patch.object(sc, "load", return_value=data), patch.object(sc, "notify_equity_block"):
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.NVDA")["action"],
                             "HOLD")
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.MSFT")["action"],
                             "WATCH_BUY")

    def test_no_cik_blocks_equity_not_etf(self):
        import decision_agent as da
        data = self._data(status={"CBRS": "ok", "NVDA": "no_cik", "DRAM": "no_cik"},
                          run_status="partial")
        with patch.object(sc, "load", return_value=data), patch.object(sc, "notify_equity_block"):
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.NVDA")["action"],
                             "HOLD")
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.DRAM")["action"],
                             "WATCH_BUY")

    def test_reader_error_blocks_equity_only(self):
        import decision_agent as da
        with patch.object(sc, "load", side_effect=RuntimeError("boom")), \
             patch.object(sc, "notify_equity_block"):
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.NVDA")["action"],
                             "HOLD")
            self.assertEqual(da._apply_supply_event_guard({"action": "WATCH_BUY"}, "US.DRAM")["action"],
                             "WATCH_BUY")

    def test_options_flow_cannot_promote_supply_hold(self):
        import decision_agent as da
        flow = {"options_flow": {"stale": False, "positions": {"CBRS": {
            "score": 80, "direction": "bullish", "data_quality": "tape",
            "score_cap": 80, "action": "watch_buy"}}}}
        mkt = {"ticker": "US.CBRS", "pct_chg": 2.0, "trend": "up", "price": 30.0}
        with patch.object(sc, "load", return_value=self._data()), \
             patch.object(da, "_et_today", return_value=self.TODAY), \
             patch.object(sc, "notify_equity_block"):
            held = da._apply_supply_event_guard({"action": "WATCH_BUY", "confidence": 7}, "US.CBRS")
            out = da._apply_options_flow_guard(held, "US.CBRS", mkt, flow)
        self.assertEqual(out["action"], "HOLD")
        self.assertEqual(out["options_flow"]["score"], 80)

    def test_watchdog_job_registered(self):
        import _webui_watchdog as wd
        job = wd.JOBS["supply_calendar"]
        self.assertTrue((AGENTS_DIR / job["bat"]).exists())
        self.assertTrue(job["market_quiet"])


class Run(unittest.TestCase):
    TODAY = date(2026, 10, 5)

    def test_total_failure_marks_run_failed(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "calendar.json"

            def fake_get_json(url, ua):
                if "company_tickers_exchange" in url:
                    return {"data": [[1, "CBRS", "CBRS", "NYSE"], [2, "NVDA", "NVDA", "NASDAQ"]]}
                raise TimeoutError("simulated SEC outage")

            with patch.object(sc, "tracked_tickers", lambda: ["CBRS", "NVDA"]), \
                 patch.object(sc, "_get_json", fake_get_json), \
                 patch.object(sc, "_ua", lambda: "test@example.com"), \
                 patch.object(sc, "notify_equity_block"):
                data = sc.run(today=self.TODAY, out_path=out)
            loaded = sc.load(path=out)
            self.assertEqual(data["run_status"], "failed")
            self.assertEqual(sc.health(loaded)["state"], "run_failed")
            self.assertTrue(sc.health(loaded)["block_equity"])
            self.assertEqual(sc.active_events("CBRS", self.TODAY, loaded), [])

    def test_run_uses_cover_page_classification(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "calendar.json"

            def fake_get_json(url, ua):
                if "company_tickers_exchange" in url:
                    return {"data": [[1, "CBRS", "CBRS", "NYSE"], [2, "NVDA", "NVDA", "NASDAQ"]]}
                if "CIK0000000001" in url:
                    return sub([{"form": "424B5", "filingDate": "2026-09-30",
                                 "accessionNumber": "eq-1", "primaryDocument": "d.htm",
                                 "primaryDocDescription": "424B5"}], cik=1)
                return sub([{"form": "424B5", "filingDate": "2026-09-30",
                             "accessionNumber": "debt-1", "primaryDocument": "d.htm",
                             "primaryDocDescription": "424B5"}], cik=2)

            def fake_doc(url, ua):
                if "debt1" in url:
                    return "aggregate principal amount of senior notes due 2032"
                return "shares of our common stock by selling stockholders"

            with patch.object(sc, "tracked_tickers", lambda: ["CBRS", "NVDA"]), \
                 patch.object(sc, "_get_json", fake_get_json), \
                 patch.object(sc, "_get_document", fake_doc), \
                 patch.object(sc, "_ua", lambda: "test@example.com"), \
                 patch.object(sc, "notify_equity_block"):
                data = sc.run(today=self.TODAY, out_path=out)
            self.assertEqual(data["run_status"], "ok")
            self.assertEqual([e["classification"] for e in data["tickers"]["CBRS"]], ["equity"])
            self.assertNotIn("NVDA", data["tickers"])
            self.assertEqual(data["classification"]["NVDA"]["debt"], 1)
            self.assertEqual(data["classification"]["CBRS"]["equity"], 1)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
