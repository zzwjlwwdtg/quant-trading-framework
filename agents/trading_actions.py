"""trading_actions.py — WP02: canonical Action enum + legacy compatibility.

audit finding F02/WP02: 决策链路多处用字符串 action ("BUY", "WATCH_BUY", etc),
enum 不清晰. 新加 (2026-09-20) 一个 canonical enum, 兼容旧字符串.

## 设计原则

- Action enum 是 canonical (每个 semantic 一个 case)
- 旧字符串通过 LEGACY_ACTION_MAP 映射到 enum
- from_str() 是 permissive: 未知字符串返 None, 不 raise
- 迁移期两者共存, 新代码用 enum, 旧代码字符串仍工作

## Enum 定义 (per audit 明确)

- WATCH   → 观察, 不下单 (显示型, 用户参考)
- PROBE   → 小仓试仓 (crisis / probe pool)
- BUY     → 全仓开新
- ADD     → 加仓 (pyramid on existing position)
- HOLD    → 持有不动
- REDUCE  → 减仓 (部分退出)
- EXIT    → 全平仓
- CAUTION → 警告 (非订单)

## 与 trading_contracts.py 的关系

`trading_contracts.py` 有 BUY_ACTIONS / SELL_ACTIONS 等字符串 frozenset —
迁移期保留. 本模块提供 typed 层, is_buy(action) / is_reduce(action) etc 支持
str + Action 混合输入.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Union


class Action(str, Enum):
    """Canonical trading action. Inherits from str so `Action.BUY == "BUY"`
    for tolerant comparison with legacy string code."""
    WATCH   = "WATCH"       # 观察, 不下单
    PROBE   = "PROBE"       # 小仓试仓
    BUY     = "BUY"         # 全仓开新
    ADD     = "ADD"         # 加仓
    HOLD    = "HOLD"        # 持有
    REDUCE  = "REDUCE"      # 部分减仓
    EXIT    = "EXIT"        # 全平
    CAUTION = "CAUTION"     # 警告 (非订单)

    @classmethod
    def parse(cls, value: Union[str, "Action", None]) -> Optional["Action"]:
        """Permissive parser: str / Action / None → Action or None.
        Unknown strings return None, does NOT raise."""
        if value is None:
            return None
        if isinstance(value, Action):
            return value
        s = str(value).upper().strip()
        # Direct enum match
        for a in cls:
            if a.value == s:
                return a
        # Legacy alias lookup
        return LEGACY_ACTION_MAP.get(s)


# Legacy action strings → canonical Action.
# 每次决策系统里遇到旧字符串, 通过这个 map 转 typed enum.
LEGACY_ACTION_MAP: dict[str, Action] = {
    # 旧字符串                        canonical
    "WATCH_BUY":                     Action.BUY,      # 现执行, 语义 = BUY
    "WATCH_BUY_PROBE":               Action.PROBE,    # 小仓 = PROBE
    "WATCH_BUY_LONG_HOLD":           Action.WATCH,    # 显示型 = WATCH
    "SELL":                          Action.EXIT,     # 全平
    "SELL_ALL":                      Action.EXIT,
    "REDUCE_RISK":                   Action.REDUCE,
    "PYRAMID_ADD":                   Action.ADD,      # 加仓 pyramid
    "REBALANCE_UP":                  Action.ADD,      # 再平衡加
    "REBALANCE_DOWN":                Action.REDUCE,   # 再平衡减
    "TAKE_PROFIT":                   Action.REDUCE,
    "TRAILING_STOP":                 Action.EXIT,
    "STOP_LOSS":                     Action.EXIT,
}


# ─── Semantic groupings (与 trading_contracts.py BUY_ACTIONS etc 平行) ────

BUY_TYPE_ACTIONS = frozenset({Action.WATCH, Action.PROBE, Action.BUY, Action.ADD})
REDUCE_TYPE_ACTIONS = frozenset({Action.REDUCE, Action.EXIT})
NON_ORDER_ACTIONS = frozenset({Action.WATCH, Action.HOLD, Action.CAUTION})
ORDER_ACTIONS = frozenset({Action.PROBE, Action.BUY, Action.ADD,
                            Action.REDUCE, Action.EXIT})


def is_buy_like(action: Union[str, Action, None]) -> bool:
    """True for any bullish/opening action (WATCH / PROBE / BUY / ADD).
    Tolerates str / Action / None input."""
    a = Action.parse(action)
    return a is not None and a in BUY_TYPE_ACTIONS


def is_reduce_like(action: Union[str, Action, None]) -> bool:
    """True for REDUCE / EXIT (position-shrinking)."""
    a = Action.parse(action)
    return a is not None and a in REDUCE_TYPE_ACTIONS


def is_order(action: Union[str, Action, None]) -> bool:
    """True if action normally results in an order submission.
    (WATCH / HOLD / CAUTION are display-only.)"""
    a = Action.parse(action)
    return a is not None and a in ORDER_ACTIONS
