"""交易主线: 强美元 (2026-10-08 用户设定)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import usd_theme as ut


def fake_fetch(trend):
    def f(tk, period="2y"):
        idx = pd.bdate_range(end="2026-10-07", periods=400)
        base = np.linspace(100, 100, len(idx))
        if tk == "DX-Y.NYB":
            base = base * np.r_[np.ones(len(idx) - 20), np.linspace(1, 1 + trend, 20)]
        if tk == "EEM":
            base = base * np.r_[np.ones(len(idx) - 20), np.linspace(1, 0.95, 20)]
        return pd.Series(base, index=idx)
    return f


class UsdTheme(unittest.TestCase):
    def test_metrics_and_strength(self):
        m = ut.compute_metrics(fake_fetch(0.04))
        self.assertAlmostEqual(m["dxy_20d_pct"], 4.0, places=1)
        self.assertGreaterEqual(m["dxy_20d_rank_1y"], 0.99)
        self.assertAlmostEqual(m["eem_vs_spy_20d_pp"], -5.0, places=1)
        self.assertEqual(ut.theme_strength(m), "兑现中: 美元强势且非美市场跑输")
        m2 = ut.compute_metrics(fake_fetch(-0.03))
        self.assertEqual(ut.theme_strength(m2), "美元回落, 主线暂未兑现")

    def test_thesis_invalidation_conditions(self):
        from thesis_config import check_invalidation
        cfg = json.loads((AGENTS_DIR / "signals" / "thesis_config.json").read_text(encoding="utf-8"))
        ids = {c["id"] for c in cfg["invalidation_conditions"]}
        self.assertTrue({"usd_reversal", "row_liquidity_returns", "jpy_squeeze"} <= ids)
        with patch("thesis_config._load", return_value=cfg):
            hit = {t["id"] for t in check_invalidation(
                {"dxy_20d_pct": -2.5, "eem_vs_spy_20d_pp": 3.5, "usdjpy_pullback_from_60d_high_pct": -6})}
            self.assertTrue({"usd_reversal", "row_liquidity_returns", "jpy_squeeze"} <= hit)
            self.assertEqual(check_invalidation({"dxy_20d_pct": 3.0, "eem_vs_spy_20d_pp": -3.0,
                                                 "usdjpy_pullback_from_60d_high_pct": -1.0}), [])

    def test_brief_fresh_and_stale(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "usd_theme.json"
            d = {"generated_at": datetime.now(timezone.utc).isoformat(), "metrics": {"dxy": 102.2, "dxy_20d_pct": 3.2},
                 "strength": "兑现中", "invalidation_triggered": [{"id": "jpy_squeeze"}]}
            p.write_text(json.dumps(d), encoding="utf-8")
            with patch.object(ut, "OUT", p):
                b = ut.brief()
                self.assertIn("强美元", b); self.assertIn("102.2", b); self.assertIn("jpy_squeeze", b)
                d["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
                p.write_text(json.dumps(d), encoding="utf-8")
                self.assertIn("已过期", ut.brief())
            with patch.object(ut, "OUT", Path(td) / "missing.json"):
                self.assertIn("强美元", ut.brief())       # 没有读数也给出主线本身

    def test_gate_prompt_context_only(self):
        import claude_gate
        with patch("usd_theme.brief", return_value="THEME-X"):
            p = claude_gate._prompt("US.MSFT", {}, {}, {"action": "WATCH_BUY"}, {}, "midday")
        self.assertIn("THEME-X", p)
        self.assertIn("do NOT veto solely", p)

    def test_wiring(self):
        import snapshot_generator as sg
        import _webui_watchdog as wd
        self.assertIn("/api/usd_theme", sg.GLOBAL_ENDPOINTS)
        self.assertEqual(wd.JOBS["usd_theme"].get("auto_every_days"), 1)
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertLess(html.index('id="usd-theme"'), html.index('id="positions"'))
        promises = html[html.index("async function loadAll()"):html.index("// Owner-only endpoints")]
        self.assertIn("loadUsdTheme()", promises)
        src = (AGENTS_DIR / "ai_prompt.py").read_text(encoding="utf-8")
        self.assertIn("from usd_theme import brief", src)


if __name__ == "__main__":
    unittest.main()
