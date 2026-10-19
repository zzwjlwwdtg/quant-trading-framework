"""cn_brief: A股研报 + 中美日重大经济数据 (2026-10-19). 全部离线, 数据源打桩."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cn_brief import brief as B, schedule as S, push as P, tick as T  # noqa: E402

JST = timezone(timedelta(hours=9))
BJT = timezone(timedelta(hours=8))


def rep(code, name, org, cur, last, title="t", ind="半导体"):
    return {"stockCode": code, "stockName": name, "orgSName": org, "emRatingName": cur, "lastEmRatingName": last,
            "title": title, "indvInduName": ind, "publishDate": "2026-10-19 00:00:00.000", "infoCode": "AP1"}


def ev(region, title, t_jst, star="3", pub="未公布", former="1.0", exp=None):
    e = {"region": region, "title": title, "star": star, "pubVal": pub, "formerVal": former,
         "date": t_jst.astimezone(BJT).date().isoformat(), "time": t_jst.astimezone(BJT).strftime("%H:%M"),
         "time_jst": t_jst, "time_bjt": t_jst.astimezone(BJT)}
    if exp is not None:
        e["indicateVal"] = exp
    return e


class Ratings(unittest.TestCase):
    def test_change(self):
        self.assertEqual(B.rating_change(rep("1", "a", "o", "买入", "增持")), "调高")
        self.assertEqual(B.rating_change(rep("1", "a", "o", "中性", "买入")), "调低")
        self.assertEqual(B.rating_change(rep("1", "a", "o", "买入", "")), "首次")
        self.assertEqual(B.rating_change(rep("1", "a", "o", "买入", "买入")), "维持")
        self.assertEqual(B.rating_change(rep("1", "a", "o", "", "")), "无评级")
        self.assertEqual(B.rating_rank("买入(Buy)"), 5)

    def test_summary(self):
        rows = [rep("1", "甲", "A证券", "买入", "增持"), rep("1", "甲", "B证券", "买入", "买入"),
                rep("2", "乙", "C证券", "增持", ""), rep("3", "丙", "D证券", "中性", "买入")]
        s = B.summarize_stock_reports(rows)
        self.assertEqual(s["n"], 4)
        self.assertEqual([h["name"] for h in s["hot"]], ["甲"])
        self.assertEqual([x["name"] for x in s["upgrades"]], ["甲"])
        self.assertEqual([x["name"] for x in s["first_coverage"]], ["乙"])
        self.assertEqual([x["name"] for x in s["downgrades"]], ["丙"])


class Events(unittest.TestCase):
    t = datetime(2026, 10, 21, 21, 30, tzinfo=JST)

    def test_importance(self):
        self.assertTrue(B.is_important(ev("美国", "美国9月CPI年率(%)", self.t, star="1")))     # 关键词
        self.assertTrue(B.is_important(ev("中国", "中国三季度GDP年率", self.t, star="2")))
        self.assertTrue(B.is_important(ev("日本", "日本某指标", self.t, star="3")))           # 星级
        self.assertFalse(B.is_important(ev("美国", "美国当周API原油库存变动", self.t, star="3")))  # 噪声
        self.assertFalse(B.is_important(ev("德国", "德国CPI", self.t, star="5")))            # 不在中美日
        self.assertFalse(B.is_important(ev("中国", "中国9月柴油出口量-能源(万吨)", self.t, star="3")))
        self.assertFalse(B.is_important(ev("中国", "中国9月规模以上工业增加值月率-单月(%)", self.t, star="1")))
        self.assertTrue(B.is_important(ev("中国", "中国9月规模以上工业增加值年率-单月(%)", self.t, star="2")))

    def test_format(self):
        s = B.fmt_event(ev("美国", "美国9月CPI年率(%)", self.t, pub="3.1", exp="2.9", former="2.8"))
        self.assertIn("10/21 21:30 JST", s)
        self.assertIn("公布 **3.1**", s)
        self.assertIn("高于预期", s)
        self.assertNotIn("公布", B.fmt_event(ev("美国", "x CPI", self.t), with_result=False))


class Schedule(unittest.TestCase):
    def test_slots_once_per_day_with_grace(self):
        mon = datetime(2026, 10, 19, 9, 3, tzinfo=JST)
        self.assertEqual(S.due_slots(mon, {}), ["morning"])
        st = {}
        S.mark_slot_done(st, "morning", mon)
        self.assertEqual(S.due_slots(mon + timedelta(minutes=5), st), [])
        self.assertEqual(S.due_slots(mon.replace(hour=9, minute=50), {}), [])        # 超过 40 分钟不补
        self.assertEqual(S.due_slots(mon.replace(hour=16, minute=31), {}), ["close"])
        self.assertEqual(S.due_slots(mon.replace(hour=21, minute=0), {}), ["evening"])
        self.assertEqual(S.due_slots(datetime(2026, 10, 18, 9, 1, tzinfo=JST), {}), [])  # 周日

    def test_reminder_window_once(self):
        now = datetime(2026, 10, 21, 21, 0, tzinfo=JST)
        e = ev("美国", "美国9月CPI年率", now + timedelta(minutes=30)); e["important"] = True; e["id"] = "x"
        self.assertEqual(len(S.due_reminders([e], now, {})), 1)
        self.assertEqual(S.due_reminders([e], now, {"reminded": ["x"]}), [])
        self.assertEqual(S.due_reminders([e], now - timedelta(minutes=30), {}), [])     # 60 分钟前不提醒


class Chunking(unittest.TestCase):
    def test_chunk(self):
        text = "\n".join("行" * 50 for _ in range(100))
        parts = P.chunk(text, 1900)
        self.assertTrue(all(len(p) <= 1900 for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), text.replace("\n", ""))
        self.assertTrue(all(len(p) <= 100 for p in P.chunk("x" * 450, 100)))


class BuildAndTick(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [patch.object(B, "OUT_DIR", self.tmp), patch.object(S, "STATE_DIR", self.tmp),
                        patch.object(S, "STATE_PATH", self.tmp / "state.json"),
                        patch.object(T, "CACHE", self.tmp / "cache.json"), patch.object(T, "LOG", self.tmp / "log.jsonl")]
        for p in self.patches:
            p.start()
        self.now = datetime(2026, 10, 21, 21, 0, tzinfo=JST)
        self.cpi = ev("美国", "美国9月CPI年率(%)", self.now + timedelta(minutes=30), exp="2.9", former="2.8")

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _src(self, cal):
        return [patch.object(B.sources, "stock_reports", return_value=[rep("1", "甲", "A", "买入", "增持")]),
                patch.object(B.sources, "industry_reports", return_value=[]),
                patch.object(B.sources, "macro_reports", return_value=[]),
                patch.object(B.sources, "calendar_range", side_effect=lambda s, d: cal)]

    def test_build_writes_files_and_never_needs_ai(self):
        ps = self._src([self.cpi])
        for p in ps: p.start()
        try:
            b = B.build_brief("now", now=self.now, use_ai=False)
        finally:
            for p in ps: p.stop()
        self.assertIn("评级调高", b["markdown"])
        self.assertIn("美国9月CPI年率", b["markdown"])
        self.assertTrue((self.tmp / "latest_now.md").exists())
        json.loads((self.tmp / "latest_now.json").read_text(encoding="utf-8"))

    def test_calendar_failure_not_shown_as_no_data(self):
        with patch.object(B.sources, "calendar_range", side_effect=lambda s, d: [{"_error": "2026-10-21: 连接失败"}]):
            b = B.build_brief("evening", now=self.now, use_ai=False)
        self.assertIn("获取失败", b["markdown"])
        self.assertNotIn("没有中美日的重大数据", b["markdown"])
        self.assertTrue(b["errors"])

    def test_ai_unavailable_reported_honestly(self):
        import types
        mod = types.ModuleType("ai_prompt")
        mod.query_ai_cli = lambda *a, **k: (None, "claude: quota; codex=timeout", "Codex", "claude: quota")
        with patch.dict(sys.modules, {"ai_prompt": mod}):
            summ, status = B.ai_summary({"hot": [], "upgrades": [{"change": "调高", "name": "甲", "code": "1",
                                                                   "org": "A", "last_rating": "增持", "rating": "买入",
                                                                   "title": "t"}]}, {}, [])
        self.assertIsNone(summ)
        self.assertIn("均失败", status)

    def test_tick_reminds_once_then_reports_result(self):
        sent = []
        fake_push = lambda title, md, slot="", data=None: (sent.append((slot, title, md)) or {"sent": ["x"], "skipped": []})
        with patch.object(T, "push", side_effect=fake_push), \
             patch.object(B, "important_events", side_effect=lambda s, d: ([dict(self.cpi, important=True, id="cpi")], [])):
            T.run_tick(now=self.now)                          # evening 时段 + 提醒
            T.run_tick(now=self.now + timedelta(minutes=5))   # 不重复
        slots = [s for s, _, _ in sent]
        self.assertEqual(slots.count("reminder"), 1)
        self.assertEqual(slots.count("evening"), 1)
        # 公布后: 拿到公布值 → 补发一次
        released = dict(self.cpi, important=True, id="cpi", pubVal="3.1")
        with patch.object(T, "push", side_effect=fake_push), \
             patch.object(B, "important_events", side_effect=lambda s, d: ([released], [])):
            T.run_tick(now=self.now + timedelta(minutes=40))
            T.run_tick(now=self.now + timedelta(minutes=45))
        res = [m for s, _, m in sent if s == "result"]
        self.assertEqual(len(res), 1)
        self.assertIn("高于预期", res[0])


if __name__ == "__main__":
    unittest.main()
