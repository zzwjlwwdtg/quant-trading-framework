"""No hindsight registration or fake wins: deterministic prices, no real network/AI/trader."""
import copy
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_forecasts as f

NOW = datetime(2026, 10, 19, 14, tzinfo=timezone.utc)  # After Monday's US open.
NOTE = {"id": "a" * 24, "published_at": "2026-10-19T13:00:00+00:00", "url": "https://source.example.org/report", "evidence":"public quote", "title":"public report",
        "themes": ["SMH"], "hypothesis": {"proxy": "SMH", "stance": "OUTPERFORM",
        "thesis": "conditional demand improvement", "condition": "orders materialize", "invalidation": "orders weaken"}}
DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
PRICES = {"SPY": {day: {"open": 100., "close": 101.} for day in DAYS},
          "SMH": {day: {"open": 100., "close": 103.} for day in DAYS}}
END = datetime(2026, 10, 26, 21, tzinfo=timezone.utc)


class Forecasts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "research_forecasts.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, note=NOTE, now=NOW):
        return f.register(note, now=now, path=self.path)

    def test_registration_server_clock_immutable_dedupe_and_daily_cap(self):
        first = self.register()
        self.assertEqual(first["created_at"], NOW.isoformat())
        changed = copy.deepcopy(NOTE)
        changed["hypothesis"]["stance"] = "UNDERPERFORM"
        changed["hypothesis"]["thesis"] = "rewrite after loss"
        self.assertEqual(self.register(changed, now=NOW + timedelta(days=1)), first)
        self.assertEqual(first["horizon_sessions"], 5)
        self.assertEqual(first["broadcast_status"], "NOT_AUTOMATICALLY_SENT")
        self.assertIsNotNone(self.register({**NOTE, "id": "b" * 24}))
        self.assertIsNone(self.register({**NOTE, "id": "c" * 24}))
        with closing(sqlite3.connect(self.path)) as db:
            for sql in ("UPDATE forecasts SET created_at='1999'", "DELETE FROM forecasts"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_unfounded_unsupported_future_or_abstaining_forecasts_not_registered(self):
        for note in [{**NOTE, "themes": ["GLD"]}, {**NOTE, "published_at": "2026-10-20T14:00:00+00:00"},
                     {**NOTE, "hypothesis": {**NOTE["hypothesis"], "stance": "ABSTAIN"}},
                     {**NOTE, "hypothesis": {**NOTE["hypothesis"], "condition": ""}}]:
            self.assertIsNone(self.register(note))
        self.assertFalse(self.path.exists())

    def test_first_open_strictly_after_prediction_and_mature_exit_only(self):
        row = self.register()
        result = f.evaluate(row, PRICES, now=END)
        self.assertEqual(result["entry_session"], "2026-10-20")
        self.assertEqual(result["exit_session"], "2026-10-26")
        self.assertEqual(result["status"], "HIT")
        self.assertEqual(result["excess_pp"], 2.)
        self.assertEqual(result["condition_outcome"], "NOT_EVALUATED")
        self.assertIsNone(f.evaluate(row, PRICES, now=END.replace(hour=19)))
        before_open = self.register({**NOTE, "id": "d" * 24}, now=NOW.replace(hour=13))
        self.assertEqual(f.evaluate(before_open, PRICES, now=END)["entry_session"], "2026-10-19")

    def test_missing_sessions_nan_and_holiday_do_not_shift_or_fake_score(self):
        row = self.register()
        prices = copy.deepcopy(PRICES)
        del prices["SMH"]["2026-10-20"]
        self.assertIsNone(f.evaluate(row, prices, now=END))
        bad = copy.deepcopy(PRICES)
        bad["SMH"]["2026-10-20"]["open"] = float("nan")
        self.assertIsNone(f.evaluate(row, bad, now=END))
        holiday = copy.deepcopy(PRICES)
        for rows in holiday.values():
            rows.pop("2026-10-23")
        self.assertIsNone(f.evaluate(row, holiday, now=END))

    def test_losses_and_flat_are_kept_not_cherry_picked_and_outcomes_do_not_rewrite(self):
        first = self.register()
        miss = copy.deepcopy(PRICES)
        miss["SMH"]["2026-10-26"]["close"] = 97.
        loader = Mock(side_effect=lambda ticker: miss[ticker])
        self.assertEqual(f.review_due(path=self.path, now=END, load_prices=loader)["evaluated"], 1)
        loader.reset_mock()
        self.assertEqual(f.review_due(path=self.path, now=END + timedelta(days=1), load_prices=loader)["evaluated"], 0)
        loader.assert_not_called()
        read = f.read(path=self.path, now=END)
        self.assertEqual(read["counts"]["miss"], 1)
        self.assertEqual(read["recent"][0]["id"], first["id"])
        self.assertEqual(read["recent"][0]["outcome"]["status"], "MISS")
        flat = copy.deepcopy(PRICES)
        flat["SMH"]["2026-10-26"]["close"] = 101.1
        self.assertEqual(f.evaluate(first, flat, now=END)["status"], "FLAT")

    def test_no_db_read_creates_nothing_and_young_forecast_does_not_download(self):
        self.assertEqual(f.read(path=self.path, now=NOW)["status"], "NO_FORECASTS")
        self.assertFalse(self.path.exists())
        self.register()
        loader = Mock()
        f.review_due(path=self.path, now=NOW, load_prices=loader)
        loader.assert_not_called()
        self.assertEqual(f.read(path=self.path, now=NOW)["counts"]["pending"], 1)

    def test_failure_is_pending_then_unresolved_not_success(self):
        self.register()
        loader = Mock(side_effect=RuntimeError("feed down"))
        f.review_due(path=self.path, now=END, load_prices=loader)
        self.assertEqual(f.read(path=self.path, now=END)["counts"]["pending"], 1)
        loader.reset_mock()
        f.review_due(path=self.path, now=END + timedelta(minutes=30), load_prices=loader)
        loader.assert_not_called()
        f.review_due(path=self.path, now=NOW + timedelta(days=61), load_prices=loader)
        self.assertEqual(f.read(path=self.path)["counts"]["unresolved"], 1)


if __name__ == "__main__":
    unittest.main()
