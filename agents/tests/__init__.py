"""Isolated regression tests for the Trading Agents runtime.

The suite exercises live order paths, so importing this package redirects every
mutable execution file to one temporary directory.  A test that forgets to
patch a path can no longer overwrite trader_state, stop lines, ledgers, or the
orchestrator lock (2026-10-20: coupling tests overwrote live LITE/TQQQ lines).
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

_TMP = tempfile.TemporaryDirectory(prefix="fsi-agents-tests-")
_TMP_DIR = Path(_TMP.name)
_PATCHES: list = []


def _redirect(module_name: str, attr: str, filename: str) -> None:
    module = __import__(module_name, fromlist=[attr])
    p = patch.object(module, attr, _TMP_DIR / filename)
    p.start()
    _PATCHES.append(p)


for _spec in (
    ("paper_trader", "STATE_PATH", "trader_state.json"),
    ("paper_trader", "TRADE_LOG_PATH", "trade_log.jsonl"),
    ("paper_trader", "EXECUTION_LOG_PATH", "execution_ledger.jsonl"),
    ("paper_trader", "COHORT_FIRED_LEDGER_PATH", "cohort_fired_ledger.jsonl"),
    ("paper_trader", "UNIVERSE_PATH", "universe_state.json"),
    ("fill_ledger", "EXEC_LEDGER_PATH", "execution_ledger.jsonl"),
    ("stop_line_guard", "LINES", "stop_lines.json"),
    ("stop_line_guard", "BLOCKS", "stop_line_blocks.jsonl"),
    ("orchestrator", "_REPORT_STATE_PATH", "report_state.json"),
    ("orchestrator", "_LOCK_PATH", ".orchestrator.lock"),
    ("_watchdog", "LOCK_PATH", ".orchestrator.lock"),
    ("_watchdog", "LOG_PATH", "watchdog.jsonl"),
):
    _redirect(*_spec)

# Keep the file logger out of logs/run_YYYYMMDD.log; stdout logging still works.
import notifier  # noqa: E402  (import triggers logging.basicConfig)

_root = logging.getLogger()
for _handler in list(_root.handlers):
    if isinstance(_handler, notifier._DailyLogHandler):
        _root.removeHandler(_handler)
        _handler.close()
