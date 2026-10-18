"""EVO-20261014-2: ±48h 候选新闻一次性批量结构化.

严守 feedback_news_pipeline (必须经 CLI 结构化) 和 feedback_technical_only_mode
(消息面不进决策). 这里只验证收集/过滤/批量调用的正确性, 不验证下游归档
(归档行为在 test_trade_postmortem.py 里覆盖)."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import news_analyzer as na


SELL_T = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
RSS_BY_SYM = {
    "MULL": [
        # 窗口内, 保留
        {"title": "MULL plunges on fraud report", "desc": "fraud detail",
         "pubDate": "Tue, 06 Oct 2026 14:00:00 +0000"},
        # 同标题重复, 去重
        {"title": "MULL plunges on fraud report", "desc": "dup",
         "pubDate": "Tue, 06 Oct 2026 15:00:00 +0000"},
        # 窗口外 (太远), 丢弃
        {"title": "Old news out of window", "desc": "",
         "pubDate": "Mon, 01 Jan 2026 00:00:00 +0000"},
    ],
    "QQQ": [
        # 窗口内, 保留
        {"title": "QQQ hits new high", "desc": "",
         "pubDate": "Wed, 07 Oct 2026 03:00:00 +0000"},
    ],
    "BAD_PUB": [
        # pubDate 解析失败, 丢弃
        {"title": "garbage pubdate item", "desc": "x", "pubDate": "not-a-date"},
    ],
}


def _fake_fetch(sym):
    return RSS_BY_SYM.get(sym, [])


class CollectNewsStructured(unittest.TestCase):
    def test_window_dedupe_and_single_cli_batch(self):
        parsed = {"items": [
            {"event_type": "BREAKING", "direction": "bearish",
             "confidence": 8, "verbatim_evidence": "fraud report"},
            {"event_type": "NOISE", "direction": "neutral",
             "confidence": 2, "verbatim_evidence": "new high"},
        ], "fallback": False}
        with patch.object(na, "analyze_rss_items", return_value=parsed) as cli:
            res = na.collect_news_structured(
                ["MULL", "QQQ"], SELL_T, window_hours=48,
                max_items=10, fetch_rss=_fake_fetch)
        cli.assert_called_once()                        # 一笔卖出 ≤ 1 次 CLI
        passed_items = cli.call_args[0][0]              # 批量喂进去的候选
        self.assertEqual(len(passed_items), 2)          # 去重 + 窗口过滤后 2 条
        self.assertEqual(res["fetched"], 2)
        self.assertEqual(len(res["items"]), 2)
        self.assertEqual(res["items"][0]["symbol"], "MULL")
        self.assertEqual(res["items"][0]["event_type"], "BREAKING")
        self.assertEqual(res["items"][0]["pub_utc"], "2026-10-06 14:00")
        self.assertEqual(res["items"][1]["symbol"], "QQQ")
        self.assertFalse(res["fallback"])

    def test_empty_candidates_skips_cli(self):
        """全部不在窗口内 → 直接返回空, 不浪费 CLI 调用."""
        with patch.object(na, "analyze_rss_items") as cli:
            res = na.collect_news_structured(
                ["BAD_PUB"], SELL_T, window_hours=48, fetch_rss=_fake_fetch)
        cli.assert_not_called()
        self.assertEqual(res["items"], [])
        self.assertEqual(res["fetched"], 0)
        self.assertFalse(res["fallback"])

    def test_fetch_exception_isolated_per_symbol(self):
        """某个 symbol 的 RSS 抓取抛错不能影响其它 symbol 的收集."""
        def flaky_fetch(sym):
            if sym == "MULL":
                raise RuntimeError("yahoo 500")
            return RSS_BY_SYM.get(sym, [])
        parsed = {"items": [{"event_type": "NOISE", "direction": "neutral",
                             "confidence": 2, "verbatim_evidence": "x"}],
                  "fallback": False}
        with patch.object(na, "analyze_rss_items", return_value=parsed):
            res = na.collect_news_structured(
                ["MULL", "QQQ"], SELL_T, fetch_rss=flaky_fetch)
        self.assertEqual(res["fetched"], 1)
        self.assertEqual(res["items"][0]["symbol"], "QQQ")

    def test_cli_fallback_propagates(self):
        """CLI 返回 fallback=True 时, 结果字段也标 fallback=True (调用方知道这笔证据不足)."""
        with patch.object(na, "analyze_rss_items",
                           return_value={"items": [], "fallback": True}):
            res = na.collect_news_structured(
                ["MULL"], SELL_T, fetch_rss=_fake_fetch)
        self.assertTrue(res["fallback"])
        self.assertEqual(res["items"], [])
        self.assertEqual(res["fetched"], 1)

    def test_max_items_cap(self):
        """候选过多时按 max_items 截断, 避免一次 CLI prompt 过长."""
        many = [{"title": f"MULL news {i}", "desc": "",
                 "pubDate": "Tue, 06 Oct 2026 14:00:00 +0000"}
                for i in range(20)]
        parsed = {"items": [{"event_type": "NOISE", "direction": "neutral",
                             "confidence": 1, "verbatim_evidence": "x"}
                            for _ in range(5)],
                  "fallback": False}
        with patch.object(na, "analyze_rss_items",
                           return_value=parsed) as cli:
            res = na.collect_news_structured(
                ["MULL"], SELL_T, max_items=5,
                fetch_rss=lambda s: many if s == "MULL" else [])
        passed_items = cli.call_args[0][0]
        self.assertEqual(len(passed_items), 5)
        self.assertEqual(res["fetched"], 5)


if __name__ == "__main__":
    unittest.main()
