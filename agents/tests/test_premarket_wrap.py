"""盘前快讯: 时段/当期判定、时间窗去重、AI 引用回填与退化路径 (全部离线)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import premarket_wrap as pw

# 2026-10-13 08:00 EDT = 12:00 UTC = 21:00 JST
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _rfc(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def fake_fetch(url: str, source: str) -> list[dict]:
    fresh = _rfc(NOW - timedelta(hours=2))
    if "Federal" in url or "Federal%20Reserve" in url:
        return [
            {"title": "Fed's Waller signals patience on rates - Reuters", "url": "https://r/1", "desc": "", "pubDate": fresh, "source": "Reuters"},
            {"title": "Fed’s Waller signals patience on rates - Bloomberg", "url": "https://b/1", "desc": "", "pubDate": fresh, "source": "Bloomberg"},
            {"title": "Old Fed story - CNBC", "url": "https://c/old", "desc": "", "pubDate": _rfc(NOW - timedelta(days=3)), "source": "CNBC"},
        ]
    if "Nikkei" in url:
        return [{"title": "Nikkei hits record as yen slides - Nikkei Asia", "url": "https://n/1", "desc": "", "pubDate": fresh, "source": "Nikkei Asia"}]
    if "s=NVDA" in url:
        return [{"title": "Nvidia unveils new AI chip", "url": "https://y/nvda", "desc": "<p>details</p>", "pubDate": fresh, "source": source}]
    return []


class Window(unittest.TestCase):
    def test_edition_and_window(self):
        self.assertEqual(pw.edition_for(NOW), "2026-10-13")
        self.assertTrue(pw.in_generation_window(NOW))
        self.assertFalse(pw.in_generation_window(datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)))  # 10:00 ET 已开盘
        self.assertFalse(pw.in_generation_window(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)))  # 周六

    def test_company_tickers_map_leveraged_etfs_to_anchor_stocks(self):
        tickers = pw.company_tickers()
        self.assertIn("NVDA", tickers)
        self.assertIn("MU", tickers)
        for etf in ("TQQQ", "SOXL", "GLD", "SHY", "USO"):
            self.assertNotIn(etf, tickers)


class Selection(unittest.TestCase):
    def test_filters_old_and_duplicate_titles(self):
        items = pw.select_items(pw.collect_headlines(fake_fetch), NOW)
        titles = [it["title"] for it in items]
        self.assertEqual(sum("Waller" in t for t in titles), 1)
        self.assertFalse(any("Old Fed" in t for t in titles))
        self.assertEqual([it["id"] for it in items], list(range(1, len(items) + 1)))
        nvda = next(it for it in items if it.get("ticker") == "NVDA")
        self.assertEqual(nvda["section"], "companies")


class Build(unittest.TestCase):
    def test_ai_output_is_bound_to_cited_items(self):
        seen_prompt = {}

        def ai(prompt: str):
            seen_prompt["p"] = prompt
            return json.dumps({
                "headline": "美联储鸽派信号叠加 AI 芯片新品，期货小幅走高",
                "sections": {
                    "macro": [{"text": "Waller 称降息需耐心", "refs": [1], "impact": "中性", "tickers": []},
                              {"text": "编造的新闻", "refs": [999], "impact": "利多"}],
                    "companies": [{"text": "英伟达发布新 AI 芯片", "refs": [3], "impact": "利多", "tickers": ["NVDA", "bad ticker"]}],
                    "asia": [{"text": "日经创新高，日元走弱", "refs": [2], "impact": "WRONG"}],
                },
                "watch_today": ["10:00 消费者信心"],
            }), "ok", "Claude"

        wrap = pw.build_wrap(NOW, fetch=fake_fetch, ai=ai)
        self.assertEqual(wrap["status"], "ok")
        self.assertTrue(wrap["ai"])
        self.assertIn("只能使用下列新闻", seen_prompt["p"])
        sections = {s["key"]: s for s in wrap["sections"]}
        self.assertEqual([s["key"] for s in wrap["sections"]], ["macro", "geo", "companies", "asia"])
        self.assertEqual(len(sections["macro"]["items"]), 1)  # 引用不存在的 999 被丢弃
        self.assertEqual(sections["macro"]["items"][0]["sources"][0]["url"], "https://r/1")
        self.assertEqual(sections["companies"]["items"][0]["tickers"], ["NVDA"])
        self.assertEqual(sections["asia"]["items"][0]["impact"], "中性")

    def test_cli_failure_falls_back_to_headlines(self):
        wrap = pw.build_wrap(NOW, fetch=fake_fetch, ai=lambda prompt: (None, "timeout", "Claude"))
        self.assertEqual(wrap["status"], "fallback")
        self.assertFalse(wrap["ai"])
        self.assertTrue(any(s["items"] for s in wrap["sections"]))

    def test_no_news_is_empty(self):
        wrap = pw.build_wrap(NOW, fetch=lambda url, source: [], ai=lambda p: self.fail("AI must not run"))
        self.assertEqual(wrap["status"], "empty")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "premarket_wrap.json"
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def builder(self, status: str):
        def build(now):
            self.calls += 1
            return {"edition": pw.edition_for(now), "generated_at": now.isoformat(), "status": status, "sections": [], "watch_today": []}
        return build

    def test_generates_once_per_edition(self):
        get = lambda now: pw.get_premarket_wrap(now, path=self.path, is_trading_day=lambda d: True, builder=self.builder("ok"))
        get(NOW)
        get(NOW + timedelta(minutes=30))
        self.assertEqual(self.calls, 1)
        self.assertTrue((self.path.parent / "premarket_wraps" / "2026-10-13.json").exists())

    def test_fallback_retries_after_delay_and_outside_window_returns_last(self):
        get = lambda now, status="fallback": pw.get_premarket_wrap(now, path=self.path, is_trading_day=lambda d: True, builder=self.builder(status))
        get(NOW)
        get(NOW + timedelta(minutes=5))
        self.assertEqual(self.calls, 1)
        get(NOW + timedelta(minutes=25))
        self.assertEqual(self.calls, 2)
        out = get(NOW + timedelta(hours=5))  # 已开盘: 不再生成
        self.assertEqual(self.calls, 2)
        self.assertEqual(out["edition"], "2026-10-13")

    def test_holiday_does_not_generate(self):
        out = pw.get_premarket_wrap(NOW, path=self.path, is_trading_day=lambda d: False, builder=self.builder("ok"))
        self.assertEqual(self.calls, 0)
        self.assertEqual(out["status"], "market_closed")
        self.assertEqual(pw.load_wrap(self.path)["edition"], "2026-10-13")


if __name__ == "__main__":
    unittest.main()
