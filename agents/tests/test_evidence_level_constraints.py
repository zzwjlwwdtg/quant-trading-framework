"""EVO-20261029-2: 复盘消息证据等级约束.

验收标准:
 · news_structured 条目含 is_proxy / entered_decision / evidence_category
 · pub_utc 为完整 ISO UTC (非展示格式)
 · post_event_background 条目在提示词里明确禁止作为触发原因
 · proxy_at_decision 条目在提示词里注明映射限制
 · price_description_only 条目在提示词里标无新增催化剂
 · 实际触发原因仅来自规则标签, 不受证据等级影响
 · 所有 news_structured 条目 entered_decision=False (TECHNICAL_ONLY 铁律)
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import news_analyzer as na
import trade_postmortem as tp

SELL_T = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

# 四类场景: 前置直接/事后背景/前置代理/仅价格描述(代理NOISE)
RSS_BY_SYM = {
    "TQQQ": [
        {"title": "TQQQ breaks out on volume",
         "pubDate": "Tue, 07 Oct 2026 10:00:00 +0000",  # 10:00 < SELL_T 12:00 → 前置直接
         "desc": "breakout detail"},
        {"title": "TQQQ drops 2% after close",
         "pubDate": "Tue, 07 Oct 2026 13:00:00 +0000",  # 13:00 >= SELL_T → 事后背景
         "desc": "post-sell"},
    ],
    "QQQ": [
        {"title": "QQQ record high",
         "pubDate": "Tue, 07 Oct 2026 09:00:00 +0000",  # 09:00 < SELL_T, 代理 → proxy_at_decision
         "desc": "qqq detail"},
    ],
    "SPY": [
        {"title": "SPY noise routine update",
         "pubDate": "Tue, 07 Oct 2026 08:00:00 +0000",  # 08:00 < SELL_T, 代理, NOISE → price_description_only
         "desc": "spy noise"},
    ],
}

# analyze_rss_items 的 mock 返回 (保持顺序与候选一致)
PARSED_ITEMS_4 = [
    {"event_type": "BREAKING",   "direction": "bullish", "confidence": 7,
     "verbatim_evidence": "breaks out"},
    {"event_type": "BREAKING",   "direction": "bearish", "confidence": 6,
     "verbatim_evidence": "drops"},
    {"event_type": "BREAKING",   "direction": "bullish", "confidence": 5,
     "verbatim_evidence": "record high"},
    {"event_type": "NOISE",      "direction": "neutral", "confidence": 2,
     "verbatim_evidence": "routine"},
]


def _fake_fetch(sym):
    return RSS_BY_SYM.get(sym, [])


class EvidenceCategoryAssignment(unittest.TestCase):
    """collect_news_structured 正确为每条候选新闻打上证据等级标签."""

    def _run(self):
        parsed = {"items": PARSED_ITEMS_4, "fallback": False}
        with patch.object(na, "analyze_rss_items", return_value=parsed):
            return na.collect_news_structured(
                ["TQQQ", "QQQ", "SPY"], SELL_T,
                window_hours=48, fetch_rss=_fake_fetch)

    def test_pub_utc_is_iso_format(self):
        """pub_utc 必须是完整 ISO UTC 字符串 (含时区偏移), 不能是展示格式."""
        res = self._run()
        for it in res["items"]:
            self.assertRegex(
                it["pub_utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]",
                msg=f"pub_utc 格式不符 ISO UTC: {it['pub_utc']!r}")

    def test_entered_decision_always_false(self):
        """所有结构化新闻均未进入当时决策 (TECHNICAL_ONLY 铁律)."""
        res = self._run()
        for it in res["items"]:
            self.assertIs(it["entered_decision"], False)

    def test_direct_at_decision(self):
        """TQQQ 10:00 (< SELL_T 12:00), 主标的, BREAKING → direct_at_decision."""
        res = self._run()
        item = res["items"][0]
        self.assertEqual(item["evidence_category"], "direct_at_decision")
        self.assertFalse(item["is_proxy"])
        self.assertEqual(item["symbol"], "TQQQ")

    def test_post_event_background(self):
        """TQQQ 13:00 (>= SELL_T 12:00) → post_event_background."""
        res = self._run()
        item = res["items"][1]
        self.assertEqual(item["evidence_category"], "post_event_background")

    def test_proxy_at_decision(self):
        """QQQ 09:00 (< SELL_T), QQQ != TQQQ → proxy_at_decision."""
        res = self._run()
        item = res["items"][2]
        self.assertEqual(item["evidence_category"], "proxy_at_decision")
        self.assertTrue(item["is_proxy"])
        self.assertEqual(item["symbol"], "QQQ")

    def test_price_description_only(self):
        """SPY 08:00, 代理, event_type=NOISE → price_description_only."""
        res = self._run()
        item = res["items"][3]
        self.assertEqual(item["evidence_category"], "price_description_only")
        self.assertTrue(item["is_proxy"])

    def test_internal_pub_dt_not_in_output(self):
        """_pub_dt 是内部字段, 不应出现在最终输出的 merged 条目里."""
        res = self._run()
        for it in res["items"]:
            self.assertNotIn("_pub_dt", it)


class NewsEvidenceSummary(unittest.TestCase):
    """news_evidence_summary 正确统计各类别数量并输出约束标志."""

    def _items(self):
        return [
            {"evidence_category": "direct_at_decision",    "is_proxy": False, "symbol": "TQQQ"},
            {"evidence_category": "post_event_background",  "is_proxy": False, "symbol": "TQQQ"},
            {"evidence_category": "proxy_at_decision",      "is_proxy": True,  "symbol": "QQQ"},
            {"evidence_category": "price_description_only", "is_proxy": True,  "symbol": "SPY"},
        ]

    def test_total_and_by_category(self):
        s = tp.news_evidence_summary({"items": self._items()})
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["by_category"]["direct_at_decision"], 1)
        self.assertEqual(s["by_category"]["post_event_background"], 1)
        self.assertEqual(s["by_category"]["proxy_at_decision"], 1)
        self.assertEqual(s["by_category"]["price_description_only"], 1)

    def test_proxy_symbols_sorted(self):
        s = tp.news_evidence_summary({"items": self._items()})
        self.assertIn("QQQ", s["proxy_symbols"])
        self.assertIn("SPY", s["proxy_symbols"])
        self.assertNotIn("TQQQ", s["proxy_symbols"])

    def test_constraints_flags(self):
        s = tp.news_evidence_summary({"items": self._items()})
        c = s["constraints"]
        self.assertTrue(c["post_event_cannot_be_trigger"])
        self.assertFalse(c["entered_decision"])
        self.assertTrue(c["proxy_requires_mapping_disclaimer"])
        self.assertTrue(c["price_only_has_no_new_catalyst"])

    def test_empty_news_structured(self):
        s = tp.news_evidence_summary(None)
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["by_category"], {})
        self.assertEqual(s["proxy_symbols"], [])
        self.assertFalse(s["constraints"]["entered_decision"])

    def test_items_without_category_go_to_unknown(self):
        s = tp.news_evidence_summary({"items": [{"symbol": "X"}]})
        self.assertIn("unknown", s["by_category"])


class BuildPromptEvidenceConstraint(unittest.TestCase):
    """build_prompt 在证据等级约束段落中包含必要的禁止/限制说明."""

    BASE_SELL = {
        "ticker": "US.TQQQ", "qty": 10, "price": 50.0,
        "decision_ts": "2026-10-07T12:00:00+00:00",
        "tag": "[SOFTWARE-STOP trigger $49.50]", "decision": {},
    }

    def test_post_event_cannot_be_trigger_in_prompt(self):
        ns = {"items": [
            {"evidence_category": "post_event_background", "is_proxy": False,
             "symbol": "TQQQ", "event_type": "BREAKING"}]}
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [], news_structured=ns)
        self.assertIn("事后背景", p)
        self.assertIn("不得作为当时卖出理由", p)

    def test_proxy_mapping_disclaimer_in_prompt(self):
        ns = {"items": [
            {"evidence_category": "proxy_at_decision", "is_proxy": True,
             "symbol": "QQQ", "event_type": "BREAKING"}]}
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [], news_structured=ns)
        self.assertIn("映射限制", p)

    def test_price_only_no_catalyst_in_prompt(self):
        ns = {"items": [
            {"evidence_category": "price_description_only", "is_proxy": True,
             "symbol": "SPY", "event_type": "NOISE"}]}
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [], news_structured=ns)
        self.assertIn("无新增催化剂", p)

    def test_entered_decision_false_always_present(self):
        """即使 news_structured 为空, 也应显示 entered_decision=False 约束."""
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [], news_structured={"items": []})
        self.assertIn("entered_decision=False", p)

    def test_evo_id_in_prompt(self):
        """提示词包含提案 ID 便于追溯."""
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [])
        self.assertIn("EVO-20261029-2", p)

    def test_no_news_structured_no_error(self):
        """news_structured=None 时不抛错, 仍生成合法提示词."""
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [])
        self.assertIsInstance(p, str)
        self.assertGreater(len(p), 100)

    def test_existing_requirements_unchanged(self):
        """新增约束不得破坏原有的结构要求."""
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [])
        self.assertIn("正好 5 条", p)
        self.assertIn("正好 3 条局限性", p)
        self.assertIn("不得当作当时的卖出理由", p)
        self.assertIn("不要建议把这只标的拉黑", p)


class ReviewOneIncludesSummary(unittest.TestCase):
    """review_one 返回结果包含 news_evidence_summary 字段."""

    GOOD = {
        "context": {"technical": "t", "news": "n"},
        "reasons": [{"type": "技术面", "reason": f"r{i}", "evidence": "e"}
                    for i in range(5)],
        "limitations": [{"limitation": f"l{i}", "why": "w"} for i in range(3)],
        "hindsight": {"summary": "ok", "verdict": "合理"},
    }

    def test_news_evidence_summary_in_result(self):
        td = Path(tempfile.mkdtemp())
        (td / "run_20261007.log").write_text("", encoding="utf-8")
        sell = {
            "order_id": "7", "ticker": "US.TQQQ", "qty": 1, "price": 1,
            "ts": "2026-10-07T12:00:00+00:00",
            "decision_ts": "2026-10-07T12:00:00+00:00",
            "tag": "[SOFTWARE-STOP]", "decision": {},
        }
        fake_ns = {
            "items": [{
                "evidence_category": "post_event_background",
                "is_proxy": False, "symbol": "TQQQ",
                "event_type": "BREAKING", "entered_decision": False,
            }],
            "fetched": 1, "fallback": False,
        }
        def ai(_):
            return (json.dumps(self.GOOD), "ok", "Claude", "")
        with patch.object(tp, "LOGS", td), \
             patch.object(tp, "news_from_cache", return_value=[]), \
             patch.object(tp, "news_from_rss", return_value=[]), \
             patch.object(tp, "closes_after", return_value=[]), \
             patch.object(tp, "collect_news_structured_for", return_value=fake_ns):
            r = tp.review_one(sell, ai=ai, live_fetch=True)
        self.assertIn("news_evidence_summary", r)
        s = r["news_evidence_summary"]
        self.assertEqual(s["total"], 1)
        self.assertIn("post_event_background", s["by_category"])
        self.assertFalse(s["constraints"]["entered_decision"])

    def test_news_evidence_summary_with_live_fetch_false(self):
        """live_fetch=False 时 news_structured 为空占位, summary 也能正常返回."""
        td = Path(tempfile.mkdtemp())
        (td / "run_20261007.log").write_text("", encoding="utf-8")
        sell = {
            "order_id": "8", "ticker": "US.TQQQ", "qty": 1, "price": 1,
            "ts": "2026-10-07T12:00:00+00:00",
            "decision_ts": "2026-10-07T12:00:00+00:00",
            "tag": "[SOFTWARE-STOP]", "decision": {},
        }
        def ai(_):
            return (json.dumps(self.GOOD), "ok", "Claude", "")
        with patch.object(tp, "LOGS", td), \
             patch.object(tp, "news_from_cache", return_value=[]):
            r = tp.review_one(sell, ai=ai, live_fetch=False)
        s = r["news_evidence_summary"]
        self.assertEqual(s["total"], 0)
        self.assertFalse(s["constraints"]["entered_decision"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
