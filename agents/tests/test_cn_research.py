"""Offline public projection tests. Never use a real model, account or push."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cn_brief import research as R, brief as B

NOW = datetime(2026, 10, 20, 10, tzinfo=timezone(timedelta(hours=9)))
ROW = {"infoCode": "AP202610191234567890", "title": "需求结构变化，维持评级", "orgSName": "测试证券",
       "publishDate": "2026-10-19 09:00:00", "emRatingName": "增持", "stockCode": "000001", "stockName": "测试公司"}


class Research(unittest.TestCase):
    def test_title_digest_has_exact_attribution_not_imagined_body(self):
        rows = R.project([ROW], [dict(ROW, infoCode="AP202610192234567890")], [])
        self.assertEqual([r["report_type"] for r in rows], ["STOCK", "INDUSTRY"])
        self.assertEqual(rows[0]["one_sentence"], "题摘：" + ROW["title"])
        self.assertEqual(rows[0]["content_scope"], "TITLE_ONLY_NOT_FULL_REPORT")
        self.assertEqual(R.project([dict(ROW, infoCode="../token"), dict(ROW, orgSName="")], [], []), [])
        macro = R.project([], [], [dict(ROW, encodeUrl="1IxELJqHobtiKusKwOp2/yr64JMHN7A+VyIO/JxIG7o=")])[0]
        self.assertTrue(macro["id"].startswith("EM"))
        self.assertIn("zw_macresearch.jshtml?encodeUrl=", macro["url"])
        self.assertIn("%2F", macro["url"])

    def test_cached_public_only_no_ai_and_dates_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = {"generated_at": NOW.isoformat(), "research_reports": R.project([ROW], [], []),
                   "markdown": "PRIVATE RAW ANALYSIS", "ai": {"summary": "DO NOT SEND"}, "account": {"nav": 123},
                   "events": {"upcoming": [{"time_jst": (NOW + timedelta(hours=1)).isoformat(), "region": "中国",
                                              "title": "GDP", "pubVal": "未公布", "formerVal": "4.8", "indicateVal": "5.0"}]}}
            (root / "latest_now.json").write_text(json.dumps(raw), encoding="utf-8")
            before = (root / "latest_now.json").read_bytes()
            with patch.object(B, "ai_summary", side_effect=AssertionError("no AI")), \
                    patch.object(B.sources, "stock_reports", side_effect=AssertionError("no network")):
                result = R.read(root=root, now=NOW)
            self.assertEqual(result["status"], "OK")
            self.assertIsNone(result["ai_analysis"])
            self.assertEqual(result["source_policy"], "FREE_PUBLIC_ONLY")
            self.assertEqual(result["reports"][0]["subject"], "测试公司")
            self.assertEqual(result["reports"][0]["code"], "000001")
            self.assertEqual(result["macro_events"][0]["expected"], "5.0")
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertNotIn("DO NOT SEND", json.dumps(result))
            self.assertNotIn("account", result)
            self.assertEqual(before, (root / "latest_now.json").read_bytes())
            stale = R.read(root=root, now=NOW + timedelta(days=1))
            self.assertEqual(stale["status"], "STALE")
            self.assertEqual(stale["reports"], [])

    def test_missing_legacy_future_malformed_and_partial_do_not_fabricate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(R.read(root=root, now=NOW)["status"], "UNAVAILABLE")
            path = root / "latest_now.json"
            for raw in [{"generated_at": NOW.isoformat(), "markdown": "old report"},
                        {"generated_at": (NOW + timedelta(hours=1)).isoformat(), "research_reports": []}, []]:
                path.write_text(json.dumps(raw), encoding="utf-8")
                self.assertEqual(R.read(root=root, now=NOW)["status"], "UNAVAILABLE")
            path.write_text(json.dumps({"generated_at": NOW.isoformat(), "research_reports": [None], "events": []}), encoding="utf-8")
            self.assertEqual(R.read(root=root, now=NOW)["status"], "PARTIAL")

    def test_collector_retains_projection_even_when_optional_ai_disabled(self):
        with patch.object(B.sources, "stock_reports", return_value=[ROW]), \
                patch.object(B.sources, "industry_reports", return_value=[]), \
                patch.object(B.sources, "macro_reports", return_value=[]), \
                patch.object(B, "important_events", return_value=([], [])), \
                patch.object(B, "ai_summary") as ai:
            result = B.build_brief("now", now=NOW, use_ai=False, write_files=False)
        ai.assert_not_called()
        self.assertEqual(len(result["research_reports"]), 1)
        self.assertIn("券商题摘，未读正文，不含AI判断", result["markdown"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
