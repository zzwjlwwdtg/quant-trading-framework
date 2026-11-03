"""2026-11-03: 10/29 NBIS 复盘 —— 旧买单晚到的成交确认不能清掉之后才记的止损线;
买入价已低于本单止损参考时不得买入."""
import json
import unittest
from pathlib import Path

import paper_trader as pt
import stop_line_guard as g


def _line(ts="2026-10-29T08:48:57+00:00", line=172.16):
    Path(g.LINES).write_text(json.dumps({"US.NBIS": {"line": line, "kind": "software_stop",
                                                   "exit_price": 169.16, "ts": ts}}), encoding="utf-8")


class FillOrdering(unittest.TestCase):
    def test_old_buy_confirmed_late_keeps_newer_line(self):
        _line()
        ok, why = g.confirm_buy_fill("US.NBIS", 181.56, "479102", fill_time="2026-10-28T19:48:37+00:00")
        self.assertTrue(ok)
        self.assertIn("predates", why)
        self.assertIn("US.NBIS", json.loads(Path(g.LINES).read_text(encoding="utf-8")))

    def test_new_buy_above_line_clears(self):
        _line()
        ok, why = g.confirm_buy_fill("US.NBIS", 181.0, "x", fill_time="2026-10-30T14:00:00+00:00")
        self.assertTrue(ok)
        self.assertNotIn("US.NBIS", json.loads(Path(g.LINES).read_text(encoding="utf-8")))

    def test_missing_fill_time_keeps_old_behaviour(self):
        _line()
        ok, _ = g.confirm_buy_fill("US.NBIS", 181.0, "x")
        self.assertTrue(ok)
        self.assertNotIn("US.NBIS", json.loads(Path(g.LINES).read_text(encoding="utf-8")))


class EntryBelowOwnStop(unittest.TestCase):
    def test_place_refuses_buy_below_own_stop_ref(self):
        src = Path(pt.__file__).read_text(encoding="utf-8")
        self.assertIn('if _entry_stop and ref_price and ref_price <= _entry_stop:', src)
        self.assertIn('"entry_stop_ref": stop if side == TrdSide.BUY else None', src)
        i_skip = src.index("if _entry_stop and ref_price and ref_price <= _entry_stop:")
        i_order = src.index("order_price = round(ref_price * (1 + buffer), 2)")
        self.assertLess(i_skip, i_order + 2000)   # 在下单前检查


if __name__ == "__main__":
    raise SystemExit("use: python -m unittest discover -s tests -t .")
