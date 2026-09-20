"""WP04 wave 5: /api/thesis_state ?as_of=YYYY-MM-DD 支持历史视图.

audit F05/WP04 精神: dashboard 应能显示"过去某天 thesis 是什么", 而不只是
'now'. 现在 as_of 参数会从 archive 找那天生效的 thesis body 展示.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class HistoricalAsOfTests(unittest.TestCase):

    def test_historical_lookup_before_first_retire(self):
        from webui import _historical_thesis_at
        retired = [
            {"retired_at": "2026-09-11T00:00:00Z",
             "thesis": {"version": "2026-Q3", "thesis_summary": "orig"}},
            {"retired_at": "2026-09-17T00:00:00Z",
             "thesis": {"version": "2026-Q3.1", "thesis_summary": "cpi_reprice"}},
        ]
        # 查询 2026-09-01 → 应返 2026-Q3 (first retired 是它, 说明 09-01 那时它 live)
        r = _historical_thesis_at("2026-09-01", retired)
        self.assertIsNotNone(r)
        self.assertEqual(r["version"], "2026-Q3")

    def test_historical_lookup_between_retires(self):
        from webui import _historical_thesis_at
        retired = [
            {"retired_at": "2026-09-11T00:00:00Z",
             "thesis": {"version": "2026-Q3", "thesis_summary": "orig"}},
            {"retired_at": "2026-09-17T00:00:00Z",
             "thesis": {"version": "2026-Q3.1", "thesis_summary": "cpi_reprice"}},
        ]
        # 09-15 是 Q3.1 时代 (它 09-17 才 retire, 所以 09-15 时它 live)
        r = _historical_thesis_at("2026-09-15", retired)
        self.assertEqual(r["version"], "2026-Q3.1")

    def test_historical_after_all_retires_returns_none(self):
        from webui import _historical_thesis_at
        retired = [
            {"retired_at": "2026-09-11T00:00:00Z",
             "thesis": {"version": "2026-Q3", "thesis_summary": "orig"}},
        ]
        # 09-20 晚于所有 retire → 应返 None (fall back to live)
        r = _historical_thesis_at("2026-09-20", retired)
        self.assertIsNone(r)

    def test_empty_retired_returns_none(self):
        from webui import _historical_thesis_at
        r = _historical_thesis_at("2026-09-15", [])
        self.assertIsNone(r)

    def test_api_thesis_state_with_as_of_returns_as_of_field(self):
        from webui import api_thesis_state
        r = api_thesis_state(as_of="2026-09-01")
        self.assertEqual(r.get("as_of"), "2026-09-01")

    def test_api_thesis_state_no_as_of_defaults_to_current(self):
        from webui import api_thesis_state
        r = api_thesis_state()
        self.assertIsNone(r.get("as_of"))
        # current 版本应含 "Q3" 表明真在返回 live current thesis
        self.assertIn("Q3", r["current"].get("version", ""))


if __name__ == "__main__":
    unittest.main()
