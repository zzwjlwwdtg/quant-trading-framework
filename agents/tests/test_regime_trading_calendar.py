"""交易所节假日 (Codex MODEL_AUDIT 2026-09-19 §2 / 探针 holiday_session_date).

_market_date 只处理周末: 2026-07-03 (独立日 7/4 周六 → 周五补休, NYSE 休市) 仍被当作交易日,
该日 regime 状态被判过期. 节假日应与周末同样处理: 顺延到下一交易日.
规则 (无网络依赖): 元旦/MLK/总统日/耶稣受难日/阵亡将士/六月节/独立日/劳动节/感恩节/圣诞;
周六→周五补休, 周日→周一补休; 元旦逢周六不在前一年 12/31 补休.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import regime_today as rt

NYSE_2026 = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
             "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]
NYSE_2027 = ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
             "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"]


class HolidayRules(unittest.TestCase):
    def test_2026_and_2027_holidays(self):
        for y, expected in ((2026, NYSE_2026), (2027, NYSE_2027)):
            got = sorted(d.isoformat() for d in rt.nyse_holidays(y))
            self.assertEqual(got, expected, y)

    def test_new_year_on_saturday_not_observed_on_dec31(self):
        # 2022-01-01 周六: NYSE 2021-12-31 照常开市
        self.assertNotIn(date(2021, 12, 31), rt.nyse_holidays(2021))
        self.assertNotIn(date(2021, 12, 31), rt.nyse_holidays(2022))

    def test_is_session(self):
        self.assertFalse(rt.is_nyse_session(date(2026, 7, 3)))
        self.assertFalse(rt.is_nyse_session(date(2026, 7, 4)))
        self.assertTrue(rt.is_nyse_session(date(2026, 7, 2)))
        self.assertTrue(rt.is_nyse_session(date(2026, 11, 27)))   # 感恩节次日半天, 仍开市


class EarlyCloseRules(unittest.TestCase):
    def test_early_close_dates(self):
        expected = {
            2024: {"2024-07-03", "2024-11-29", "2024-12-24"},
            2025: {"2025-07-03", "2025-11-28", "2025-12-24"},
            # 2026-07-03 是独立日补休全天休市; 2027 圣诞夜是补休全天休市
            2026: {"2026-11-27", "2026-12-24"},
            2027: {"2027-11-26"},
        }
        for year, want in expected.items():
            got = {d.isoformat() for d in rt.nyse_early_closes(year)}
            self.assertEqual(got, want, year)

    def test_close_minute(self):
        self.assertEqual(rt.nyse_close_minute(date(2026, 11, 27)), 13 * 60)
        self.assertEqual(rt.nyse_close_minute(date(2026, 12, 24)), 13 * 60)
        self.assertEqual(rt.nyse_close_minute(date(2026, 11, 25)), 16 * 60)

    def test_holiday_is_not_early_close(self):
        self.assertFalse(rt.is_nyse_early_close(date(2026, 7, 3)))
        self.assertFalse(rt.is_nyse_early_close(date(2026, 11, 26)))


class MarketDateSkipsHolidays(unittest.TestCase):
    def test_codex_probe_holiday_session_date(self):
        # Codex 探针: 2026-07-03 14:00 UTC → 应跳到下一交易日 2026-07-06
        self.assertEqual(rt._market_date(datetime(2026, 7, 3, 14, tzinfo=timezone.utc)), "2026-07-06")

    def test_evening_before_holiday_rolls_past_it(self):
        # 2026-07-02 21:00 ET (after 20:00) → 下一交易日跳过 7/3 周五假日与周末
        self.assertEqual(rt._market_date(datetime(2026, 7, 3, 1, tzinfo=timezone.utc)), "2026-07-06")

    def test_normal_weekday_unchanged(self):
        self.assertEqual(rt._market_date(datetime(2026, 9, 30, 14, tzinfo=timezone.utc)), "2026-09-30")

    def test_weekend_still_rolls_to_monday(self):
        self.assertEqual(rt._market_date(datetime(2026, 10, 3, 14, tzinfo=timezone.utc)), "2026-10-05")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
