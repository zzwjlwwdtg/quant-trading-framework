"""
模块准确率回测 — 对每个标的 × 每个系统模块 × 每个持仓周期跑回测，
输出 `signals/module_accuracy.md` 供 AI prompt 注入参考。

回测对象（模块）：
  · decision_agent.action       (REDUCE / CAUTION / WATCH_BUY)
  · confluence (bull-bear)      (净多/净空/中性)

周期：1d / 5d / 10d / 20d
判定：方向命中（信号方向 == N 天后实际方向，收益 > 0 即 bullish）

回测窗口：默认 250 天日 K（yfinance）。
缓存：报告 mtime < 7 天则跳过。
"""
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

import numpy as np
from backtest_engine import (
    load_history, add_indicators, build_mkt, TICKERS
)
from config import SIGNALS_DIR
from decision_agent import get_decision
# WP04 (2026-09-20 followup): per-bar context 隔离历史 thesis/regime/calib.
# 修正上轮 commit 里错误声明 "modules_accuracy 无 thesis 暴露" (它 line 87
# 确实调 get_decision, thesis filter 会跑).
from decision_context import from_snapshot as _from_snapshot
from datetime import timezone as _tz
from trading_contracts import BULLISH_SIGNAL_ACTIONS, BEARISH_SIGNAL_ACTIONS

DAYS = 250
PERIODS = [1, 5, 10, 20]
FAKE_EVENTS = {"breaking_news": False, "days_to_event": 99,
               "risk_level": "moderate", "gold_bias": "neutral",
               "trump_signal": {"fallback": True}}
FAKE_MACRO = {"vix": 18, "fg_score": 55, "t10y2y": 0.4}

REPORT_PATH = Path(SIGNALS_DIR) / "module_accuracy.md"


# ── 模块判定（信号 → 期望方向） ─────────────────────────────────────────────
def _action_to_direction(action: str) -> str:
    """decision_agent.action → 期望方向"""
    if action in BULLISH_SIGNAL_ACTIONS:
        return "bullish"
    if action in BEARISH_SIGNAL_ACTIONS:
        return "bearish"
    return "skip"


def _confluence_to_direction(conf: dict) -> str:
    if not conf:
        return "skip"
    bull = conf.get("bull_count", 0)
    bear = conf.get("bear_count", 0)
    diff = bull - bear
    if diff >= 2:
        return "bullish"
    if diff <= -2:
        return "bearish"
    return "skip"


# ── 回测单标的 ─────────────────────────────────────────────────────────────
def backtest_ticker(tk: str) -> dict:
    """{module: {period: {hit, n, avg_ret, samples}}}"""
    full = "US." + tk
    df = add_indicators(load_history(tk, days=DAYS + 60))
    df = df.dropna(subset=["rsi_14", "ma20", "ma50", "bb_pct", "cci_20"])
    dates = list(df.index)[-DAYS:]

    # 初始化结果
    results = {
        "decision":   {p: {"hit": 0, "n": 0, "rets": []} for p in PERIODS},
        "confluence": {p: {"hit": 0, "n": 0, "rets": []} for p in PERIODS},
    }

    from confluence import get_confluence

    for i, d in enumerate(dates):
        # 检查最长周期 N 后是否还在数据范围内
        if i + max(PERIODS) >= len(dates):
            break
        row = df.loc[d]
        try:
            mkt = build_mkt(full, row)
            conf = get_confluence(mkt)
            # WP04: per-bar snapshot context, thesis/calibration empty → 消 look-ahead
            try:
                as_of = d.to_pydatetime().replace(tzinfo=_tz.utc)
            except Exception:
                as_of = None
            ctx = _from_snapshot(as_of=as_of, market=mkt,
                                  events=FAKE_EVENTS, macro=FAKE_MACRO,
                                  thesis_snapshot={}, board_regime="neutral",
                                  strategy_version="modules_accuracy") if as_of else None
            dec = get_decision(mkt, FAKE_EVENTS, FAKE_MACRO,
                                confluence=conf,
                                board_regime="neutral", context=ctx)
        except Exception:
            continue

        # 模块各自的方向预测
        directions = {
            "decision":   _action_to_direction(dec.get("action", "HOLD")),
            "confluence": _confluence_to_direction(conf),
        }

        today_close = float(df.loc[d, "close"])
        for period in PERIODS:
            future_idx = i + period
            if future_idx >= len(dates):
                continue
            future_close = float(df.loc[dates[future_idx], "close"])
            ret_pct = (future_close - today_close) / today_close * 100
            actual = "bullish" if ret_pct > 0 else "bearish"
            for mod, sig_dir in directions.items():
                if sig_dir == "skip":
                    continue
                cell = results[mod][period]
                cell["n"] += 1
                # 正向 hit：方向一致；样本收益按 sig_dir 方向计算
                if sig_dir == actual:
                    cell["hit"] += 1
                signed_ret = ret_pct if sig_dir == "bullish" else -ret_pct
                cell["rets"].append(signed_ret)

    # 聚合
    summary = {}
    for mod, periods_data in results.items():
        summary[mod] = {}
        for p, cell in periods_data.items():
            n = cell["n"]
            hit_rate = cell["hit"] / n * 100 if n else 0
            avg = float(np.mean(cell["rets"])) if cell["rets"] else 0
            summary[mod][p] = {"hit_rate": hit_rate, "n": n, "avg_ret": avg}
    return summary


# ── 报告生成 ───────────────────────────────────────────────────────────────
def format_report(all_results: dict) -> str:
    """生成 markdown 报告。"""
    lines = [
        "# 模块准确率回测",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}    回测窗口：过去 {DAYS} 个交易日    周期：1d / 5d / 10d / 20d",
        "",
        "判定：信号方向 vs N 天后实际收益方向（>0 多 / <0 空），HOLD/中性不计入。",
        "样本数 N < 30 准确率仅供参考；N ≥ 50 才有较强统计意义。",
        "",
        "## 模块释义",
        "",
        "- **decision**：[decision_agent.get_decision](f:/fsi-skills/agents/decision_agent.py) 的 action 字段（WATCH_BUY/BUY = bullish，REDUCE/SELL/CAUTION = bearish）",
        "- **confluence**：[confluence.get_confluence](f:/fsi-skills/agents/confluence.py) 的 (bull_count - bear_count) ≥+2 算 bullish, ≤-2 算 bearish",
        "",
        "## 已知补充数据（其它回测脚本）",
        "",
        "- **Trump signal aggregate_direction**：80.0% (12/15, 排除 neutral) — 见 [_backtest_trump_signal.py](f:/fsi-skills/agents/_backtest_trump_signal.py)",
        "- **regime 单一源**：legacy 与 new 决策 100% 一致（中性宏观下） — 见 [_backtest_regime_fix.py](f:/fsi-skills/agents/_backtest_regime_fix.py)",
        "- **新闻管道（CLI vs keyword）**：CLI 假阳 0，keyword 假阳 2 — 见 [_backtest_news_pipeline.py](f:/fsi-skills/agents/_backtest_news_pipeline.py)",
        "",
        "---",
        "",
    ]
    for tk in sorted(all_results.keys()):
        lines.append(f"## {tk}")
        lines.append("")
        for mod in ("decision", "confluence"):
            lines.append(f"### {mod}")
            lines.append("")
            lines.append("| 周期 | N | 准确率 | 平均收益(按信号方向) | 评级 |")
            lines.append("|---|---:|---:|---:|---|")
            mod_data = all_results[tk][mod]
            best_period = None
            best_rate = 0
            for p in PERIODS:
                cell = mod_data[p]
                n = cell["n"]
                rate = cell["hit_rate"]
                avg = cell["avg_ret"]
                if n >= 30 and rate > best_rate:
                    best_rate = rate
                    best_period = p
                # 评级
                if n < 20:
                    grade = "样本不足"
                elif rate >= 65:
                    grade = "**优秀**"
                elif rate >= 58:
                    grade = "良好"
                elif rate >= 52:
                    grade = "略优于随机"
                elif rate >= 48:
                    grade = "≈随机"
                else:
                    grade = "**反向**"
                lines.append(f"| {p}d | {n} | {rate:.1f}% | {avg:+.2f}% | {grade} |")
            if best_period and best_rate >= 55:
                lines.append("")
                lines.append(f"→ **最佳周期：{best_period}d** ({best_rate:.1f}%)")
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**使用建议**：")
    lines.append("")
    lines.append("- 看到信号矛盾时，按 **最佳周期 + 准确率高** 的模块为主")
    lines.append("- 以 decision / confluence 的最佳周期和样本量判断技术信号稳定性")
    lines.append("- N < 30 的数据不要作为决策依据，仅供参考")
    lines.append("- 评级\"反向\"的模块当反指标用（信号反过来跟）")
    return "\n".join(lines) + "\n"


def main():
    print("=" * 72)
    print(f"  模块准确率回测  | 标的: {TICKERS}  | 窗口: {DAYS}d")
    print(f"  周期: {PERIODS}")
    print("=" * 72)
    all_results = {}
    # backtest_engine.TICKERS = ["TQQQ", "SOXL", "GLD"]（不带 US. 前缀）
    for tk in TICKERS:
        print(f"\n[{tk}] 回测中...")
        try:
            res = backtest_ticker(tk)
            all_results[tk] = res
            # 摘要打印
            for mod in ("decision", "confluence"):
                line = f"  {mod:<11}: "
                for p in PERIODS:
                    c = res[mod][p]
                    line += f"{p}d={c['hit_rate']:.0f}%(N={c['n']}) "
                print(line)
        except Exception as e:
            print(f"  ERR: {e}")
            import traceback; traceback.print_exc()
    print()
    print(f"写报告: {REPORT_PATH}")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(format_report(all_results), encoding="utf-8")
    print(f"[done] 已写入 {REPORT_PATH}")


if __name__ == "__main__":
    main()
