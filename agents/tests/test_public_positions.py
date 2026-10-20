"""公开页显示持仓并置顶 (2026-10-06 用户决定: GitHub Pages 完整显示持仓)."""
import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import snapshot_generator as sg


class PublicPositions(unittest.TestCase):
    def test_snapshot_includes_positions(self):
        self.assertIn("/api/positions", sg.GLOBAL_ENDPOINTS)
        self.assertNotIn("/api/positions", sg.PRIVATE_ENDPOINTS)
        for ep in ("/api/nav", "/api/trades", "/api/log"):      # 其余私有项不变
            self.assertIn(ep, sg.PRIVATE_ENDPOINTS)

    def test_dashboard_positions_pinned_and_public(self):
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertLess(html.index('id="positions"'), html.index('id="top-picks"'))
        static_private = html[html.index("const PRIVATE = ["):].split("\n", 1)[0]
        self.assertNotIn("/api/positions", static_private)
        card = html[:html.index('id="positions"')].rsplit('<div class="grid', 1)[1]
        self.assertNotIn("owner-only", card)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
