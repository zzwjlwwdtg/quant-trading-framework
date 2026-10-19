"""止损线守卫 (2026-10-19 用户要求: "总之是有一条线的吧").

止损在哪条线卖出, 价格回到这条线上方之前, 同一标的不再买入. 买和卖看同一条线, 避免
"止损卖出 → 几小时后又买回" (例: 10/16 TQQQ 14:58 在 $68.50 止损, 15:46 又在 $67.86 试图买回 296 股).

  - 记线: 软件止损 = 触发价 stop_price; 移动止损 = 入场后最高价 × (1 − 回撤比例)
  - 拦截: 买入价 ≤ 线 → 不下单 (在现金筹措之前拦, 不会为一笔被拦的买单先卖 SHY)
  - 解除: 买入时价格 > 线 → 线清除, 照常下单
  - 不拦再平衡 (REBALANCE) 的买入: 它按组合权重运作, 拦下会打乱现金计算
只会少买, 不会多买, 也不改任何卖出 / 止损逻辑.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SIGNALS = Path(__file__).resolve().parent / "signals"
LINES = SIGNALS / "stop_lines.json"
BLOCKS = SIGNALS / "stop_line_blocks.jsonl"


def _key(ticker: str) -> str:
    t = str(ticker or "").upper()
    return t if t.startswith("US.") else f"US.{t}"


def _load() -> dict:
    try:
        return json.loads(LINES.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    LINES.parent.mkdir(parents=True, exist_ok=True)
    tmp = LINES.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LINES)


def record(ticker: str, line: float, kind: str, exit_price: float | None = None) -> None:
    """止损卖出成功后调用. 失败只记日志, 不能影响卖出."""
    try:
        line = float(line)
        if line <= 0:
            return
        d = _load()
        d[_key(ticker)] = {"line": round(line, 4), "kind": kind,
                           "exit_price": float(exit_price) if exit_price else None,
                           "ts": datetime.now(timezone.utc).isoformat()}
        _save(d)
    except Exception:
        pass


def check_buy(ticker: str, price: float, tag: str = "") -> tuple[bool, str]:
    """返回 (允许, 原因). 任何异常都放行 (守卫自身出错不能卡住正常交易, 其余风控照常)."""
    try:
        if "REBALANCE" in str(tag or "").upper():
            return True, "rebalance_exempt"
        d = _load()
        k = _key(ticker)
        rec = d.get(k)
        if not rec:
            return True, "no_line"
        line, price = float(rec.get("line") or 0), float(price or 0)
        if price <= 0 or line <= 0:
            return True, "no_price"
        if price > line:
            d.pop(k, None)          # 价格回到线上方 → 线解除
            _save(d)
            return True, f"reclaimed {price:.2f} > {line:.2f}"
        reason = (f"止损线未收复: 现价 ${price:.2f} ≤ {rec.get('kind')} 线 ${line:.2f}"
                  f" (止损于 {str(rec.get('ts'))[:16]})")
        try:
            with open(BLOCKS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "ticker": k, "price": price,
                                    "line": line, "kind": rec.get("kind"), "tag": tag, "reason": reason},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass
        return False, reason
    except Exception:
        return True, "guard_error"


def trailing_line(entry_high: float, trail_pct: float) -> float:
    return float(entry_high) * (1 - float(trail_pct))
