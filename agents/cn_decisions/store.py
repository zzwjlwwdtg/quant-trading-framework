"""Independent append-only public research ledger. No account or existing forecast DB."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .core import DecisionResult, canonical


def _connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS cn_decision_runs(
        run_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, horizon INTEGER NOT NULL,
        prediction_time TEXT NOT NULL, body TEXT NOT NULL);
      CREATE TRIGGER IF NOT EXISTS cn_runs_no_update BEFORE UPDATE ON cn_decision_runs
        BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DECISION'); END;
      CREATE TRIGGER IF NOT EXISTS cn_runs_no_delete BEFORE DELETE ON cn_decision_runs
        BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DECISION'); END;
    """)
    return db


def freeze(result: DecisionResult, path: Path) -> DecisionResult:
    result = DecisionResult.model_validate_json(result.model_dump_json())
    path.parent.mkdir(parents=True, exist_ok=True)
    db = _connect(path)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT body FROM cn_decision_runs WHERE run_id=?", (result.run_id,)).fetchone()
            if old:
                prior = DecisionResult.model_validate_json(old[0])
                left, right = prior.model_dump(mode="json"), result.model_dump(mode="json")
                left.pop("prediction_time"); right.pop("prediction_time")
                if canonical(left) != canonical(right):
                    raise ValueError("SAME_SNAPSHOT_MODEL_RULE_CONFLICT")
                return prior
            db.execute("INSERT INTO cn_decision_runs VALUES(?,?,?,?,?)", (result.run_id, result.symbol,
                       result.horizon_trading_days, result.prediction_time.isoformat(), result.model_dump_json()))
            return result
    finally:
        db.close()


def get_run(path: Path, run_id: str) -> DecisionResult | None:
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        row = db.execute("SELECT body FROM cn_decision_runs WHERE run_id=?", (run_id,)).fetchone()
        return DecisionResult.model_validate_json(row[0]) if row else None
    finally:
        db.close()


def compare_predictions(old: DecisionResult, new: DecisionResult) -> dict:
    old = DecisionResult.model_validate_json(old.model_dump_json())
    new = DecisionResult.model_validate_json(new.model_dump_json())
    if (old.symbol, old.horizon_trading_days) != (new.symbol, new.horizon_trading_days):
        raise ValueError("DIFFERENT_SYMBOL_OR_HORIZON_NOT_COMPARABLE")
    same = old.run_id == new.run_id
    if same and (old.direction, old.trade_action, old.calibrated_p_up, old.expected_net_return) != (
            new.direction, new.trade_action, new.calibrated_p_up, new.expected_net_return):
        raise ValueError("UNEXPLAINED_MACHINE_CHANGE")
    causes = [field for field in ("data_snapshot_id", "model_id", "model_version", "decision_rule_version",
                                  "rule_config", "validation", "costs", "risk_ok", "executable", "quality_flags")
              if getattr(old, field) != getattr(new, field)]
    old_factors = {f.id: f.value for f in old.top_factors}
    factors = [{"id": f.id, "old": old_factors.get(f.id), "new": f.value,
                "delta": f.value - old_factors[f.id] if f.id in old_factors else None}
               for f in new.top_factors if old_factors.get(f.id) != f.value]
    factors.sort(key=lambda x: abs(x["delta"]) if x["delta"] is not None else float("inf"), reverse=True)
    def delta(a, b):
        return b - a if a is not None and b is not None else None
    return {"old_run_id": old.run_id, "new_run_id": new.run_id, "same_identity": same,
            "direction_changed": old.direction != new.direction, "causes": causes,
            "old_prediction_time": old.prediction_time.isoformat(), "new_prediction_time": new.prediction_time.isoformat(),
            "delta_p_up": delta(old.calibrated_p_up, new.calibrated_p_up),
            "delta_expected_return": delta(old.expected_net_return, new.expected_net_return),
            "old_thresholds": json.loads(old.rule_config.model_dump_json()),
            "new_thresholds": json.loads(new.rule_config.model_dump_json()), "factor_changes": factors[:3]}


def read(symbol: str, horizon: int, *, path: Path | None = None) -> dict:
    """GET is read-only: no implicit freeze, model, calendar inference or download."""
    from .core import DecisionInput
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    DecisionInput(symbol=symbol, prediction_time=now, cutoff_time=now, horizon_trading_days=horizon)
    path = path or Path(__file__).resolve().parents[1] / "signals/cn_decisions/runs.sqlite3"
    if not path.is_file():
        return {"version": 1, "scope": "CN_A_FROZEN_RESEARCH_ONLY", "status": "NO_RECORD",
                "symbol": symbol, "horizon_trading_days": horizon, "result": None}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        row = db.execute("SELECT body FROM cn_decision_runs WHERE symbol=? AND horizon=? ORDER BY prediction_time DESC LIMIT 1",
                         (symbol, horizon)).fetchone()
    finally:
        db.close()
    result = DecisionResult.model_validate_json(row[0]) if row else None
    return {"version": 1, "scope": "CN_A_FROZEN_RESEARCH_ONLY", "status": "FROZEN" if result else "NO_RECORD",
            "symbol": symbol, "horizon_trading_days": horizon,
            "result": result.model_dump(mode="json") if result else None}
