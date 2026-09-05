"""top_picks.py — 每 cycle 从**所有 tracked ticker** 里排一个"今日值得关注"榜

设计目的:
    Dashboard 上散点式展示信号, 用户得手动 scan 找机会. 本模块聚合所有
    signals/*_latest.json + JP social reco snapshot, 应用 thesis 硬过滤,
    按 opportunity score 排序, 每 cycle 给 top-N ticker.

Score 计算 (透明可解释, 无 magic):
    action_score:
        BUY / WATCH_BUY / WATCH_BUY_PROBE  = +1.0 × conf
        HOLD_CROSS / HOLD                  = 0
        CAUTION                             = -0.3 × conf
        REDUCE / REDUCE_RISK               = -0.5 × conf
        SELL / SELL_ALL                    = -1.0 × conf
    thesis_bonus:
        whitelist                          = +2.0
        blacklist                          = **-999 (完全排除)**
        neither                            = 0
    regime_alignment (bull 类 regime 对 BUY 加分, defensive regime 对 BUY 减分):
        bull_trending / bull_pulling + BUY = +1.0
        risk_off / recession_risk + BUY    = -1.0
        crisis + 任何 BUY                  = -3.0
    extension_penalty (避免追高):
        cum_5d_pct > +8%                   = -1.5 (追高)
        cum_5d_pct < -8%                   = +0.5 (deep pullback 可能反弹)
    freshness_penalty:
        signal age > 48h                   = 排除

用途:
    - Dashboard 顶部 "🎯 今日值得关注 (top 10)"
    - ai_prompt.py 注入 morning/review 报告
    - CLI 命令行 quick lookup

CLI: python top_picks.py [--n 10] [--universe us|jp|all]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from config import SIGNALS_DIR
from thesis_config import is_ticker_blacklisted, is_ticker_whitelisted

_SIG_DIR = Path(SIGNALS_DIR)
_MAX_AGE_HOURS = 48

_BUY_ACTIONS = {"BUY", "WATCH_BUY", "WATCH_BUY_PROBE"}
_HOLD_ACTIONS = {"HOLD", "HOLD_CROSS"}
_CAUTION_ACTIONS = {"CAUTION"}
_REDUCE_ACTIONS = {"REDUCE", "REDUCE_RISK"}
_SELL_ACTIONS = {"SELL", "SELL_ALL"}


def _load_signal(path: Path) -> Optional[dict]:
    """读一个 _latest.json 并做 mtime 新鲜度检查."""
    try:
        age_h = (time.time() - path.stat().st_mtime) / 3600
        if age_h > _MAX_AGE_HOURS:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_signal_age_hours"] = round(age_h, 1)
        data["_source_file"] = path.name
        return data
    except Exception:
        return None


def _score_signal(sig: dict) -> dict:
    """给 signal 计算 opportunity score + 生成解释."""
    mkt = sig.get("market") or {}
    dec = sig.get("decision") or {}
    ticker = mkt.get("ticker") or dec.get("ticker") or ""
    action = (dec.get("action") or "").upper()
    conf = float(dec.get("confidence") or 0)
    regime = (dec.get("regime") or "").lower()

    # thesis 硬过滤
    is_black, black_reason = is_ticker_blacklisted(ticker)
    if is_black:
        return {
            "ticker": ticker,
            "score": -999.0,
            "why": [f"❌ thesis blacklist: {black_reason[:60]}"],
            "action": action,
            "confidence": conf,
            "regime": regime,
            "excluded": True,
        }

    is_white, _ = is_ticker_whitelisted(ticker)

    reasons = []
    score = 0.0

    # action_score
    if action in _BUY_ACTIONS:
        score += 1.0 * conf
        reasons.append(f"{action} conf={conf:.1f}")
    elif action in _CAUTION_ACTIONS:
        score -= 0.3 * conf
        reasons.append(f"CAUTION conf={conf:.1f} (-0.3×)")
    elif action in _REDUCE_ACTIONS:
        score -= 0.5 * conf
        reasons.append(f"REDUCE conf={conf:.1f} (-0.5×)")
    elif action in _SELL_ACTIONS:
        score -= 1.0 * conf
        reasons.append(f"SELL conf={conf:.1f}")

    # thesis whitelist bonus
    if is_white:
        score += 2.0
        reasons.append("✅ thesis whitelist +2")

    # regime alignment
    if action in _BUY_ACTIONS:
        if regime in ("bull_trending", "bull_pulling"):
            score += 1.0
            reasons.append(f"regime {regime} bull +1")
        elif regime in ("bull_extended", "overheated"):
            score -= 0.5
            reasons.append(f"regime {regime} 顶部 -0.5")
        elif regime in ("risk_off", "recession_risk"):
            score -= 1.0
            reasons.append(f"regime {regime} 防御 -1")
        elif regime == "crisis":
            score -= 3.0
            reasons.append(f"regime crisis -3")

    # extension penalty
    cum_5d = mkt.get("cum_5d_pct")
    if isinstance(cum_5d, (int, float)):
        if cum_5d > 8:
            score -= 1.5
            reasons.append(f"5d涨{cum_5d:+.1f}% 追高 -1.5")
        elif cum_5d < -8:
            score += 0.5
            reasons.append(f"5d跌{cum_5d:+.1f}% 深回调 +0.5")

    # RSI overbought penalty (若 available)
    rsi = mkt.get("rsi_14")
    if isinstance(rsi, (int, float)):
        if rsi > 78 and action in _BUY_ACTIONS:
            score -= 1.0
            reasons.append(f"RSI {rsi:.0f} 超买 -1")
        elif rsi < 30 and action in _BUY_ACTIONS:
            score += 0.5
            reasons.append(f"RSI {rsi:.0f} 超卖 +0.5")

    # Phase A: overnight 分类作为 display 字段, 不影响 score (weight=0).
    # Phase B backtest 通过后再加权重, 见 project_closed_loop_architecture.md.
    overnight_info = None
    try:
        from overnight_signal import classify as _classify_overnight
        overnight_info = _classify_overnight(
            pre_pct=mkt.get("pre_pct"),
            overnight_pct=mkt.get("overnight_pct"),
            after_pct=mkt.get("after_pct"),
            action=action,
            rsi=rsi,
            pre_vol=mkt.get("pre_volume"),
            pre_vol_avg=mkt.get("avg_volume_20"),
        )
    except Exception:
        pass

    return {
        "ticker": ticker,
        "score": round(score, 2),
        "why": reasons,
        "action": action,
        "confidence": conf,
        "regime": regime,
        "price": mkt.get("price"),
        "cum_5d": cum_5d,
        "rsi_14": rsi,
        "signal_age_hours": sig.get("_signal_age_hours"),
        "whitelist": is_white,
        "excluded": False,
        "overnight": overnight_info,   # {classification, why, confidence, has_data} or None
    }


def _load_all_signals(universe: str = "all") -> list[dict]:
    """从 signals/*_latest.json 读所有. universe 目前 filter 简单 (JP ticker 有 .T)"""
    out = []
    for p in sorted(_SIG_DIR.glob("*_latest.json")):
        sig = _load_signal(p)
        if not sig:
            continue
        # 跳过 policy_toolkit 之类非 ticker 文件
        stem = p.stem.replace("_latest", "")
        if stem in {"policy_toolkit"}:
            continue
        out.append(sig)
    # TODO: JP social_reco snapshot 合并 (若有对应结构)
    return out


def compute_top_picks(n: int = 10, universe: str = "all",
                      include_negative: bool = False) -> dict:
    """返 {ok, picks: [scored], excluded_count, generated_at, universe}."""
    sigs = _load_all_signals(universe)
    scored = [_score_signal(s) for s in sigs]

    # 排除 blacklisted
    valid = [s for s in scored if not s.get("excluded")]
    excluded_count = len(scored) - len(valid)

    # 排序
    valid.sort(key=lambda x: x["score"], reverse=True)

    if not include_negative:
        # 只保留 score > 0 的 (真"值得尝试")
        positive = [s for s in valid if s["score"] > 0]
        picks = positive[:n]
        rest_count = len(valid) - len(positive)
    else:
        picks = valid[:n]
        rest_count = max(0, len(valid) - n)

    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "universe": universe,
        "n_requested": n,
        "n_returned": len(picks),
        "n_universe": len(sigs),
        "n_excluded_by_thesis": excluded_count,
        "n_score_leq_zero": rest_count if not include_negative else 0,
        "picks": picks,
    }


def format_picks_text(result: dict, max_reasons: int = 3) -> str:
    """Human-readable 版本, 供 CLI / AI prompt 注入."""
    if not result.get("ok"):
        return "(top_picks 无数据)"
    picks = result.get("picks", [])
    lines = [f"🎯 Top {len(picks)} 值得尝试 (universe={result['n_universe']} tickers, "
             f"thesis 排除={result['n_excluded_by_thesis']}, score≤0 排除={result['n_score_leq_zero']}, "
             f"生成于 {result['generated_at']})"]
    if not picks:
        lines.append("  (无 score > 0 的候选. 全市场信号偏弱/防御.)")
        return "\n".join(lines)
    for i, p in enumerate(picks, 1):
        badge = " ⭐" if p.get("whitelist") else ""
        age_str = f" (age={p['signal_age_hours']:.0f}h)" if p.get("signal_age_hours") is not None else ""
        price_str = f" @ ${p['price']:.2f}" if p.get("price") else ""
        lines.append(f"  #{i} {p['ticker']:<10} score={p['score']:>+5.2f}{badge} "
                     f"[{p['action']}] regime={p['regime']}{price_str}{age_str}")
        for r in p["why"][:max_reasons]:
            lines.append(f"       · {r}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--universe", default="all", choices=["us", "jp", "all"])
    parser.add_argument("--include-negative", action="store_true",
                        help="包含 score ≤ 0 的候选 (默认只显示 >0)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()
    r = compute_top_picks(n=args.n, universe=args.universe,
                          include_negative=args.include_negative)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(format_picks_text(r))
