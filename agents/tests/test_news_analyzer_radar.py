"""Opt-in radar prompt cannot change the default trading/postmortem parser."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import news_analyzer as na
import ai_prompt


class Mode(unittest.TestCase):
    def test_research_type_and_hypothesis_exist_only_in_opted_in_parser(self):
        raw={"event_type":"RESEARCH_NOTE","institution":"Broker","radar_hypothesis":{"proxy":"SMH","stance":"OUTPERFORM","thesis":"source-based"}}
        self.assertEqual(na._normalize_item(raw,"s")["event_type"],"NOISE")
        result=na._normalize_item(raw,"s",radar_mode=True)
        self.assertEqual(result["event_type"],"RESEARCH_NOTE")
        self.assertEqual(result["radar_hypothesis"]["proxy"],"SMH")

    def test_default_unchanged_and_radar_cache_isolated(self):
        prompts = []
        keys = []

        def query(prompt, **kwargs):
            prompts.append(prompt)
            self.assertEqual(kwargs["complexity"], "simple")
            return json.dumps({"items": [{"event_type": "NOISE"}]}), "ok", "test", ""

        def cache(key):
            keys.append(key)
            return None

        with patch.object(ai_prompt, "query_ai_cli", side_effect=query), \
                patch.object(na, "_read_cache", side_effect=cache), patch.object(na, "_write_cache"):
            for radar in (False, True):
                result = na.analyze_rss_items([{"title": "public title", "desc": "public excerpt"}],
                                              source="same-source", events_calendar=[], radar_mode=radar)
                self.assertFalse(result["fallback"])
        self.assertNotIn("本次仅供公开新闻聊天雷达", prompts[0])
        self.assertIn("本次仅供公开新闻聊天雷达", prompts[1])
        self.assertIn("不要运行工具或读取文件", prompts[1])
        self.assertNotEqual(keys[0], keys[1])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
