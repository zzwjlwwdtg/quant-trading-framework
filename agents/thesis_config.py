"""thesis_config.py — thesis 状态**单一源**, decision_agent / rebalance 等读它做硬过滤

设计目的:
    memory 里的 thesis (如 project_thesis_2026Q3.md) 是给 AI 读的自然语言,
    rule engine 不消费. 结果 2026-07 → 09 paper trader 违反 thesis avoid semi,
    -24% drawdown. 本模块把 thesis 结构化, decision 每次调用都自动过滤.

单一入口:
    is_ticker_blacklisted(ticker)  → BUY 前必查
    is_ticker_whitelisted(ticker)  → 可选加分 (thesis 明确看多)
    check_invalidation(macro)      → 返 [] 或 [triggered_condition_id, ...]
    thesis_needs_review()          → 返 bool + reason (每 review_interval_days review)
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from config import SIGNALS_DIR

_CONFIG_PATH  = Path(SIGNALS_DIR) / "thesis_config.json"
_ARCHIVE_PATH = Path(SIGNALS_DIR) / "thesis_archive.jsonl"
_CACHE: dict = {"mtime": 0, "data": None}


def _load() -> Optional[dict]:
    """读 thesis_config.json, mtime 变化时刷新 cache (hot reload)."""
    if not _CONFIG_PATH.exists():
        return None
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
        if _CACHE["data"] is None or mtime != _CACHE["mtime"]:
            _CACHE["data"] = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            _CACHE["mtime"] = mtime
        return _CACHE["data"]
    except Exception:
        return None


def _normalize_ticker(ticker: str) -> str:
    """去掉 US./HK./JP. 前缀, 统一大写."""
    t = (ticker or "").upper().strip()
    for prefix in ("US.", "HK.", "JP."):
        if t.startswith(prefix):
            t = t[len(prefix):]
    return t


def _match_ticker(ticker: str, ticker_list: list[str]) -> bool:
    """匹配时忽略前缀 (US.SOXL 匹配列表里的 US.SOXL 或 SOXL)."""
    if not ticker or not ticker_list:
        return False
    target = _normalize_ticker(ticker)
    for t in ticker_list:
        if _normalize_ticker(t) == target:
            return True
    return False


def is_ticker_blacklisted(ticker: str) -> tuple[bool, str]:
    """返 (True/False, reason). blacklist 命中时 reason 是 thesis 拒绝理由."""
    cfg = _load()
    if not cfg:
        return False, ""
    blacklist = cfg.get("blacklist_tickers", [])
    if _match_ticker(ticker, blacklist):
        return True, cfg.get("blacklist_reason", "thesis_blacklist")
    return False, ""


def is_ticker_whitelisted(ticker: str) -> tuple[bool, str]:
    cfg = _load()
    if not cfg:
        return False, ""
    if _match_ticker(ticker, cfg.get("whitelist_tickers", [])):
        return True, cfg.get("whitelist_reason", "thesis_whitelist")
    return False, ""


def is_ticker_soft_blacklisted(ticker: str) -> tuple[bool, str, dict]:
    """Soft block (2026-09-18 加): whitelist 移除的 ticker 需要更高置信度才能 BUY.

    Returns:
      (True, reason, {"min_confidence": N, "since": "YYYY-MM-DD"})
      (False, "", {}) if not soft-blocked

    decision_agent._apply_thesis_filter 后续用: 若 soft-blocked 且 result.confidence
    < min_confidence → 降级 HOLD. min_confidence 未指定时默认 7.

    Schema 防御 (2026-09-19 加): config 手误 (list 而非 dict / entry 非 dict /
    min_confidence 非数字) 时 fail-safe 处理, 不 crash.
    """
    cfg = _load()
    if not cfg:
        return False, "", {}
    soft = cfg.get("soft_blacklist", {})
    if not isinstance(soft, dict):
        # 手误: soft_blacklist 写成 list/其他类型 → 静默降级为空 dict
        # 不 crash 但也不 block (fail-open, 因为 soft_blacklist 是 defense-in-depth)
        return False, "", {}
    if not soft:
        return False, "", {}
    target = _normalize_ticker(ticker)
    default_min_conf = 7
    for key, meta in soft.items():
        if _normalize_ticker(key) != target:
            continue
        if not isinstance(meta, dict):
            # entry 应是 dict, 不是就用默认 min_conf 且 fallback reason
            return True, cfg.get("soft_blacklist_reason", "soft_thesis_block"), {
                "min_confidence": default_min_conf,
                "since":          "",
            }
        # min_confidence 应可转 int, 不能就用默认
        try:
            min_conf = int(meta.get("min_confidence", default_min_conf))
        except (TypeError, ValueError):
            min_conf = default_min_conf
        reason = meta.get("reason", cfg.get("soft_blacklist_reason", "soft_thesis_block"))
        return True, reason, {
            "min_confidence": min_conf,
            "since":          meta.get("since", ""),
        }
    return False, "", {}


def get_thesis_version() -> Optional[str]:
    cfg = _load()
    return cfg.get("version") if cfg else None


def thesis_needs_review() -> tuple[bool, str]:
    """按 review_interval_days 判 config 是否 stale."""
    cfg = _load()
    if not cfg:
        return False, "no_config"
    interval = int(cfg.get("review_interval_days", 30))
    last = cfg.get("last_reviewed_at")
    if not last:
        return True, "no_last_reviewed_at"
    try:
        last_d = date.fromisoformat(last)
    except Exception:
        return True, "invalid_last_reviewed_at"
    age = (date.today() - last_d).days
    if age > interval:
        return True, f"{age}d since last review (interval {interval}d)"
    return False, f"{age}d since last review (interval {interval}d)"


def check_invalidation(macro: dict) -> list[dict]:
    """给定 macro dict, 检查所有 invalidation_conditions, 返触发条件列表.
    每个元素: {id, metric, actual, threshold, description}."""
    cfg = _load()
    if not cfg:
        return []
    triggered = []
    for cond in cfg.get("invalidation_conditions", []):
        metric = cond.get("metric")
        op = cond.get("operator", ">")
        threshold = cond.get("threshold")
        actual = macro.get(metric) if macro else None
        if actual is None:
            continue
        hit = False
        try:
            if op == ">": hit = float(actual) > float(threshold)
            elif op == ">=": hit = float(actual) >= float(threshold)
            elif op == "<": hit = float(actual) < float(threshold)
            elif op == "<=": hit = float(actual) <= float(threshold)
            elif op == "==": hit = actual == threshold
        except (TypeError, ValueError):
            continue
        if hit:
            triggered.append({
                "id": cond.get("id"),
                "metric": metric,
                "operator": op,
                "threshold": threshold,
                "actual": actual,
                "description": cond.get("description", ""),
            })
    return triggered


def summary() -> dict:
    """返当前 thesis 状态摘要, 供 dashboard / log 用."""
    cfg = _load()
    if not cfg:
        return {"ok": False, "error": "no_thesis_config"}
    needs_review, review_msg = thesis_needs_review()
    return {
        "ok": True,
        "version": cfg.get("version"),
        "summary": cfg.get("thesis_summary"),
        "blacklist_count": len(cfg.get("blacklist_tickers", [])),
        "whitelist_count": len(cfg.get("whitelist_tickers", [])),
        "invalidation_count": len(cfg.get("invalidation_conditions", [])),
        "last_reviewed_at": cfg.get("last_reviewed_at"),
        "needs_review": needs_review,
        "review_msg": review_msg,
        "has_next_conjecture": bool(cfg.get("next_thesis_conjecture")),
        "archived_count": _count_archived(),
        "soft_blacklist_count": len(cfg.get("soft_blacklist", {}) or {}),
    }


def next_thesis_conjecture() -> Optional[dict]:
    """返当前 thesis_config 里 next_thesis_conjecture 块 (若存在).

    这是下一个 thesis 的候选假设 + 验证 metric + promote 条件, 供 dashboard
    展示 / _check_thesis_invalidation 触发时给出 next 路线图.
    """
    cfg = _load()
    if not cfg:
        return None
    return cfg.get("next_thesis_conjecture")


def list_retired_theses() -> list[dict]:
    """按时间顺序返 thesis_archive.jsonl 里所有 retired thesis 记录.

    每条: { retired_at, retired_reason, invalidation_evidence,
             promoted_to_version, thesis: {...full old config...} }
    """
    if not _ARCHIVE_PATH.exists():
        return []
    entries: list[dict] = []
    for line in _ARCHIVE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _count_archived() -> int:
    """便宜的 count, 不 parse JSON."""
    if not _ARCHIVE_PATH.exists():
        return 0
    try:
        return sum(1 for l in _ARCHIVE_PATH.read_text(encoding="utf-8").splitlines()
                    if l.strip())
    except Exception:
        return 0


def archive_thesis_for_promotion(
    new_thesis: dict,
    retired_reason: str,
    invalidation_evidence: Optional[list[dict]] = None,
) -> None:
    """在把新 thesis 写入 thesis_config.json **之前**调, 把当前 (即将 retire 的)
    版本 append 到 thesis_archive.jsonl. 这样每次 promote 都留下审计轨迹.

    调用方 pattern:
        cur = _load()          # 拿当前 (即将 retire 的)
        archive_thesis_for_promotion(new_thesis, "cpi hot triggered", [...])
        # ... 然后写 new_thesis 到 thesis_config.json
    """
    cur = _load()
    if not cur:
        return   # 无当前 config, 无需 archive (首次创建)
    entry = {
        "retired_at":            datetime.now().isoformat(timespec="seconds"),
        "retired_reason":        retired_reason,
        "invalidation_evidence": invalidation_evidence or [],
        "promoted_to_version":   new_thesis.get("version"),
        "thesis":                cur,
    }
    _ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
