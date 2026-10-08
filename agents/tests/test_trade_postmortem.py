"""卖出复盘 (2026-10-08 用户要求): 5 条理由 + 3 条局限性, 每笔只复盘一次, 补现金卖单不复盘."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import trade_postmortem as tp

LOG = """2026-10-07 19:55:00 [宏观] VIX:15.0  F&G:47
2026-10-07 19:56:00 【MULL】价格:23.40  RSI:40.0  量比:1.2  趋势:down
2026-10-07 19:56:00   信号: ⬜ 持仓观望
2026-10-07 19:56:00     [空] [2026-10-07 今日盘前(进行中)] 跌-5.67%  → 开盘大概率跳空低开

2026-10-07 19:57:00 【DRAM】价格:58.0  RSI:50
2026-10-07 19:57:00   信号: 无关
2026-10-07 20:00:21 [trader-LIVE] SELL   346 US.MULL  @    23.07 [SOFTWARE-STOP]
2026-10-07 20:00:22 [宏观] VIX:15.0  F&G:47
2026-10-08 01:03:54 【MULL】价格:25.95  RSI:72.8
"""

GOOD = {"context": {"technical": "t", "news": "n"},
        "reasons": [{"type": "技术面", "reason": f"r{i}", "evidence": "e"} for i in range(4)]
                   + [{"type": "消息面", "reason": "r4", "evidence": "e"}],
        "limitations": [{"limitation": f"l{i}", "why": "w"} for i in range(3)],
        "hindsight": {"summary": "反弹", "verdict": "不合理"}}


class Postmortem(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        (self.td / "run_20261007.log").write_text(LOG, encoding="utf-8")

    def test_log_time_is_jst(self):
        rows = tp.parse_log(self.td / "run_20261007.log")
        self.assertEqual(rows[0][0], datetime(2026, 10, 7, 10, 55, tzinfo=timezone.utc))
        self.assertIsNone(rows[4][0])                      # 空行 = 续行

    def test_excerpt_block_and_dedupe(self):
        t = datetime(2026, 10, 7, 11, 0, 21, tzinfo=timezone.utc)
        rows = tp.load_log_window(t, 8, 1, logs_dir=self.td)
        ex = tp.select_excerpt(rows, "MULL")
        joined = "\n".join(ex)
        self.assertIn("跳空低开", joined)                  # 信号块续行被保留
        self.assertIn("SELL   346 US.MULL", joined)
        self.assertNotIn("DRAM", joined)                  # 其它标的块不进来
        ex_d = "\n".join(tp.select_excerpt(rows, "DRAM"))
        self.assertNotIn("跳空低开", ex_d)                 # MULL 块里的"盘前"行不会混进 DRAM
        self.assertEqual(sum("[宏观]" in l for l in ex), 1)  # 同句不同时间只留一次
        self.assertNotIn("25.95", joined)                 # 窗口外 (事后) 不混入当时材料
        after = tp.load_log_window(t + tp.timedelta(hours=1), 0, 36, logs_dir=self.td)
        self.assertTrue(any("25.95" in l for l in tp.price_path_after(after, "MULL")))

    def test_label_closes_and_news_symbols(self):
        rows = [{"date": d, "close": c} for d, c in (("2026-10-05", 1), ("2026-10-06", 2), ("2026-10-07", 3), ("2026-10-08", 4))]
        self.assertEqual([r["rel"] for r in tp.label_closes(rows, "2026-10-07")], ["D-2", "D-1", "D0", "D+1"])
        syms = tp.news_symbols("US.MULL")
        self.assertEqual(syms[:2], ["MULL", "MU"]); self.assertIn("QQQ", syms)

    def test_parse_ai_validation(self):
        self.assertEqual(tp.parse_ai("前言 " + json.dumps(GOOD, ensure_ascii=False) + " 结尾")["hindsight"]["verdict"], "不合理")
        bad = dict(GOOD, reasons=GOOD["reasons"][:4])
        self.assertIsNone(tp.parse_ai(json.dumps(bad)))
        bad2 = dict(GOOD, limitations=GOOD["limitations"][:2])
        self.assertIsNone(tp.parse_ai(json.dumps(bad2)))
        bad3 = dict(GOOD, reasons=[dict(r, type="其他") for r in GOOD["reasons"]])
        self.assertIsNone(tp.parse_ai(json.dumps(bad3)))
        self.assertIsNone(tp.parse_ai(None))

    def test_classify(self):
        self.assertEqual(tp.classify_sell("[SOFTWARE-STOP trigger $23.29]"), "强制调仓·机械止损")
        self.assertEqual(tp.classify_sell("[TRAILING-STOP from high]"), "强制调仓·机械止损")
        self.assertEqual(tp.classify_sell("[REBALANCE CASH restore cash>=0]"), "强制调仓·现金纪律")
        self.assertEqual(tp.classify_sell("[REBALANCE drift]"), "强制调仓·再平衡")
        self.assertEqual(tp.classify_sell("[TAKE-PROFIT tp15]"), "规则止盈")
        self.assertEqual(tp.classify_sell("[SELL conf=4 win=midday]"), "信号卖出")

    def test_prompt_separates_hindsight(self):
        p = tp.build_prompt({"ticker": "MULL", "qty": 1, "price": 1, "decision_ts": "x", "tag": "T"},
                            ["a"], [], ["later 25.95"], [])
        self.assertIn("正好 5 条", p); self.assertIn("正好 3 条局限性", p)
        self.assertIn("不得当作当时的卖出理由", p)
        self.assertIn("不要建议把这只标的拉黑", p)
        p2 = tp.build_prompt({"ticker": "MULL", "qty": 1, "price": 1, "decision_ts": "x", "tag": "[SOFTWARE-STOP]"}, [], [], [], [])
        self.assertIn("强制调仓·机械止损", p2)

    def test_run_skips_cash_and_reviewed_and_retries_failures(self):
        sells = [
            {"order_id": "1", "ticker": "MULL", "qty": 346.0, "price": 23.14, "ts": "2026-10-07T12:32:42+00:00",
             "decision_ts": "2026-10-07T11:00:21+00:00", "tag": "[SOFTWARE-STOP]", "decision": {}, "mechanical_cash": False, "side": "SELL"},
            {"order_id": "2", "ticker": "SHY", "qty": 305.0, "price": 81.0, "ts": "2026-10-07T16:03:55+00:00",
             "decision_ts": "2026-10-07T16:03:55+00:00", "tag": "[REBALANCE CASH fund BUY]", "decision": {}, "mechanical_cash": True, "side": "SELL"},
            {"order_id": "3", "ticker": "DRAM", "qty": 282.0, "price": 58.83, "ts": "2026-10-07T12:32:42+00:00",
             "decision_ts": "2026-10-07T11:00:20+00:00", "tag": "[SOFTWARE-STOP]", "decision": {}, "mechanical_cash": False, "side": "SELL"},
        ]
        calls = []
        def ai(prompt):
            calls.append(prompt)
            return (json.dumps(GOOD, ensure_ascii=False), "ok", "Claude", "") if "标的 MULL" in prompt else (None, "error", "Claude", "")
        out = self.td / "pm"
        with patch.object(tp, "OUT_DIR", out), patch.object(tp, "REVIEWED", out / "reviewed.json"), \
             patch.object(tp, "LATEST", out / "latest.json"), patch.object(tp, "LOGS", self.td), \
             patch.object(tp, "ARCHIVE", out / "archive.jsonl"), \
             patch.object(tp, "collect_sells", return_value=sells), \
             patch.object(tp, "news_from_cache", return_value=[]):
            r1 = tp.run(ai=ai, live_fetch=False)
            self.assertEqual(len(calls), 2)                    # SHY 补现金不复盘
            self.assertEqual(r1["last_run"], {"new": 1, "failed": 1})
            self.assertEqual([x["ticker"] for x in r1["reviews"]], ["MULL"])
            self.assertEqual(r1["mechanical_cash_sells"][0]["ticker"], "SHY")
            r2 = tp.run(ai=ai, live_fetch=False)
            self.assertEqual(len(calls), 3)                    # MULL 不再复盘, DRAM 重试
            self.assertEqual(r2["last_run"], {"new": 0, "failed": 1})
            arch = [json.loads(l) for l in (out / "archive.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sorted(a["ticker"] for a in arch), ["MULL", "SHY"])   # 各一次, 失败的不归档
            self.assertEqual({a["ticker"]: a["nature"] for a in arch},
                             {"MULL": "强制调仓·机械止损", "SHY": "强制调仓·现金纪律"})
            self.assertIn("SHY", next(a["reason"] for a in arch if a["ticker"] == "SHY"))

    def test_wiring(self):
        import snapshot_generator as sg
        self.assertIn("/api/postmortem", sg.GLOBAL_ENDPOINTS)
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertTrue(html.index('id="fills"') < html.index('id="postmortem"') < html.index('id="equity-chart"'))
        promises = html[html.index("async function loadAll()"):html.index("// Owner-only endpoints")]
        self.assertIn("loadPostmortem()", promises)
        import _webui_watchdog as wd
        self.assertEqual(wd.JOBS["trade_postmortem"].get("auto_every_days"), 1)
        self.assertTrue(wd.JOBS["trade_postmortem"].get("market_quiet"))
        bat = (AGENTS_DIR / "_trade_postmortem.bat").read_text(encoding="utf-8")
        self.assertIn('AI_CLI_PRIMARY=claude', bat)


if __name__ == "__main__":
    unittest.main()
