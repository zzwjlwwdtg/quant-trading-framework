"""backtest_verdicts.py — 回测 verdict **单一源** (机器可读, 系统可消费)

设计目的:
    每个 _backtest_*.py 跑完的 verdict (pass/edge/reject + 参数 + 阈值) 之前只印到
    stdout, 用户看完就散. 系统下次不会知道 "上次 stop backtest 说 trail_8 是最优".
    本模块让 verdict 落盘为 json, 谁需要就 read_verdict(name), 天然闭环.

结构 (signals/backtest_verdicts/<name>.json):
    {
      "name": "stop_distance",
      "verdict": "keep_baseline",     # pass / edge / reject / keep_baseline
      "run_at": "2026-09-02T12:00:00+00:00",
      "params": {"universe": "trade_log", "n": 24, "horizons": [30]},
      "metrics": {"baseline_avg": -2.21, "best_alt": "trail_5", "best_alt_delta_pp": 0.04},
      "conclusion": "trail_8 已本地最优, 收窄扩宽都无改善",
      "next_review_days": 60,          # 60 天后再跑
      "should_integrate": false,        # verdict 是否值得改代码
      "recommendation": "keep TRAILING_STOP_BASE_PCT=0.08"
    }

API:
    write_verdict(name, verdict, **fields) -> Path
    read_verdict(name) -> dict | None
    list_verdicts_needing_review() -> list[dict]  # next_review 到期的
    all_verdicts() -> list[dict]                  # 全部 (dashboard 用)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from config import SIGNALS_DIR
from atomic_io import atomic_write_json

_VERDICTS_DIR = Path(SIGNALS_DIR) / "backtest_verdicts"

_VALID_VERDICTS = {"pass", "edge", "reject", "keep_baseline", "inconclusive"}


def _path(name: str) -> Path:
    """安全化 name → 允许 [a-z0-9_-]."""
    safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name.lower())
    return _VERDICTS_DIR / f"{safe}.json"


def write_verdict(name: str, verdict: str, *,
                   conclusion: str = "",
                   metrics: Optional[dict] = None,
                   params: Optional[dict] = None,
                   next_review_days: int = 60,
                   should_integrate: bool = False,
                   recommendation: str = "") -> Path:
    """写 verdict. atomic, 覆盖同 name 的老版本. 返写入 path."""
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"invalid verdict {verdict!r}, must be one of {_VALID_VERDICTS}")
    _VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name)
    payload = {
        "name":              name,
        "verdict":           verdict,
        "run_at":            datetime.now(timezone.utc).isoformat(),
        "conclusion":        conclusion,
        "metrics":           metrics or {},
        "params":            params or {},
        "next_review_days":  int(next_review_days),
        "should_integrate":  bool(should_integrate),
        "recommendation":    recommendation,
    }
    atomic_write_json(p, payload)
    return p


def read_verdict(name: str) -> Optional[dict]:
    p = _path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def all_verdicts() -> list[dict]:
    if not _VERDICTS_DIR.exists():
        return []
    out = []
    for p in sorted(_VERDICTS_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def list_verdicts_needing_review() -> list[dict]:
    """返 next_review_days 已过期的 verdict, dashboard/weekly 用作提醒."""
    now = datetime.now(timezone.utc)
    stale = []
    for v in all_verdicts():
        try:
            run_at = datetime.fromisoformat(v["run_at"].replace("Z", "+00:00"))
            interval = timedelta(days=int(v.get("next_review_days", 60)))
            if now - run_at > interval:
                age_days = (now - run_at).days
                stale.append({**v, "age_days": age_days})
        except Exception:
            continue
    return stale
