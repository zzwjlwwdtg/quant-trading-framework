"""EVO-20261115-1: 事件→资产历史方向只读查表 + 复盘 banner.

验收标准:
 · event_direction_table.json 可加载, metadata/validation/distributions 齐全
 · classify_event_category 按 (event_type, 关键词) 命中 10 类中的一类
 · lookup_historical_direction 在 validation.passed_both=false 时拒绝返回数据
 · event_direction_banner 为匹配的 ticker 附历史分布, 并标出与新闻层方向相反的 mismatch_tickers
 · news_analyzer._normalize_item 自动把 banner 挂到 item['historical_direction']
 · trade_postmortem.event_direction_banners 汇总 news_structured 里的 banner
 · build_prompt 包含历史方向参考段落 (有命中时)
 · review_one 结果包含 event_direction_banner 字段 (含难判断场景, 至少有命中时列表非空)

banner 仅进 postmortem / verdict 校准, 不回传决策 (TECHNICAL_ONLY 继续生效).
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

import events_watch as ew
import news_analyzer as na
import trade_postmortem as tp


SELL_T = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class TableLoading(unittest.TestCase):
    """JSON 结构 + 关键字段存在性校验."""

    def setUp(self):
        ew._EVENT_DIR_TABLE = None   # 清缓存, 确保每次重新读文件

    def test_table_loads_and_has_required_sections(self):
        tbl = ew._load_event_direction_table()
        self.assertIn("metadata", tbl)
        self.assertIn("validation", tbl)
        self.assertIn("event_category_classifiers", tbl)
        self.assertIn("distributions", tbl)
        self.assertIn("ticker_to_asset", tbl)

    def test_metadata_marks_read_only_and_does_not_enter_decision(self):
        md = ew._load_event_direction_table().get("metadata") or {}
        self.assertTrue(md.get("read_only"))
        self.assertFalse(md.get("enters_decision"))

    def test_validation_passed_both_matches_gate_thresholds(self):
        v = ew._load_event_direction_table().get("validation") or {}
        self.assertGreaterEqual(v.get("sign_consistency_rate", 0),
                                 v.get("sign_consistency_threshold", 1))
        self.assertGreaterEqual(v.get("mislabel_rate_vs_history", 0),
                                 v.get("mislabel_rate_threshold", 1))
        self.assertTrue(v.get("passed_both"))

    def test_ten_or_more_categories(self):
        cats = ew._load_event_direction_table().get("distributions") or {}
        self.assertGreaterEqual(len(cats), 10,
                                "需至少 10 类历史事件覆盖 (提案 EVO-20261115-1)")

    def test_six_assets_per_category(self):
        for cat, dist in (ew._load_event_direction_table().get("distributions") or {}).items():
            self.assertEqual(sorted(dist.keys()),
                              sorted(["WTI", "BRENT", "GLD", "SPY", "EEM", "TLT"]),
                              f"{cat} 需覆盖 WTI/BRENT/GLD/SPY/EEM/TLT 6 资产")

    def test_each_distribution_t1_block_sums_to_one(self):
        for cat, dist in (ew._load_event_direction_table().get("distributions") or {}).items():
            for asset, hz in dist.items():
                t1 = hz.get("T1") or {}
                total = (t1.get("up_pct", 0) + t1.get("down_pct", 0)
                         + t1.get("flat_pct", 0))
                self.assertAlmostEqual(total, 1.0, places=2,
                    msg=f"{cat}/{asset}/T1 概率和需 ≈1.0, 实际 {total}")
                self.assertIn(t1.get("modal_direction"), ("up", "down", "flat"))


class ClassifyEventCategory(unittest.TestCase):
    """按 (父 event_type, 关键词) 命中 10 类中的一类."""

    def setUp(self):
        ew._EVENT_DIR_TABLE = None

    def test_iran_geopolitical(self):
        self.assertEqual(
            ew.classify_event_category("BREAKING",
                "US strike Iran's Qeshm Island oil terminal"),
            "IRAN_GEOPOLITICAL")

    def test_opec_cut(self):
        self.assertEqual(
            ew.classify_event_category("BREAKING",
                "OPEC+ announces output cut of 1M bpd"),
            "OPEC_CUT")

    def test_fomc_hawkish(self):
        self.assertEqual(
            ew.classify_event_category("FOMC",
                "Fed Chair signals higher for longer"),
            "FOMC_HAWKISH")

    def test_cpi_hot(self):
        self.assertEqual(
            ew.classify_event_category("CPI_RELEASE",
                "CPI rose 0.4% hotter than expected"),
            "CPI_HOT")

    def test_nfp_weak(self):
        self.assertEqual(
            ew.classify_event_category("NFP_RELEASE",
                "Payrolls fell short, unemployment rate rose"),
            "NFP_WEAK")

    def test_noise_does_not_cross_parent_type(self):
        """关键词 'iran' 在 CPI_RELEASE 下不能错判 (classifier 限制 父 type)."""
        self.assertIsNone(
            ew.classify_event_category("CPI_RELEASE",
                "Iran statistics office releases CPI"))

    def test_no_keyword_returns_none(self):
        self.assertIsNone(
            ew.classify_event_category("BREAKING",
                "unrelated chatter about stocks"))

    def test_none_event_type(self):
        self.assertIsNone(ew.classify_event_category("", "iran strike"))


class LookupHistoricalDirection(unittest.TestCase):
    def setUp(self):
        ew._EVENT_DIR_TABLE = None

    def test_lookup_returns_distribution(self):
        d = ew.lookup_historical_direction("IRAN_GEOPOLITICAL", "WTI")
        self.assertIsNotNone(d)
        self.assertEqual(d["modal_direction"], "up")
        self.assertGreater(d["up_pct"], d["down_pct"])

    def test_lookup_gated_by_oos_validation(self):
        """validation.passed_both=False 时, lookup 全部返回 None, 查表不上线."""
        ew._EVENT_DIR_TABLE = {
            "validation": {"passed_both": False},
            "distributions": {"X": {"WTI": {"T1": {"up_pct": 1.0}}}},
        }
        self.assertIsNone(ew.lookup_historical_direction("X", "WTI"))

    def test_lookup_unknown_asset(self):
        self.assertIsNone(ew.lookup_historical_direction("IRAN_GEOPOLITICAL", "ZZZ"))

    def test_ticker_to_asset_category_map(self):
        self.assertEqual(ew.ticker_to_asset_category("US.USO"), "WTI")
        self.assertEqual(ew.ticker_to_asset_category("TQQQ"), "SPY")
        self.assertEqual(ew.ticker_to_asset_category("GLD"), "GLD")
        self.assertIsNone(ew.ticker_to_asset_category("UNKNOWN_TICKER"))
        self.assertIsNone(ew.ticker_to_asset_category(""))


class EventDirectionBanner(unittest.TestCase):
    def setUp(self):
        ew._EVENT_DIR_TABLE = None

    def test_banner_builds_for_matched_event(self):
        b = ew.event_direction_banner(
            "BREAKING", "US strike Iran's Qeshm Island", ["USO", "SPY"],
            news_direction="bearish")
        self.assertIsNotNone(b)
        self.assertEqual(b["category"], "IRAN_GEOPOLITICAL")
        self.assertIn("USO", b["per_ticker"])
        self.assertEqual(b["per_ticker"]["USO"]["asset"], "WTI")
        # 新闻层 bearish, 历史 WTI modal=up → USO 应进 mismatch
        self.assertIn("USO", b["mismatch_tickers"])

    def test_banner_marks_disclaimer_as_non_decision(self):
        b = ew.event_direction_banner(
            "BREAKING", "OPEC cut 1M bpd", ["USO"], news_direction="bullish")
        self.assertIsNotNone(b)
        self.assertIn("不回传决策", b["disclaimer"])

    def test_banner_none_when_no_tickers_map(self):
        """classifier 命中, 但 tickers 不在 ticker_to_asset 里 → None."""
        b = ew.event_direction_banner(
            "BREAKING", "US strike Iran's Qeshm Island",
            ["UNKNOWN_FOO"], news_direction="bearish")
        self.assertIsNone(b)

    def test_banner_none_when_oos_not_passed(self):
        ew._EVENT_DIR_TABLE = {
            "validation": {"passed_both": False},
            "event_category_classifiers": {
                "X": {"label": "X", "keywords_any": ["xxx"],
                       "applies_when_event_type_in": ["BREAKING"]}},
            "distributions": {"X": {"WTI": {"T1": {"up_pct": 1.0}}}},
            "ticker_to_asset": {"USO": "WTI"},
        }
        b = ew.event_direction_banner("BREAKING", "xxx event", ["USO"])
        self.assertIsNone(b)

    def test_news_direction_neutral_no_mismatch(self):
        b = ew.event_direction_banner(
            "BREAKING", "US strike Iran's Qeshm Island", ["USO"],
            news_direction="neutral")
        self.assertIsNotNone(b)
        self.assertEqual(b["mismatch_tickers"], [])


class NewsAnalyzerAttachesBanner(unittest.TestCase):
    def setUp(self):
        ew._EVENT_DIR_TABLE = None

    def test_normalize_item_adds_historical_direction(self):
        raw = {
            "event_type": "BREAKING",
            "direction": "bearish", "magnitude": "large",
            "tickers_affected": ["USO"],
            "confidence": 8,
            "verbatim_evidence": "US strike Iran's Qeshm Island oil terminal",
        }
        item = na._normalize_item(raw, source="Yahoo USO")
        self.assertIn("historical_direction", item)
        hd = item["historical_direction"]
        self.assertIsNotNone(hd)
        self.assertEqual(hd["category"], "IRAN_GEOPOLITICAL")
        self.assertIn("USO", hd["per_ticker"])
        # 这是提案里的核心场景: 新闻层 bearish, 历史 WTI up → mismatch
        self.assertIn("USO", hd["mismatch_tickers"])

    def test_normalize_item_none_when_no_category_match(self):
        raw = {
            "event_type": "NOISE",
            "direction": "neutral", "magnitude": "small",
            "tickers_affected": ["AAPL"], "confidence": 1,
            "verbatim_evidence": "routine market chatter",
        }
        item = na._normalize_item(raw, source="Yahoo")
        self.assertIsNone(item["historical_direction"])

    def test_events_watch_import_failure_is_safe(self):
        """attach helper 吞异常, 不破坏 _normalize_item 常规路径."""
        with patch.object(na, "_attach_historical_direction", return_value=None):
            raw = {"event_type": "BREAKING", "direction": "bearish",
                    "magnitude": "large", "tickers_affected": ["USO"],
                    "confidence": 5, "verbatim_evidence": "iran strike"}
            item = na._normalize_item(raw, source="x")
            self.assertIsNone(item["historical_direction"])
            self.assertEqual(item["event_type"], "BREAKING")


class PostmortemBannerAggregation(unittest.TestCase):
    """trade_postmortem.event_direction_banners 汇总 news_structured 里的 banner."""

    def setUp(self):
        ew._EVENT_DIR_TABLE = None

    def _ns_with_iran(self):
        banner = ew.event_direction_banner(
            "BREAKING", "US strike Iran's Qeshm Island",
            ["USO"], news_direction="bearish")
        return {"items": [{
            "symbol": "USO", "title": "US strike Qeshm",
            "direction": "bearish", "event_type": "BREAKING",
            "evidence_category": "direct_at_decision",
            "historical_direction": banner,
        }]}

    def test_banner_aggregation_lists_matched_items(self):
        rows = tp.event_direction_banners(self._ns_with_iran(), primary_ticker="US.USO")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "IRAN_GEOPOLITICAL")
        self.assertIn("note_for_primary", rows[0])
        self.assertIn("USO", rows[0]["note_for_primary"])

    def test_banner_aggregation_empty_when_no_history(self):
        rows = tp.event_direction_banners(
            {"items": [{"symbol": "AAPL", "title": "noise",
                         "historical_direction": None}]})
        self.assertEqual(rows, [])

    def test_banner_aggregation_empty_on_missing_news_structured(self):
        self.assertEqual(tp.event_direction_banners(None), [])
        self.assertEqual(tp.event_direction_banners({}), [])


class PostmortemPromptIncludesHistory(unittest.TestCase):
    BASE_SELL = {
        "ticker": "US.USO", "qty": 10, "price": 50.0,
        "decision_ts": "2026-10-07T12:00:00+00:00",
        "tag": "[SOFTWARE-STOP trigger $49.50]", "decision": {},
    }

    def setUp(self):
        ew._EVENT_DIR_TABLE = None

    def test_prompt_includes_history_section_when_banner_present(self):
        banner = ew.event_direction_banner(
            "BREAKING", "US strike Iran's Qeshm Island",
            ["USO"], news_direction="bearish")
        ns = {"items": [{"symbol": "USO", "title": "Iran",
                          "direction": "bearish",
                          "event_type": "BREAKING",
                          "evidence_category": "direct_at_decision",
                          "historical_direction": banner}]}
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [], news_structured=ns)
        self.assertIn("历史同类事件 T+1 方向参考", p)
        self.assertIn("EVO-20261115-1", p)
        self.assertIn("IRAN_GEOPOLITICAL", p)
        self.assertIn("mismatch_tickers", p)

    def test_prompt_omits_history_section_when_no_banner(self):
        ns = {"items": [{"symbol": "AAPL", "title": "noise",
                          "historical_direction": None}]}
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [], news_structured=ns)
        self.assertNotIn("历史同类事件 T+1 方向参考", p)

    def test_prompt_still_has_existing_requirements(self):
        """新增段落不得破坏现有要求的结构."""
        p = tp.build_prompt(self.BASE_SELL, [], [], [], [])
        self.assertIn("正好 5 条", p)
        self.assertIn("正好 3 条局限性", p)
        self.assertIn("EVO-20261029-2", p)


class ReviewOneIncludesEventDirectionBanner(unittest.TestCase):
    GOOD = {
        "context": {"technical": "t", "news": "n"},
        "reasons": [{"type": "技术面", "reason": f"r{i}", "evidence": "e"}
                    for i in range(5)],
        "limitations": [{"limitation": f"l{i}", "why": "w"} for i in range(3)],
        "hindsight": {"summary": "difficult", "verdict": "难判断"},
    }

    def setUp(self):
        ew._EVENT_DIR_TABLE = None

    def _build_news_structured(self):
        banner = ew.event_direction_banner(
            "BREAKING", "US strike Iran's Qeshm Island",
            ["USO"], news_direction="bearish")
        return {
            "items": [{
                "symbol": "USO", "title": "US strike Qeshm",
                "direction": "bearish", "event_type": "BREAKING",
                "evidence_category": "direct_at_decision",
                "entered_decision": False,
                "historical_direction": banner,
            }],
            "fetched": 1, "fallback": False,
        }

    def test_event_direction_banner_present_even_for_hard_to_judge(self):
        """用户追加要求: 难判断复盘必须生成事件方向 banner (期望≥1)."""
        td = Path(tempfile.mkdtemp())
        (td / "run_20261007.log").write_text("", encoding="utf-8")
        sell = {
            "order_id": "9", "ticker": "US.USO", "qty": 1, "price": 1,
            "ts": "2026-10-07T12:00:00+00:00",
            "decision_ts": "2026-10-07T12:00:00+00:00",
            "tag": "[SOFTWARE-STOP]", "decision": {},
        }
        def ai(_):
            return (json.dumps(self.GOOD), "ok", "Claude", "")
        with patch.object(tp, "LOGS", td), \
             patch.object(tp, "news_from_cache", return_value=[]), \
             patch.object(tp, "news_from_rss", return_value=[]), \
             patch.object(tp, "closes_after", return_value=[]), \
             patch.object(tp, "collect_news_structured_for",
                           return_value=self._build_news_structured()):
            r = tp.review_one(sell, ai=ai, live_fetch=True)
        self.assertIn("event_direction_banner", r)
        banners = r["event_direction_banner"]
        self.assertIsInstance(banners, list)
        self.assertGreaterEqual(len(banners), 1,
            "难判断 verdict 下至少要有 1 条历史方向 banner (EVO-20261115-1)")
        self.assertEqual(banners[0]["category"], "IRAN_GEOPOLITICAL")
        # 校准反向占比: 新闻 bearish, 历史 WTI modal=up → USO 应在 mismatch
        self.assertIn("USO", banners[0]["mismatch_tickers"])

    def test_event_direction_banner_empty_list_when_no_hits(self):
        """没有 classifier 命中时 banner 为空列表, 但 key 必须存在."""
        td = Path(tempfile.mkdtemp())
        (td / "run_20261007.log").write_text("", encoding="utf-8")
        sell = {
            "order_id": "10", "ticker": "US.AAPL", "qty": 1, "price": 1,
            "ts": "2026-10-07T12:00:00+00:00",
            "decision_ts": "2026-10-07T12:00:00+00:00",
            "tag": "[SOFTWARE-STOP]", "decision": {},
        }
        empty_ns = {"items": [], "fetched": 0, "fallback": False}
        def ai(_):
            return (json.dumps(self.GOOD), "ok", "Claude", "")
        with patch.object(tp, "LOGS", td), \
             patch.object(tp, "news_from_cache", return_value=[]), \
             patch.object(tp, "news_from_rss", return_value=[]), \
             patch.object(tp, "closes_after", return_value=[]), \
             patch.object(tp, "collect_news_structured_for",
                           return_value=empty_ns):
            r = tp.review_one(sell, ai=ai, live_fetch=True)
        self.assertEqual(r["event_direction_banner"], [])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
