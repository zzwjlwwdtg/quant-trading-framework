"""Explicit analyze freezes a research record. HTTP GET never does."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import DecisionInput, RuleConfig, build_decision, render_report
from .store import compare_predictions, freeze, get_run


def main():
    # Windows Japanese locale must not turn a valid Chinese JSON result into an empty pipe.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="A-share P0 research only; no validated model installed")
    p.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1] / "signals/cn_decisions/runs.sqlite3")
    sub = p.add_subparsers(dest="action", required=True)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("--symbol", required=True)
    analyze.add_argument("--horizon", type=int, choices=(1, 3, 5), required=True)
    analyze.add_argument("--asof", help="ISO offset timestamp; not a historical point-in-time guarantee")
    analyze.add_argument("--fixture", type=Path, help="synthetic input, forcibly marked mock")
    analyze.add_argument("--json", action="store_true")
    show = sub.add_parser("show"); show.add_argument("run_id")
    compare = sub.add_parser("compare"); compare.add_argument("old_run_id"); compare.add_argument("new_run_id")
    a = p.parse_args()
    if a.action == "analyze":
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        cutoff = datetime.fromisoformat(a.asof) if a.asof else now
        if cutoff.tzinfo is None:
            p.error("--asof requires a timezone offset")
        if a.fixture:
            payload = json.loads(a.fixture.read_text(encoding="utf-8"))
            payload.update(symbol=a.symbol, horizon_trading_days=a.horizon, is_mock_data=True)
            req = DecisionInput.model_validate_json(json.dumps(payload))
        else:
            req = DecisionInput(symbol=a.symbol, horizon_trading_days=a.horizon,
                                cutoff_time=cutoff, prediction_time=now)
        result = freeze(build_decision(req, RuleConfig()), a.db)
        print(result.model_dump_json() if a.json else render_report(result))
    elif a.action == "show":
        result = get_run(a.db, a.run_id)
        if result is None:
            p.error("run_id not found")
        print(result.model_dump_json())
    else:
        old, new = get_run(a.db, a.old_run_id), get_run(a.db, a.new_run_id)
        if old is None or new is None:
            p.error("run_id not found")
        print(json.dumps(compare_predictions(old, new), ensure_ascii=False))


if __name__ == "__main__":
    main()
