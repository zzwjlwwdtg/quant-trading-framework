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

_CONFIG_PATH = Path(SIGNALS_DIR) / "thesis_config.json"
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
    }
