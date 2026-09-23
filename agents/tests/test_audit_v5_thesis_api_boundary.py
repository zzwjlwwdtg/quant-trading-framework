"""V5-04 audit (2026-09-23): api_thesis_state 边界必须明确返回状态,
不能静默 fallback 到 live thesis.

audit 复现场景:
- 历史空档 (gap between theses): as_of 落在 V1 retired 后 / V2 effective_from 前
  → 应 historical_unknown=True, 不返 live
- V2 提前失效后 (effective_to < retired_at): as_of 在 effective_to 之后
  → 应 historical_unknown=True, 不返 live
- 参数错误 (not-a-date): 无法解析 → 应 invalid_as_of=True, 不返 live
- 无 effective_from 的 archive + 远古查询: 无起点信息 → 应 historical_unknown=True
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import webui
import thesis_config


class V5_04_HistoricalGapBetweenTheses(unittest.TestCase):
    """空档 (V1 retired 后, V2 effective 前) 应 unknown, 不返 live."""

    def test_gap_after_v1_retired_before_v2_effective_returns_unknown(self):
        # V1 retired at 2026-09-01, V2 effective_from 2026-09-05
        # as_of = 2026-09-03 (gap) → 应 unknown
        # 现有 archive 里 V2 可能已经 retired (e.g., retired_at 2026-09-20).
        # 但 as_of 2026-09-03 < V2 effective_from 2026-09-05 → V2 不适用.
        # V1 retired 2026-09-01 < 2026-09-03 → V1 也不适用.
        # 空档 → historical_unknown.
        fake_retired = [
            {"retired_at": "2026-09-01T00:00:00Z",
             "thesis": {"version": "V1",
                        "effective_from": "2026-08-01",
                        "thesis_summary": "V1"}},
            {"retired_at": "2026-09-20T00:00:00Z",
             "thesis": {"version": "V2",
                        "effective_from": "2026-09-05",
                        "thesis_summary": "V2"}},
        ]
        with patch.object(thesis_config, "list_retired_theses", return_value=fake_retired), \
             patch.object(thesis_config, "summary", return_value={"version": "LIVE"}), \
             patch.object(thesis_config, "next_thesis_conjecture", return_value=None):
            r = webui.api_thesis_state(as_of="2026-09-03")
        # 关键 audit assertion: 不能返 LIVE
        self.assertNotEqual(r.get("current", {}).get("version"), "LIVE",
                            f"V5-04: gap 不能 fallback 到 LIVE, actual={r.get('current')}")
        self.assertTrue(r.get("historical_unknown"),
                        f"V5-04: gap → historical_unknown=True, actual={r}")

    def test_effective_to_expired_before_retired_at_returns_unknown(self):
        # V2 声明 effective_to=2026-09-06, retired_at=2026-09-10.
        # as_of=2026-09-08 → V2 已 self-invalidated, 但 archive 里没别的.
        # 现有 code: _historical_thesis_at 返 None (因为 effective_to 过);
        # 然后 fallback 判 is_unknown: as_of 早于 earliest_retired_at? 不是 (09-08 < 09-10 是 True, 所以 is_unknown=True).
        # BUT if archive contains multi entries and earliest is EARLIER than as_of, this test needs care.
        fake_retired = [
            {"retired_at": "2026-09-10T00:00:00Z",
             "thesis": {"version": "V2",
                        "effective_from": "2026-08-01",
                        "effective_to":   "2026-09-06",
                        "thesis_summary": "V2"}},
        ]
        with patch.object(thesis_config, "list_retired_theses", return_value=fake_retired), \
             patch.object(thesis_config, "summary", return_value={"version": "LIVE"}), \
             patch.object(thesis_config, "next_thesis_conjecture", return_value=None):
            r = webui.api_thesis_state(as_of="2026-09-08")
        # V2 已 self-invalidated 09-06, as_of 09-08 → 应 unknown, 不返 LIVE
        self.assertNotEqual(r.get("current", {}).get("version"), "LIVE",
                            f"V5-04: effective_to 已过应 unknown, actual={r.get('current')}")
        self.assertTrue(r.get("historical_unknown"),
                        f"V5-04: effective_to 已过 → historical_unknown, actual={r}")


class V5_04_InvalidAsOf(unittest.TestCase):
    """无法解析的 as_of 应返 invalid_as_of=True, 不 fallback."""

    def test_invalid_as_of_string_returns_invalid_marker(self):
        with patch.object(thesis_config, "list_retired_theses", return_value=[]), \
             patch.object(thesis_config, "summary", return_value={"version": "LIVE"}), \
             patch.object(thesis_config, "next_thesis_conjecture", return_value=None):
            r = webui.api_thesis_state(as_of="not-a-date")
        # 关键 audit assertion: 无效日期不能返 LIVE
        self.assertNotEqual(r.get("current", {}).get("version"), "LIVE",
                            f"V5-04: invalid as_of 不能 fallback LIVE, actual={r.get('current')}")
        self.assertTrue(r.get("invalid_as_of") or r.get("error"),
                        f"V5-04: invalid as_of 应显式 error, actual={r}")


class F5_CreatedAtCannotSubstituteEffectiveFrom(unittest.TestCase):
    """F5 audit followup (2026-09-23): 生产归档只有 created_at 没 effective_from,
    严格契约必须拒绝该 body, 不用 created_at 冒名生效日期."""

    def test_created_at_only_archive_returns_unknown(self):
        from webui import _historical_thesis_at
        # 只有 created_at (无 effective_from) → 无法证明生效时点 → None
        archive = [
            {"retired_at": "2026-09-15",
             "thesis": {"version": "CREATED_ONLY",
                        "created_at": "2026-08-24"}},
        ]
        r = _historical_thesis_at("2026-09-01", archive)
        self.assertIsNone(r,
                          f"F5: created_at 不能兜底 effective_from, actual={r}")


class V5_04_ArchiveWithoutEffectiveFrom(unittest.TestCase):
    """archive 里 thesis 无 effective_from + 远古查询 → unknown."""

    def test_ancient_query_against_archive_without_effective_from(self):
        # V1 retired 2026-09-15, 没有 effective_from 声明.
        # as_of = 2020-01-01 (远古): 无 effective_from 信息, 不能证明 V1 那时 alive.
        # 应 unknown, 不返 V1 body.
        fake_retired = [
            {"retired_at": "2026-09-15T00:00:00Z",
             "thesis": {"version": "V1",
                        "thesis_summary": "V1"}},  # 没有 effective_from
        ]
        with patch.object(thesis_config, "list_retired_theses", return_value=fake_retired), \
             patch.object(thesis_config, "summary", return_value={"version": "LIVE"}), \
             patch.object(thesis_config, "next_thesis_conjecture", return_value=None):
            r = webui.api_thesis_state(as_of="2020-01-01")
        # 关键: 远古查询不能返 V1 body (无 effective_from 证明)
        self.assertNotEqual(r.get("current", {}).get("version"), "V1",
                            f"V5-04: 无 effective_from + 远古查询 → 不能返 V1, actual={r.get('current')}")
        self.assertTrue(r.get("historical_unknown"),
                        f"V5-04: 无 effective_from + 远古 → historical_unknown, actual={r}")


if __name__ == "__main__":
    unittest.main()
