"""QQQ/SPY 大额看跌监控 (2026-10-07): 只展示 + 存档, 不进交易决策."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import option_flow as of


def ev(**kw):
    base = {"id": "QQQ|2026-11-20|put|700", "status": "oi_confirmation_pending", "event_date": "2026-10-07",
            "source": "QQQ", "option_type": "put", "dte": 44, "moneyness": 0.93,
            "estimated_premium": 2_500_000.0, "score": 55, "complex_suspected": False}
    base.update(kw)
    return base


class IndexWatch(unittest.TestCase):
    def test_filters(self):
        events = [
            ev(),                                                        # 入选
            ev(id="a", dte=0),                                           # 0DTE 剔除
            ev(id="b", moneyness=1.01),                                  # 实值 put 剔除
            ev(id="c", estimated_premium=200_000.0),                     # 金额不够
            ev(id="d", complex_suspected=True),                          # 疑似多腿
            ev(id="e", option_type="call", estimated_premium=5_000_000), # call 只进比值
            ev(id="f", source="SPY", status="oi_confirmed", estimated_premium=3_000_000.0),
            ev(id="g", source="SMH"),                                    # 非指数源
        ]
        w = of.build_index_watch(events)
        q, s = w["sources"]["QQQ"], w["sources"]["SPY"]
        self.assertEqual([r["id"] for r in q["top"]], ["QQQ|2026-11-20|put|700"])
        self.assertEqual(q["n_large_otm_puts"], 1)
        # put 权利金 (含被剔除的 put) / call 权利金, 只算 DTE≥7
        self.assertAlmostEqual(q["put_call_premium_ratio_dte7plus"], (2.5e6 + 2.5e6 + 0.2e6 + 2.5e6) / 5e6, places=3)
        self.assertEqual((s["n_large_otm_puts"], s["n_oi_confirmed"]), (1, 1))
        self.assertIn("无法区分买方/卖方", w["caveat"])

    def test_spy_not_mapped_to_positions(self):
        self.assertIn("SPY", of.MONITORED_SOURCES)
        for proxies in of.POSITION_PROXY_MAP.values():
            self.assertNotIn("SPY", [p["source"] for p in proxies])
        pos = of._aggregate_positions([ev(source="SPY", direction="bearish", score=69, score_cap=69)])
        self.assertTrue(all(v.get("score", 0) == 0 or "SPY" not in (v.get("sources") or []) for v in pos.values()))

    def test_log_dedup(self):
        w = of.build_index_watch([ev(), ev(source="SPY", id="S1")])
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "log.jsonl"
            self.assertEqual(of.append_index_watch_log(w, p), 2)
            self.assertEqual(of.append_index_watch_log(w, p), 0)           # 同一状态不重复
            w2 = of.build_index_watch([ev(status="oi_confirmed")])
            self.assertEqual(of.append_index_watch_log(w2, p), 1)          # 次日确认是新记录
            self.assertEqual(len(p.read_text(encoding="utf-8").splitlines()), 3)

    def test_analyze_outputs_index_watch(self):
        from datetime import datetime, timezone
        res, _ = of.analyze_option_payloads({}, {}, now=datetime(2026, 10, 7, 15, tzinfo=timezone.utc))
        self.assertIn("index_watch", res)
        self.assertEqual(set(res["index_watch"]["sources"]), {"QQQ", "SPY"})


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
