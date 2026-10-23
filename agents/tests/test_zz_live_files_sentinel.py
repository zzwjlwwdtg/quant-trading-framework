"""Sentinel: the suite must leave every live execution file byte-identical.

tests/__init__.py snapshots the mutation-prone paths (trader state, ledgers,
stop lines, lock, AI call log, gate health/cache, alert logs) before the
redirect patches start.  Discovery sorts this module last; if any test forgot
its patch and wrote to a live path, the mismatch is reported here.

Run from agents/: python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest
from pathlib import Path

from . import LIVE_SNAPSHOT, snapshot_path


class LiveFilesSentinel(unittest.TestCase):

    def test_zz_live_files_unchanged(self):
        changed = {}
        for raw_path, before in LIVE_SNAPSHOT.items():
            after = snapshot_path(Path(raw_path))
            if after != before:
                changed[raw_path] = {"before": before, "after": after}
        self.assertEqual(
            changed, {},
            "live files changed during the test run; a test bypassed "
            f"tests/__init__.py isolation: {changed}",
        )


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
