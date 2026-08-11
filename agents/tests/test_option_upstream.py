from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import moomoo_data


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class OptionChainRateLimitTests(unittest.TestCase):
    def setUp(self):
        moomoo_data._reset_option_chain_rate_limiter_for_tests()

    def tearDown(self):
        moomoo_data._reset_option_chain_rate_limiter_for_tests()

    def test_eleventh_request_waits_for_rolling_window(self):
        clock = _FakeClock()
        with patch.object(moomoo_data, "_OPTION_CHAIN_RATE_LIMIT", 10), \
             patch.object(moomoo_data, "_OPTION_CHAIN_WINDOW_SEC", 30.0), \
             patch.object(moomoo_data, "_OPTION_CHAIN_RATE_PADDING_SEC", 0.25):
            for _ in range(10):
                waited = moomoo_data._wait_for_option_chain_slot(
                    now_fn=clock.monotonic, sleep_fn=clock.sleep
                )
                self.assertEqual(waited, 0.0)
            waited = moomoo_data._wait_for_option_chain_slot(
                now_fn=clock.monotonic, sleep_fn=clock.sleep
            )

        self.assertEqual(clock.sleeps, [30.25])
        self.assertEqual(waited, 30.25)

    def test_rate_limit_errors_are_recognized_for_retry(self):
        for message in (
            "A maximum of 10 requests per 30 seconds",
            "too many requests, please try later",
            "请求频率超限",
        ):
            with self.subTest(message=message):
                self.assertTrue(moomoo_data._is_option_chain_rate_limit_error(message))


if __name__ == "__main__":
    unittest.main()
