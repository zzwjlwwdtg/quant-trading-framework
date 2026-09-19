"""Purged walk-forward validation for fixed, explainable strategy rules."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class WalkForwardSplit:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def purged_walk_forward_splits(
    n_samples: int,
    *,
    train_size: int = 160,
    test_size: int = 40,
    purge: int = 7,
    embargo: int = 3,
    anchored: bool = True,
) -> list[WalkForwardSplit]:
    """Create chronological train/test folds with purge and embargo gaps."""
    if min(n_samples, train_size, test_size) <= 0:
        return []
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be non-negative")
    splits: list[WalkForwardSplit] = []
    train_end = train_size
    while True:
        test_start = train_end + purge
        test_end = min(n_samples, test_start + test_size)
        if test_end - test_start < max(10, test_size // 2):
            break
        train_start = 0 if anchored else max(0, train_end - train_size)
        splits.append(WalkForwardSplit(train_start, train_end, test_start, test_end))
        train_end += test_size + embargo
        if train_end + purge >= n_samples:
            break
    return splits


def _stats(trades: list[dict], hold: int) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0.0, "avg_ret": 0.0, "sharpe": 0.0}
    returns = [float(x["ret"]) for x in trades]
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / max(1, len(returns) - 1)
    std = math.sqrt(variance)
    return {
        "n": len(trades),
        "win_rate": sum(1 for x in trades if x["win"]) / len(trades) * 100,
        "avg_ret": mean,
        "sharpe": mean / std * math.sqrt(252 / max(1, hold)) if std > 0 else 0.0,
    }


def evaluate_rule_walk_forward(
    rule: dict,
    rows,
    check_rule: Callable,
    *,
    train_size: int | None = None,
    test_size: int = 40,
    embargo: int = 3,
) -> dict:
    """Evaluate a fixed rule only on purged chronological OOS folds."""
    n = len(rows)
    hold = max(1, int(rule.get("hold", 1)))
    if train_size is None:
        train_size = min(160, max(60, n // 2))
    splits = purged_walk_forward_splits(
        n,
        train_size=train_size,
        test_size=min(test_size, max(20, n // 5)),
        purge=hold,
        embargo=max(embargo, hold // 2),
    )
    action = rule.get("action") or ""
    is_bull = action in {"WATCH_BUY", "BUY", "WATCH_BUY_PROBE", "ADD", "PROBE"}
    # F06 fix (2026-09-19, audit): REDUCE/EXIT/SELL 是多头减仓, 不等于开空.
    # 之前 avg_ret 用原始 raw return, REDUCE 后价格跌 → ret<0 → 平均 avg_ret 负,
    # 但方向 100% 命中 (win_rate 高), 出现 "胜率 100%, 收益负" 的矛盾.
    # Fix: reduce 动作用 effective_ret = -ret (避损收益), 与 win 定义对齐.
    is_reduce = action in {"REDUCE", "REDUCE_RISK", "EXIT", "SELL", "SELL_ALL"}
    folds = []
    all_oos: list[dict] = []
    for split in splits:
        trades = []
        last_entry = min(split.test_end, n - hold)
        for i in range(split.test_start, last_entry):
            row = rows.iloc[i] if hasattr(rows, "iloc") else rows[i]
            if not check_rule(row, rule):
                continue
            future = rows.iloc[i + hold] if hasattr(rows, "iloc") else rows[i + hold]
            entry = float(row["close"])
            exit_price = float(future["close"])
            raw_ret = (exit_price - entry) / entry * 100
            # 方向判定: bull → ret > 0 是命中; reduce → ret < 0 是命中 (成功预警下跌)
            if is_bull:
                win = raw_ret > 0
                effective_ret = raw_ret
            elif is_reduce:
                win = raw_ret < 0
                effective_ret = -raw_ret   # avoided loss = negated raw return
            else:
                # HOLD/CAUTION/未知 action: 只跟踪方向对错的 raw movement
                win = raw_ret > 0
                effective_ret = raw_ret
            date_value = row.get("time_key") if hasattr(row, "get") else None
            trades.append({
                "index":         i,
                "date":          str(getattr(date_value, "date", lambda: date_value)()),
                "ret":           effective_ret,   # 用 effective 让 win 与 avg 方向一致
                "raw_ret":       raw_ret,          # 保留原始价格变化供审计
                "win":           win,
                "action_class":  "bull" if is_bull else ("reduce" if is_reduce else "neutral"),
            })
        fold_stats = _stats(trades, hold)
        fold_stats.update({
            "train": [split.train_start, split.train_end],
            "test": [split.test_start, split.test_end],
        })
        folds.append(fold_stats)
        all_oos.extend(trades)
    aggregate = _stats(all_oos, hold)
    positive_folds = sum(1 for fold in folds if fold["n"] > 0 and fold["win_rate"] >= 50)
    active_folds = sum(1 for fold in folds if fold["n"] > 0)
    stability = positive_folds / active_folds if active_folds else 0.0
    passed = bool(
        aggregate["n"] >= 5
        and aggregate["win_rate"] >= 52.0
        and stability >= 0.5
    )
    return {
        "method": "purged_walk_forward",
        "purge_days": hold,
        "embargo_days": max(embargo, hold // 2),
        "fold_count": len(folds),
        "active_fold_count": active_folds,
        "oos_n": aggregate["n"],
        "oos_win_rate": round(aggregate["win_rate"], 1),
        "oos_avg_ret": round(aggregate["avg_ret"], 3),
        "oos_sharpe": round(aggregate["sharpe"], 2),
        "fold_stability": round(stability, 3),
        "passed": passed,
        "folds": folds,
    }

