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

    def test_review_one_attaches_news_structured(self):
        """EVO-20261014-2: live_fetch=True 时 ±48h 候选新闻结构化拆解结果挂到 news_structured;
        live_fetch=False 时不调 CLI, 只落占位. 决策链路不读这个字段 (postmortem-only)."""
        sell = {"order_id": "99", "ticker": "US.MULL", "qty": 1, "price": 1,
                "ts": "2026-10-07T11:00:21+00:00",
                "decision_ts": "2026-10-07T11:00:21+00:00",
                "tag": "[SOFTWARE-STOP]", "decision": {}}
        def ai(_):
            return (json.dumps(GOOD, ensure_ascii=False), "ok", "Claude", "")
        fake_ns = {"items": [{"event_type": "BREAKING", "direction": "bearish",
                              "confidence": 7, "verbatim_evidence": "fraud report",
                              "pub_utc": "2026-10-06 14:00", "symbol": "MULL",
                              "title": "MULL plunges on fraud report"}],
                   "fetched": 3, "fallback": False, "window_hours": 48}
        with patch.object(tp, "LOGS", self.td), \
             patch.object(tp, "news_from_cache", return_value=[]), \
             patch.object(tp, "news_from_rss", return_value=[]), \
             patch.object(tp, "closes_after", return_value=[]), \
             patch.object(tp, "collect_news_structured_for", return_value=fake_ns) as mock_cns:
            r = tp.review_one(sell, ai=ai, live_fetch=True)
        mock_cns.assert_called_once()
        self.assertTrue(r["ok"])
        self.assertEqual(r["news_structured"]["fetched"], 3)
        self.assertEqual(r["news_structured"]["items"][0]["event_type"], "BREAKING")
        # live_fetch=False → 不调 CLI, 只写占位
        with patch.object(tp, "LOGS", self.td), \
             patch.object(tp, "news_from_cache", return_value=[]), \
             patch.object(tp, "collect_news_structured_for") as mock_skip:
            r2 = tp.review_one(sell, ai=ai, live_fetch=False)
        mock_skip.assert_not_called()
        self.assertEqual(r2["news_structured"]["items"], [])
        self.assertEqual(r2["news_structured"].get("skipped"), "live_fetch=False")

    def test_collect_news_structured_for_isolates_errors(self):
        """news_analyzer 挂了也不能把 review_one 拖崩 — 返回 fallback 字典, 决策链路不受影响."""
        sell = {"order_id": "77", "ticker": "US.MULL"}
        t = datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc)
        with patch("news_analyzer.collect_news_structured",
                   side_effect=RuntimeError("CLI timeout")):
            ns = tp.collect_news_structured_for(sell, t)
        self.assertTrue(ns["fallback"])
        self.assertEqual(ns["items"], [])
        self.assertIn("CLI timeout", ns["error"])

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
        bat = (AGENTS_DIR / "_sell_review_ai.bat").read_text(encoding="utf-8")
        self.assertIn('AI_CLI_PRIMARY=claude', bat)


class TestEntryChain(unittest.TestCase):
    """EVO-20261123-1: 只读入场链条 cross-link 测试."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        # probe 规则建仓的 BUY + SELL 对
        buy_probe = {
            "ts": "2026-10-05T12:00:00+00:00", "ticker": "US.INTC", "side": "BUY",
            "qty": 200, "price": 25.0, "order_id": "101", "dry_run": False,
            "tag": "[WATCH_BUY_PROBE conf=4 win=pre-market sat]",
            "decision": {
                "action": "WATCH_BUY_PROBE", "confidence": 4,
                "reason": "sim_active_probe_after_claude_veto: weak signal",
                "regime": "neutral",
                "score_breakdown": {
                    "tech": 3, "tech_weighted": 1.2,
                    "macro": 0, "event": 1, "quant": 0, "boost": 1, "raw": 4,
                },
            },
            "market": {"rsi": 45.0, "cci": -20.0, "trend": "up",
                       "vol_ratio": 0.5, "ma_stack": "bull",
                       "pct_chg": -1.5, "cum_5d": -5.0},
        }
        sell_probe = {
            "ts": "2026-10-10T14:00:00+00:00", "ticker": "US.INTC", "side": "SELL",
            "qty": 200, "price": 22.0, "order_id": "205", "dry_run": False,
            "tag": "[TRAILING-STOP from high $26.00 → $22.10 (-15%)]",
            "decision": {"action": None, "confidence": None, "reason": "", "regime": None},
        }
        # 普通规则建仓的 BUY + SELL 对
        buy_normal = {
            "ts": "2026-10-06T12:00:00+00:00", "ticker": "US.TQQQ", "side": "BUY",
            "qty": 100, "price": 50.0, "order_id": "301", "dry_run": False,
            "tag": "[WATCH_BUY conf=5 win=pre-market]",
            "decision": {
                "action": "WATCH_BUY", "confidence": 5, "reason": "uptrend + oversold",
                "regime": "bull_trending",
                "score_breakdown": {
                    "tech": 5, "tech_weighted": 2.5,
                    "macro": 0, "event": 0, "quant": 0, "boost": 0, "raw": 5,
                },
            },
            "market": {"rsi": 30.0, "cci": -120.0, "trend": "up",
                       "vol_ratio": 1.5, "ma_stack": "bull",
                       "pct_chg": 1.0, "cum_5d": -8.0},
        }
        sell_normal = {
            "ts": "2026-10-09T12:00:00+00:00", "ticker": "US.TQQQ", "side": "SELL",
            "qty": 100, "price": 45.0, "order_id": "402", "dry_run": False,
            "tag": "[TRAILING-STOP ...]",
            "decision": {"action": None, "reason": "", "regime": None, "confidence": None},
        }
        (self.td / "trade_log.jsonl").write_text(
            "\n".join(json.dumps(e) for e in [
                buy_probe, sell_probe, buy_normal, sell_normal,
            ]) + "\n",
            encoding="utf-8",
        )

    # ── _find_entry_buy_event ────────────────────────────────────────────────

    def test_find_entry_buy_event_fifo_match(self):
        """probe BUY 101 → SELL 205 の FIFO 配对."""
        with patch.object(tp, "SIGNALS", self.td):
            result = tp._find_entry_buy_event("205", "US.INTC")
        self.assertIsNotNone(result)
        self.assertEqual(str(result["order_id"]), "101")
        self.assertEqual(result["decision"]["action"], "WATCH_BUY_PROBE")

    def test_find_entry_buy_event_no_match(self):
        """找不到时返回 None."""
        with patch.object(tp, "SIGNALS", self.td):
            result = tp._find_entry_buy_event("999", "US.INTC")
        self.assertIsNone(result)

    def test_find_entry_buy_event_fifo_consumes_prior_sells(self):
        """TQQQ BUY 301 → SELL 402 (INTC BUY/SELL 消耗后 TQQQ 仍可配对)."""
        with patch.object(tp, "SIGNALS", self.td):
            result = tp._find_entry_buy_event("402", "US.TQQQ")
        self.assertIsNotNone(result)
        self.assertEqual(str(result["order_id"]), "301")
        self.assertEqual(result["decision"]["action"], "WATCH_BUY")

    # ── load_entry_chain ─────────────────────────────────────────────────────

    def test_load_entry_chain_probe_rule(self):
        """probe 规则建仓: 必须有 trigger_rule + probe_tracker_at_postmortem."""
        sell = {"order_id": "205", "ticker": "US.INTC",
                "qty": 200, "price": 22.0,
                "decision_ts": "2026-10-10T14:00:00+00:00",
                "tag": "[TRAILING-STOP ...]", "decision": {}}
        with patch.object(tp, "SIGNALS", self.td):
            chain = tp.load_entry_chain(sell)
        self.assertTrue(chain["read_only"])
        self.assertEqual(chain["proposal"], "EVO-20261123-1")
        self.assertEqual(str(chain["entry_order_id"]), "101")
        self.assertEqual(chain["ai_gate_decision"], "WATCH_BUY_PROBE")
        self.assertEqual(chain["trigger_rule"], "sim_active_probe_after_claude_veto")
        self.assertEqual(chain["confidence"], 4)
        self.assertEqual(chain["regime"], "neutral")
        self.assertIsInstance(chain["confluence"], dict)
        self.assertEqual(chain["confluence"]["raw_total"], 4)
        self.assertIsInstance(chain["market_at_entry"], dict)
        self.assertEqual(chain["market_at_entry"]["rsi"], 45.0)
        # probe tracker ブロックが必ず存在
        self.assertIn("probe_tracker_at_postmortem", chain)
        pt = chain["probe_tracker_at_postmortem"]
        self.assertTrue(pt["read_only"])
        self.assertEqual(pt["proposal"], "EVO-20261122-1")

    def test_load_entry_chain_normal_rule(self):
        """正常建仓: trigger_rule=normal, probe_tracker なし."""
        sell = {"order_id": "402", "ticker": "US.TQQQ",
                "qty": 100, "price": 45.0,
                "decision_ts": "2026-10-09T12:00:00+00:00",
                "tag": "[TRAILING-STOP ...]", "decision": {}}
        with patch.object(tp, "SIGNALS", self.td):
            chain = tp.load_entry_chain(sell)
        self.assertEqual(chain["trigger_rule"], "normal")
        self.assertEqual(chain["ai_gate_decision"], "WATCH_BUY")
        self.assertNotIn("probe_tracker_at_postmortem", chain)

    def test_load_entry_chain_missing_pair(self):
        """FIFO 配对失败时全字段显式 missing."""
        sell = {"order_id": "999", "ticker": "US.INTC",
                "qty": 100, "price": 20.0,
                "decision_ts": "2026-10-15T14:00:00+00:00",
                "tag": "[TRAILING-STOP]", "decision": {}}
        with patch.object(tp, "SIGNALS", self.td):
            chain = tp.load_entry_chain(sell)
        self.assertTrue(chain["read_only"])
        self.assertEqual(chain["entry_order_id"], "missing")
        self.assertEqual(chain["ai_gate_decision"], "missing")
        self.assertEqual(chain["trigger_rule"], "missing")
        self.assertIn("missing_reason", chain)

    def test_load_entry_chain_snapshot_fallback(self):
        """entry_snapshots.jsonl がない場合は snapshot_source=trade_log."""
        sell = {"order_id": "205", "ticker": "US.INTC",
                "qty": 200, "price": 22.0,
                "decision_ts": "2026-10-10T14:00:00+00:00",
                "tag": "[TRAILING-STOP ...]", "decision": {}}
        with patch.object(tp, "SIGNALS", self.td):
            chain = tp.load_entry_chain(sell)
        # 本 worktree では EVO-20261119-1 は未合并なので trade_log
        self.assertEqual(chain["snapshot_source"], "trade_log")

    def test_load_entry_chain_snapshot_preferred(self):
        """entry_snapshots.jsonl にデータがある時は snapshot_source=entry_snapshot_file."""
        snapshot = {
            "order_id": "101",
            "decision": {
                "action": "WATCH_BUY_PROBE", "confidence": 4,
                "reason": "snapshot_reason",
                "regime": "bull",
                "score_breakdown": {"tech": 3, "macro": 0, "event": 1,
                                    "quant": 0, "boost": 1, "raw": 4,
                                    "tech_weighted": 1.2},
            },
            "market": {"rsi": 50.0, "cci": 0.0, "trend": "up",
                       "vol_ratio": 1.0, "ma_stack": "bull",
                       "pct_chg": 0.5, "cum_5d": 2.0},
        }
        (self.td / "entry_snapshots.jsonl").write_text(
            json.dumps(snapshot) + "\n", encoding="utf-8")
        sell = {"order_id": "205", "ticker": "US.INTC",
                "qty": 200, "price": 22.0,
                "decision_ts": "2026-10-10T14:00:00+00:00",
                "tag": "[TRAILING-STOP ...]", "decision": {}}
        with patch.object(tp, "SIGNALS", self.td):
            chain = tp.load_entry_chain(sell)
        self.assertEqual(chain["snapshot_source"], "entry_snapshot_file")
        # 快照里的 regime 覆盖 trade_log 的 neutral
        self.assertEqual(chain["regime"], "bull")

    # ── review_one 包含 entry_chain ─────────────────────────────────────────

    def test_review_one_includes_entry_chain(self):
        """review_one 输出里必须包含 entry_chain 块 (read_only=True)."""
        sell = {"order_id": "205", "ticker": "US.INTC",
                "qty": 200, "price": 22.0,
                "ts": "2026-10-10T14:00:00+00:00",
                "decision_ts": "2026-10-10T14:00:00+00:00",
                "tag": "[TRAILING-STOP ...]", "decision": {}}
        def ai(_):
            return (json.dumps(GOOD, ensure_ascii=False), "ok", "Claude", "")
        with patch.object(tp, "LOGS", self.td), \
             patch.object(tp, "SIGNALS", self.td), \
             patch.object(tp, "news_from_cache", return_value=[]), \
             patch.object(tp, "closes_after", return_value=[]), \
             patch.object(tp, "collect_news_structured_for",
                          return_value={"items": [], "fetched": 0, "fallback": None}):
            r = tp.review_one(sell, ai=ai, live_fetch=False)
        self.assertIn("entry_chain", r)
        chain = r["entry_chain"]
        self.assertTrue(chain["read_only"])
        self.assertEqual(chain["proposal"], "EVO-20261123-1")
        # probe 建仓 → entry_order_id 101, trigger_rule = probe
        self.assertEqual(str(chain["entry_order_id"]), "101")
        self.assertEqual(chain["trigger_rule"], "sim_active_probe_after_claude_veto")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
