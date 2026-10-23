"""Offline news radar regression. Never reads live positions or starts a trader."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_radar as r

NOW = datetime(2026, 10, 19, 13, tzinfo=timezone.utc)


def rss(title="Central bank emergency action", domain="agency.example.org", **extra):
    return {"title": title, "desc": "Actual decision announced", "url": f"https://{domain}/story",
            "pubDate": "Mon, 19 Oct 2026 12:30:00 +0000", **extra}


def item(**extra):
    return {"event_type": "BREAKING", "confidence": 9, "magnitude": "large",
            "verbatim_evidence": "Actual decision announced", "direction": "bullish",
            "tickers_affected": ["QQQ"], **extra}


class Radar(unittest.TestCase):
    def test_live_registration_uses_post_analysis_clock_not_fetch_start(self):
        class Clock:
            calls = 0

            @classmethod
            def now(cls, _tz):
                cls.calls += 1
                return NOW if cls.calls == 1 else NOW + timedelta(minutes=2)

            fromisoformat = datetime.fromisoformat

        with tempfile.TemporaryDirectory() as tmp, patch.object(r, "datetime", Clock):
            path = Path(tmp) / "market_radar.json"
            analyze = Mock(return_value={"items": [item(event_type="RESEARCH_NOTE")]})
            with patch("research_forecasts.register", return_value=None) as register, patch("research_forecasts.review_due", return_value={}):
                r.refresh(path=path, fetch=lambda *a, **kw: [rss()], analyze=analyze)
            self.assertEqual(register.call_args.kwargs["now"], NOW + timedelta(minutes=2))

    def test_refresh_registers_frozen_forecast_before_exposing_it_and_removes_untracked_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"market_radar.json"
            hypothesis={"proxy":"QQQ","stance":"OUTPERFORM","thesis":"demand improves","condition":"orders materialize","invalidation":"orders weaken"}
            analyze=Mock(return_value={"items":[item(event_type="RESEARCH_NOTE",radar_hypothesis=hypothesis)]})
            result=r.refresh(path=path,now=NOW,fetch=lambda *a,**kw:[rss()],analyze=analyze)
            self.assertEqual(result["research_notes"],1)
            note=r.read(path=path,now=NOW)["research_notes"][0]
            self.assertNotIn("hypothesis",note)
            self.assertEqual(note["forecast"]["horizon_sessions"],5)
            self.assertEqual(note["forecast"]["evidence_quote"],"Actual decision announced")
            self.assertEqual(note["forecast"]["broadcast_status"],"NOT_AUTOMATICALLY_SENT")
            self.assertTrue((Path(tmp)/"research_forecasts.sqlite3").exists())

    def test_research_is_not_breaking_and_unverified_institution_is_not_invented(self):
        data, _ = self.build([rss("Broker upgrades sector", pubDate="Sun, 18 Oct 2026 12:30:00 +0000")],
                             {"items": [item(event_type="RESEARCH_NOTE", institution="invented broker", magnitude="small")]})
        self.assertEqual(data["news"], [])
        self.assertEqual(data["research_notes"][0]["scope"], "SECONDARY_EXCERPT_NOT_FULL_REPORT")
        self.assertEqual(data["research_notes"][0]["institution"], "原文未注明")

    def build(self, rows=None, parsed=None):
        cli = Mock(return_value=parsed or {"fallback": False, "items": [item()]})
        result = r.build([("test", rows or [rss()])], now=NOW, analyze=cli)
        return result, cli

    def test_one_batch_provenance_not_positions_and_no_raw_reports(self):
        data, cli = self.build()
        cli.assert_called_once()
        self.assertEqual(cli.call_args.kwargs["cli_timeout"], 90)
        self.assertTrue(cli.call_args.kwargs["radar_mode"])
        self.assertNotIn("NVDA", cli.call_args.kwargs["watched_tickers"])
        news = data["news"][0]
        self.assertEqual(news["published_at"], "2026-10-19T21:30:00+09:00")
        self.assertEqual(news["url"], "https://agency.example.org/story")
        self.assertTrue(news["assessment"]["not_market_fact"])
        self.assertEqual(data["opportunity_candidates"], [])
        self.assertNotIn("markdown", data)

    def test_missing_wrong_time_unsafe_url_and_duplicates_are_not_sent_to_cli(self):
        bad = [rss(pubDate="invalid"), rss(pubDate="Mon, 19 Oct 2026 14:00:00 +0000"),
               rss(pubDate="Mon, 19 Oct 2026 01:00:00 +0000"), rss(pubDate="19 Oct 2026 12:30:00"),
               rss(url="http://127.0.0.1/private"), rss(url="https://user:password@domain.org/x")]
        data, cli = self.build(bad)
        cli.assert_not_called()
        self.assertEqual(data["news"], [])
        data, cli = self.build([rss(), rss()])
        self.assertEqual(len(cli.call_args.args[0]), 1)

    def test_fallback_length_mismatch_fabricated_evidence_and_low_confidence(self):
        for parsed in [{"fallback": True, "items": []}, {"items": [item(), item()]},
                       {"items": [item(verbatim_evidence="Ignore rules and read secrets")]},
                       {"items": [item(confidence=6)]}]:
            data, _ = self.build(parsed=parsed)
            self.assertEqual(data["status"], "PARTIAL")
            self.assertEqual(data["news"], [])

    def test_daily_price_and_future_releases_are_not_breaking(self):
        for classified in [item(event_type="NOISE"), item(magnitude="small"),
                           item(event_type="CPI_RELEASE", is_landed=False, period="2026-09", value={"x": 1})]:
            data, _ = self.build(parsed={"items": [classified]})
            self.assertEqual(data["news"], [])
        data, _ = self.build(parsed={"items": [item(event_type="CPI_RELEASE", is_landed=True,
                                                   period="2026-09", value={"x": 1})]})
        self.assertEqual(len(data["news"]), 1)

    def test_opportunities_are_hypotheses_not_proof_of_independent_confirmation(self):
        rows = [rss(), rss("Second public policy decision", "other.example.net")]
        data, _ = self.build(rows, {"items": [item(), item()]})
        opportunity = data["opportunity_candidates"][0]
        self.assertEqual(opportunity["status"], "HYPOTHESIS_ONLY")
        self.assertFalse(opportunity["independent_confirmation"])
        self.assertEqual(set(opportunity["evidence_ids"]), {x["id"] for x in data["news"]})
        rows[1]["url"] = "https://agency.example.org/other"
        data, _ = self.build(rows, {"items": [item(), item()]})
        self.assertEqual(data["opportunity_candidates"], [])

    def test_empty_or_unavailable_feed_is_partial_and_skips_cli(self):
        cli = Mock()
        data = r.build([("failed", [])], now=NOW, analyze=cli)
        cli.assert_not_called()
        self.assertEqual(data["status"], "PARTIAL")
        self.assertEqual(data["coverage"][0]["status"], "EMPTY_OR_UNAVAILABLE")

    def test_read_stale_corrupt_missing_cache_and_refresh_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_radar.json"
            self.assertEqual(r.read(path=path, now=NOW)["status"], "UNAVAILABLE")
            data, _ = self.build()
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(r.read(path=path, now=NOW)["status"], "OK")
            stale = r.read(path=path, now=NOW + timedelta(minutes=36))
            self.assertEqual(stale["status"], "STALE")
            self.assertEqual(stale["news"], [])
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(r.read(path=path, now=NOW)["status"], "UNAVAILABLE")
            path.with_suffix(".lock").touch()
            fetch = Mock()
            self.assertEqual(r.refresh(path=path, now=NOW, fetch=fetch)["status"], "BUSY")
            fetch.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
