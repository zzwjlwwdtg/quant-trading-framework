from datetime import date, datetime
from unittest import TestCase
from unittest.mock import patch

from cn_brief import brief, schedule, sectors, tick


class SectorBriefTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 12, 35, tzinfo=schedule.JST)
        self.days = [date(2026, 11, day) for day in (2, 3, 4, 5)]

    def test_streak_compounds_and_stops_on_first_opposite_close(self):
        closes = dict(zip(self.days, [95, 90, 100, 110], strict=True))
        quote = {"last": 121, "previous": 110, "change_pct": 10}
        result = sectors.streak(quote, closes, self.days)
        self.assertEqual(result["days"], 3)
        self.assertAlmostEqual(result["cumulative_pct"], (121 / 90 - 1) * 100)
        self.assertFalse(result["partial"])

    def test_missing_day_does_not_join_two_runs(self):
        closes = {self.days[0]: 110, self.days[1]: 105, self.days[3]: 100}
        quote = {"last": 95, "previous": 100, "change_pct": -5}
        result = sectors.streak(quote, closes, self.days)
        self.assertEqual(result["days"], 1)
        self.assertTrue(result["partial"])
        self.assertIsNone(sectors.streak({**quote, "previous": 99}, closes, self.days))

    def test_midday_only_accepts_same_day_quotes_after_morning_close(self):
        epoch = datetime(2026, 11, 6, 12, 30, tzinfo=schedule.JST).timestamp()
        raw = {"total": 3, "diff": [
            {"f12": "BK1001", "f14": "板块甲", "f2": 105, "f18": 100, "f3": 5, "f124": epoch},
            {"f12": "BK1002", "f14": "板块乙", "f2": 100, "f18": 100, "f3": 0, "f124": epoch},
            {"f12": "BK1003", "f14": "板块丙", "f2": 97, "f18": 100, "f3": -3, "f124": epoch},
        ]}
        with patch.object(sectors, "_get", return_value=raw):
            self.assertEqual(sectors._quotes(True, self.now.astimezone(sectors.BJT), "midday")[0], 3)
            raw["diff"][0]["f124"] -= 60
            with self.assertRaises(ValueError):
                sectors._quotes(True, self.now.astimezone(sectors.BJT), "midday")

    def test_unverified_midday_does_not_push_or_consume_slot(self):
        snapshot = {"status": "UNAVAILABLE", "reason": "quote_snapshot_unverified"}
        with patch.object(sectors, "build", return_value=snapshot), patch.object(brief, "save") as save, \
             patch.object(schedule, "load_state", return_value={}), patch.object(schedule, "save_state"), \
             patch.object(tick, "load_events", return_value=[]), patch.object(tick, "push") as push, \
             patch.object(schedule, "mark_slot_done") as mark:
            self.assertEqual(tick.run_tick(now=self.now)["slots"][0]["slot"], "midday")
        save.assert_not_called()
        push.assert_not_called()
        mark.assert_not_called()

    def test_real_slot_carries_source_asof_and_independent_day_key(self):
        data = {
            "status": "READY", "slot": "midday", "universe_size": 30, "source": sectors.SOURCE_URL,
            "asof": "2026-11-06T12:30:00+09:00", "latest_asof": "2026-11-06T12:30:00+09:00",
            "up": [{"name": "机械设备", "change_pct": 3, "streak": None}],
            "down": [{"name": "传媒", "change_pct": -2,
                      "streak": {"days": 2, "cumulative_pct": -4.5, "partial": False}}],
        }
        with patch.object(sectors, "build", return_value=data):
            result = brief.build_brief("midday", now=self.now, write_files=False)
        self.assertIn("### 领涨板块", result["markdown"])
        self.assertIn("机械设备  当日 +3.00%｜连涨跌与累计待核实", result["markdown"])
        self.assertIn("传媒  当日 -2.00%｜连跌 2 日｜区间累计 -4.50%", result["markdown"])
        self.assertEqual(schedule.due_slots(self.now, {}), ["midday"])
        self.assertEqual(schedule.due_slots(self.now, {"slots_done": {"midday": "2026-11-06"}}), [])
