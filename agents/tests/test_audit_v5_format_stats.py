"""F2 audit followup (2026-09-23): format_stats 应按选定 primary 源渲染,
不能标题写 fills_replay_partial 却正文显示旧 cohort ledger 数字.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import cohort_tracker as ct


class F2_PartialAuthorityFormat(unittest.TestCase):

    def test_partial_authority_renders_fills_numbers_not_ledger(self):
        # audit 复现: fills_replay_partial 分支被误路由到 ledger 分支.
        # 构造: fills 有 unreconciled (partial), n_events>0; ledger 有一条亏损.
        # 期望: 标题 fills_replay_partial, 正文用 fills 数字 (0W/0L / total 0)
        # + unreconciled warning; 不能出现 ledger 的 -598.64 数字.
        fake_fills = {
            "n": 0, "n_roundtrips": 0, "n_winners": 0, "n_losers": 0,
            "win_rate": 0.0, "total_pnl_usd": 0.0,
            "n_events": 1, "n_tickers": 1,
            "authority": "fills_replay_partial",
            "warning": "1 tickers have unreconciled sells (10 shares total).",
            "unreconciled_tickers": ["US.TEST"],
            "unreconciled_sells_qty": 10,
            "positions": {},
        }
        fake_ledger = {
            "n": 1, "n_winners": 0, "n_losers": 1,
            "total_pnl_usd": -598.64, "win_rate": 0,
            "authority": "cohort_ledger_only",
        }
        with patch.object(ct, "stats_from_fills", return_value=fake_fills), \
             patch.object(ct, "stats",           return_value=fake_ledger), \
             patch.object(ct, "all_active_cohorts", return_value=[]):
            text = ct.format_stats(30)
        # 关键: primary render (标题之后, 双源分歧行之前) 不能出现 ledger 数字.
        # 双源分歧行是明确 diagnostic, 可以并列展示 fills=0 vs ledger=-598.64.
        divergence_marker = "双源分歧"
        primary_body = text.split(divergence_marker)[0]
        self.assertNotIn("-598.64", primary_body,
                          f"F2: primary 正文泄漏 ledger 数字 -598.64\n----\n{primary_body}")
        self.assertNotIn("已 close cohorts", primary_body,
                          f"F2: partial 被路由到旧 cohort ledger 分支\n----\n{primary_body}")
        # 应含 fills partial warning
        self.assertIn("unreconciled", text.lower(),
                        f"F2: 缺 unreconciled 警告\n----\n{text}")
        self.assertIn("fills_replay_partial", text,
                        f"F2: 标题必须写 fills_replay_partial\n----\n{text}")

    def test_clean_fills_still_renders_fills(self):
        # 无 unreconciled → authority fills_replay → 正常 render fills 数字
        fake_fills = {
            "n": 2, "n_roundtrips": 2, "n_winners": 2, "n_losers": 0,
            "win_rate": 100.0, "total_pnl_usd": 200.0,
            "n_events": 4, "n_tickers": 2,
            "authority": "fills_replay",
            "warning": None,
            "unreconciled_tickers": [], "unreconciled_sells_qty": 0,
            "positions": {},
        }
        with patch.object(ct, "stats_from_fills", return_value=fake_fills), \
             patch.object(ct, "stats", return_value={"n": 0, "total_pnl_usd": 0.0}), \
             patch.object(ct, "all_active_cohorts", return_value=[]):
            text = ct.format_stats(30)
        self.assertIn("+200.00", text,
                        f"F2 clean: 应 render fills total, actual\n----\n{text}")


if __name__ == "__main__":
    unittest.main()
