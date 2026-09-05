"""overnight_signal.py — 判断当前时点是否适合 open BUY, 基于夜盘/盘前/盘后价

Phase A (2026-09-04 起): 只做分类 + shadow log, **不影响任何 score / decision**.
待 shadow log 累积 30-60 天后, 由 `_backtest_overnight_signal.py` 用事后前向收益
验证每类的实际胜率, 通过后再由 `top_picks._score_signal` 消费 classification 加权.

数据来源 (无需拉数据, 全部现成于 signals/{ticker}_latest.json):
- `pre_pct`         : 今日盘前 vs 昨日收盘, moomoo/commodity_overlay 已 enrich
- `pre_volume`      : 今日盘前成交量
- `overnight_pct`   : 昨夜隔夜段变化 (GLD 走 GC=F 期货 24h)
- `after_pct`       : 昨日盘后段变化
- `rsi_14` / `vol_ratio` / decision.action

分类逻辑 (跑前硬编码, 不后调):
  buy_dip          pre ∈ [-3, -0.5]% AND action ∈ BUY AND rsi<60          逢低机会
  momentum         pre ∈ [+0.3, +2]% AND action ∈ BUY AND pre_vol>2×norm  追涨 OK
  chase_risk       pre > +2%          AND action ∈ BUY                     追高警示
  panic_gap_down   pre < -3%                                              等 15min 企稳
  reversal_setup   after > +1% AND overnight < -0.5% AND pre < 0          mean-revert
  neutral          其它                                                   用白盘信号

**Phase A 阶段 caveat**: 上述阈值是**未回测的先验**, 只作分类展示. 真验证在
Phase B backtest 通过后.

用法:
  from overnight_signal import classify, classify_from_ticker, log_shadow
  info = classify(pre_pct=-1.5, overnight_pct=0.3, after_pct=0.1,
                   action="WATCH_BUY", rsi=45, pre_vol=None, pre_vol_avg=None)
  # info = {"classification": "buy_dip", "why": [...], "confidence": 3}

  # 从 signals/{ticker}_latest.json 直接分类
  r = classify_from_ticker("US.TQQQ")

  # shadow log (每 cycle 每 ticker 一条, 供未来 backtest)
  log_shadow(ticker, info, market_snapshot)

CLI:
  python overnight_signal.py [--ticker US.TQQQ] [--all]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import SIGNALS_DIR

_SIG_DIR = Path(SIGNALS_DIR)
_SHADOW_LOG = _SIG_DIR / "overnight_signal_log.jsonl"

_BUY_ACTIONS = {"BUY", "WATCH_BUY", "WATCH_BUY_PROBE"}

# 6 类 metadata (供 dashboard badge / AI prompt 展示用)
CLASSIFICATION_META = {
    "buy_dip": {
        "zh":    "逢低买入区",
        "en":    "Buy the dip",
        "icon":  "🟢",
        "css":   "ok",
        "action_hint": "开盘时可分批 BUY",
    },
    "momentum": {
        "zh":    "顺势追涨",
        "en":    "Momentum",
        "icon":  "🟢",
        "css":   "ok",
        "action_hint": "开盘 BUY 可以追",
    },
    "chase_risk": {
        "zh":    "追高警示",
        "en":    "Chase risk",
        "icon":  "🔴",
        "css":   "warn",
        "action_hint": "**不建议** open BUY, 等 fade",
    },
    "panic_gap_down": {
        "zh":    "恐慌 gap 下",
        "en":    "Panic gap-down",
        "icon":  "🟡",
        "css":   "warn",
        "action_hint": "等开盘 15min 企稳再看",
    },
    "reversal_setup": {
        "zh":    "反转 setup",
        "en":    "Reversal",
        "icon":  "🟡",
        "css":   "info",
        "action_hint": "mean-revert 提示, 谨慎",
    },
    "neutral": {
        "zh":    "中性",
        "en":    "Neutral",
        "icon":  "⚪",
        "css":   "info",
        "action_hint": "用白盘 signal 即可",
    },
}


def classify(*, pre_pct: Optional[float] = None,
             overnight_pct: Optional[float] = None,
             after_pct: Optional[float] = None,
             action: Optional[str] = None,
             rsi: Optional[float] = None,
             pre_vol: Optional[float] = None,
             pre_vol_avg: Optional[float] = None) -> dict:
    """核心分类函数. 所有输入都是可选 (None → skip 相关规则). 返 dict:
    {classification, why, confidence: 1-5, has_data: bool}."""
    reasons: list[str] = []
    action = (action or "").upper()
    is_buy = action in _BUY_ACTIONS

    # 数据可用性检查
    have_pre = pre_pct is not None
    have_overnight = overnight_pct is not None
    have_after = after_pct is not None
    has_data = have_pre or have_overnight or have_after
    if not has_data:
        return {"classification": "neutral", "why": ["无夜盘/盘前/盘后数据"],
                "confidence": 1, "has_data": False}

    # 分类逻辑 (顺序: 先判高风险, 再判机会, 最后 neutral)

    # 1) panic_gap_down: 盘前 <-3% (最高优先, 独立于 action)
    if have_pre and pre_pct < -3:
        reasons.append(f"盘前 {pre_pct:+.2f}% <-3% 恐慌 gap")
        return {"classification": "panic_gap_down", "why": reasons,
                "confidence": 4, "has_data": True}

    # 2) chase_risk: 盘前 >+2% + BUY 信号 (追高警示)
    if have_pre and pre_pct > 2 and is_buy:
        reasons.append(f"盘前 {pre_pct:+.2f}% >+2% 且规则 {action}")
        reasons.append("追高 open 通常 fade, 等回落")
        return {"classification": "chase_risk", "why": reasons,
                "confidence": 4, "has_data": True}

    # 3) reversal_setup: 昨盘后 >+1% + 隔夜 <-0.5% + 盘前 <0
    if (have_after and have_overnight and have_pre
            and after_pct > 1 and overnight_pct < -0.5 and pre_pct < 0):
        reasons.append(f"昨盘后 {after_pct:+.2f}% + 隔夜 {overnight_pct:+.2f}% + 盘前 {pre_pct:+.2f}%")
        reasons.append("盘后拉高 → 隔夜回吐 → 盘前继续跌: 典型 mean-revert setup")
        return {"classification": "reversal_setup", "why": reasons,
                "confidence": 3, "has_data": True}

    # 4) buy_dip: 盘前 [-3, -0.5]% + BUY + RSI<60
    if (have_pre and -3 <= pre_pct <= -0.5 and is_buy):
        rsi_ok = rsi is None or rsi < 60
        if rsi_ok:
            reasons.append(f"盘前 {pre_pct:+.2f}% 温和回调")
            reasons.append(f"规则 {action}" + (f" + RSI {rsi:.0f}<60 未超买" if rsi is not None else ""))
            return {"classification": "buy_dip", "why": reasons,
                    "confidence": 4, "has_data": True}
        else:
            reasons.append(f"盘前 {pre_pct:+.2f}% 但 RSI {rsi:.0f}≥60 已偏高, 不算逢低")

    # 5) momentum: 盘前 [+0.3, +2]% + BUY + 量 >2×
    if (have_pre and 0.3 <= pre_pct <= 2 and is_buy):
        vol_ok = pre_vol is None or pre_vol_avg is None or pre_vol_avg <= 0 or pre_vol > 2 * pre_vol_avg
        if vol_ok:
            reasons.append(f"盘前 {pre_pct:+.2f}% 温和上涨")
            reasons.append(f"规则 {action}" +
                           (f" + 盘前量 {pre_vol/pre_vol_avg:.1f}× 均值 (>2×)"
                            if pre_vol and pre_vol_avg and pre_vol_avg > 0 else " (量数据缺, 仍归 momentum)"))
            return {"classification": "momentum", "why": reasons,
                    "confidence": 3, "has_data": True}
        else:
            reasons.append(f"盘前 {pre_pct:+.2f}% 但量能不足 ({pre_vol/pre_vol_avg:.1f}×<2×)")

    # neutral fallback
    if have_pre:
        reasons.append(f"盘前 {pre_pct:+.2f}% + action={action or '?'}: 无明显 setup")
    else:
        reasons.append("无盘前数据, 只能中性")
    return {"classification": "neutral", "why": reasons,
            "confidence": 2, "has_data": has_data}


def classify_from_ticker(ticker: str) -> Optional[dict]:
    """从 signals/{ticker}_latest.json 抽字段调 classify. 返 None 若文件不存在."""
    # 支持 US.XXX 或 XXX
    stem_variants = [ticker.replace("US.", "").replace("JP.", "").replace("HK.", "")]
    for stem in stem_variants:
        p = _SIG_DIR / f"{stem}_latest.json"
        if p.exists():
            break
    else:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    m = data.get("market") or {}
    d = data.get("decision") or {}
    info = classify(
        pre_pct=m.get("pre_pct"),
        overnight_pct=m.get("overnight_pct"),
        after_pct=m.get("after_pct"),
        action=d.get("action"),
        rsi=m.get("rsi_14"),
        pre_vol=m.get("pre_volume"),
        pre_vol_avg=m.get("avg_volume_20"),   # 用 20d avg 做量对比 (approximation)
    )
    info["ticker"] = ticker
    info["price"] = m.get("price")
    return info


def log_shadow(ticker: str, info: dict, market_snapshot: Optional[dict] = None) -> None:
    """写 shadow log 一条. 供未来 backtest 用. 静默失败 (不阻塞主流程)."""
    if not info or not info.get("has_data"):
        return
    entry = {
        "ts":              datetime.now(timezone.utc).isoformat(),
        "ticker":          ticker,
        "classification":  info["classification"],
        "confidence":      info.get("confidence"),
        "why":             info.get("why", []),
    }
    # 快照关键 market 字段供 backtest 追溯
    if market_snapshot:
        snap = {}
        for k in ("price", "pre_pct", "overnight_pct", "after_pct",
                  "rsi_14", "pct_chg", "cum_5d_pct"):
            v = market_snapshot.get(k)
            if v is not None:
                snap[k] = v
        entry["snapshot"] = snap
    # P0-S1 fix (2026-09-05): 之前双层 try/except 全静默. 现在若持续 fail
    # 至少要落地 health 状态 + 每日一次警告 log, 避免 Phase B backtest 时数据全丢无察觉.
    ok = False
    err_msg = ""
    try:
        from atomic_io import append_jsonl
        append_jsonl(_SHADOW_LOG, entry)
        ok = True
    except Exception as e:
        err_msg = f"append_jsonl fail: {e}"
        try:
            _SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_SHADOW_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            ok = True
            err_msg = f"used fallback (no lock): {e}"
        except Exception as e2:
            err_msg = f"both fail: {e}; fallback: {e2}"
    _update_shadow_health(ok, err_msg)


_HEALTH_PATH = _SIG_DIR / "overnight_signal_health.json"


def _update_shadow_health(ok: bool, err_msg: str = "") -> None:
    """Track shadow log write health. If persistent fail (>24h no success),
    emit warning so Phase B backtest data loss isn't silent."""
    import time as _t
    now_ts = _t.time()
    state = {"last_success_ts": 0.0, "last_fail_ts": 0.0,
             "consecutive_fails": 0, "last_err": ""}
    try:
        if _HEALTH_PATH.exists():
            state.update(json.loads(_HEALTH_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    if ok:
        state["last_success_ts"] = now_ts
        state["consecutive_fails"] = 0
        state["last_err"] = ""
    else:
        state["last_fail_ts"] = now_ts
        state["consecutive_fails"] = int(state.get("consecutive_fails", 0)) + 1
        state["last_err"] = err_msg[:200]
        # 每 10 次失败或超过 24h 无成功时 warn 一次 (避免每 cycle 刷屏)
        age_since_success = now_ts - float(state.get("last_success_ts", 0) or 0)
        should_warn = (state["consecutive_fails"] % 10 == 1) or (age_since_success > 86400)
        if should_warn:
            try:
                import logging
                logging.getLogger("agents").warning(
                    f"[overnight_shadow_log] fail #{state['consecutive_fails']} "
                    f"(age_since_last_success={age_since_success/3600:.1f}h): {err_msg[:100]}"
                )
            except Exception:
                pass
    try:
        _HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HEALTH_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def shadow_log_health() -> dict:
    """供 dashboard / weekly review 读, 判断 shadow log 是否健康."""
    if not _HEALTH_PATH.exists():
        return {"status": "no_data", "healthy": True}   # 未跑过, 视为初始状态
    try:
        state = json.loads(_HEALTH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "corrupted_health_file", "healthy": False}
    import time as _t
    age = _t.time() - float(state.get("last_success_ts", 0) or 0)
    healthy = age < 86400 and state.get("consecutive_fails", 0) < 20
    return {
        "status": "ok" if healthy else "degraded",
        "healthy": healthy,
        "age_hours_since_success": round(age / 3600, 1),
        "consecutive_fails": state.get("consecutive_fails", 0),
        "last_err": state.get("last_err", ""),
    }


def _format_short(info: dict) -> str:
    """一句话总结, 供 CLI / log 输出."""
    if not info:
        return "(no data)"
    cls = info.get("classification", "neutral")
    meta = CLASSIFICATION_META.get(cls, {})
    icon = meta.get("icon", "⚪")
    zh = meta.get("zh", cls)
    hint = meta.get("action_hint", "")
    ticker = info.get("ticker", "?")
    return f"{icon} {ticker:<8} {zh:<12} → {hint}"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Overnight signal classifier")
    parser.add_argument("--ticker", help="单个 ticker (如 US.TQQQ)")
    parser.add_argument("--all", action="store_true",
                        help="扫所有 signals/*_latest.json")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    def _run_one(tk):
        r = classify_from_ticker(tk)
        if not r:
            print(f"  {tk}: 无 signal 文件")
            return
        if args.json:
            print(json.dumps({tk: r}, ensure_ascii=False, indent=2))
        else:
            print(_format_short(r))
            for w in r.get("why", []):
                print(f"    · {w}")

    if args.all:
        for p in sorted(_SIG_DIR.glob("*_latest.json")):
            stem = p.stem.replace("_latest", "")
            if stem in {"policy_toolkit"}:
                continue
            _run_one(stem)
    elif args.ticker:
        _run_one(args.ticker)
    else:
        parser.print_help()
