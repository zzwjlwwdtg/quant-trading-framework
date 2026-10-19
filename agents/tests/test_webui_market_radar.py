"""Real loopback HTTP routing, mocked public cache; no AI, account calls or production port."""
import json
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webui
import market_radar
import news_analyzer
import research_forecasts


class Route(unittest.TestCase):
    def test_cache_only_endpoint_and_read_catalog(self):
        expected = {"version": 1, "scope": "PUBLIC_NEWS_NOT_QUOTES", "status": "UNAVAILABLE",
                    "news": [], "opportunity_candidates": []}
        with patch.object(market_radar, "read", return_value=expected) as read, \
                patch.object(news_analyzer, "analyze_rss_items") as ai, \
                patch.object(research_forecasts, "read", return_value={"status":"NO_FORECASTS"}) as review, \
                patch.object(research_forecasts, "review_due") as calculate:
            server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/api/bot/radar", timeout=3) as response:
                    self.assertEqual(json.load(response), expected)
                with urllib.request.urlopen(base + "/api/bot", timeout=3) as response:
                    directory = json.load(response)
                entries = [row for row in directory["endpoints"] if row["path"] == "/api/bot/radar"]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["class"], "read")
                read.assert_called_once_with()
                with urllib.request.urlopen(base + "/api/bot/forecasts", timeout=3) as response:
                    self.assertEqual(json.load(response),{"status":"NO_FORECASTS"})
                self.assertEqual([x["class"] for x in directory["endpoints"] if x["path"]=="/api/bot/forecasts"],["read"])
                review.assert_called_once_with()
                calculate.assert_not_called()
                ai.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
