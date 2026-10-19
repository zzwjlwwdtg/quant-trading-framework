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
    unittest.main()
