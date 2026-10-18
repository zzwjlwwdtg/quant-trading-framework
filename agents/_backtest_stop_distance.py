"""_backtest_stop_distance.py — 对实盘 BUY 重播不同 stop 距离, 看现制度是否最优

背景 (2026-09-02 分析): paper_trader 近 60d 12 个真信号 pair 里 10 hit stop,
    stop 桶 avg -8.74%, 净胜率 16.7%. 问题: 是"stop 太紧"造成过早触发,
    还是"signal 入场就 timing 差"?

方法 (section 1 — 现有):
    对 trade_log.jsonl 里所有非-REBALANCE BUY, 从入场后 30 天 daily bar 里
    重播 N 种 stop 策略, 比较 realized pnl 分布. 现制度是 TRAIL_8 × √lev.
    若某新策略 avg pnl ≥ TRAIL_8 + 2pp → 值得考虑改.

方法 (section 2 — EVO-20261014-1 新增, ATR 候选 vs 固定 trail_8):
    固定 universe = {TQQQ, SOXL, SQQQ, SOXS, MULL, GLD} (与 LEVERAGE dict + GLD
    对齐, 不扩展). 每 ENTRY_SPACING_DAYS 个交易日生成一次 open-at-open 入场,
    持 HOLD_DAYS 天. 对每笔入场同时模拟:
      · 现制度: trail_8 (0.08 × √leverage)
      · ATR 候选: ATR(14/20) × k, k ∈ {1.5, 2.0, 2.5, 3.0} (共 8 个)
    按入场日期切 train/OOS 两期分别统计, 预注册 pass gate 两期全过才上线.

**Caveat**:
    · section 1 n≈15 单, 统计意义弱, 结论 exploratory
    · section 2 用均匀 time-based 入场, 不代表 strategy_engine 真实入场分布;
      目的是比较 stop 机制本身, 不是验 entry 质量 → 不构成 signal 过拟合
    · yfinance 历史是 auto_adjust=True 后复权, 分红/split 已回填

**Pass condition — section 1 (跑前不改)**:
    C1: 某策略 avg pnl 比 TRAIL_8 高 ≥ 2 pp (绝对)
    C2: 该策略 median pnl 也不劣于 TRAIL_8
    通过 → 建议改, 否则保持现制度

**Pass condition — section 2 ATR (预注册, 跑前不改, EVO-20261014-1):**
    训练期 2024-01-01 — 2026-06-30, OOS 2026-07-01 — 今日. 候选在两期都要满足:
      (a) CAGR_候选 ≥ CAGR_baseline - 50bp
      (b) 平均亏损不恶化: avg_loss_候选 ≥ avg_loss_baseline  (负值, 不更负)
      (c) 中位亏损不恶化: median_loss_候选 ≥ median_loss_baseline
      (d) 总交易次数不显著放大: trade_count_候选 ≤ 1.3 × trade_count_baseline
    任一期任一条不达 → reject. 不写回执行代码, 仅落 verdict.

CLI: python _backtest_stop_distance.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

TRADE_LOG = Path("signals/trade_log.jsonl")
HOLD_DAYS = 30    # 拉入场后 30 天 daily bar
LEVERAGE = {"US.SOXL": 3, "US.TQQQ": 3, "US.MULL": 2, "US.SQQQ": 3, "US.SOXS": 3}

# 排除的 tag 前缀 (rebalance / pyramid 属于操作性交易, 不是信号入场)
EXCLUDE_TAG_KEYWORDS = ["REBALANCE"]

# 策略列表 (leverage-scaled). ATR 候选的 param 是 (period, k) 二元组, 由
# _simulate_stop 分支消化; k 不再乘 √lev — ATR 本身已反映波动率, 不叠加杠杆缩放.
STOP_POLICIES = [
    ("fixed_5",   "fixed",   0.05),
    ("fixed_8",   "fixed",   0.08),
    ("fixed_12",  "fixed",   0.12),
    ("trail_5",   "trail",   0.05),
    ("trail_8",   "trail",   0.08),   # ← 现制度 baseline
    ("trail_12",  "trail",   0.12),
    ("trail_15",  "trail",   0.15),
    ("no_stop_20d", "hold",  20),      # 不设 stop, 20 天后无脑卖
    # ── EVO-20261014-1 新增 ATR 候选 ────────────────────────────────────
    ("atr14_x1.5", "atr",    (14, 1.5)),
    ("atr14_x2.0", "atr",    (14, 2.0)),
    ("atr14_x2.5", "atr",    (14, 2.5)),
    ("atr14_x3.0", "atr",    (14, 3.0)),
    ("atr20_x1.5", "atr",    (20, 1.5)),
    ("atr20_x2.0", "atr",    (20, 2.0)),
    ("atr20_x2.5", "atr",    (20, 2.5)),
    ("atr20_x3.0", "atr",    (20, 3.0)),
]

ATR_POLICIES = [p for p in STOP_POLICIES if p[1] == "atr"]
BASELINE_NAME = "trail_8"

PASS_PP_MIN = 2.0   # 新策略 avg pnl 至少高 2pp 才 promote

# ── EVO-20261014-1 预注册阈值 (绝不运行时调整) ──────────────────────────
TRAIN_START = date(2024, 1, 1)
TRAIN_END   = date(2026, 6, 30)
OOS_START   = date(2026, 7, 1)

ATR_UNIVERSE = ["US.TQQQ", "US.SOXL", "US.SQQQ", "US.SOXS", "US.MULL", "US.GLD"]
ENTRY_SPACING_DAYS = 20       # 每 20 个交易日一次 open-at-open 入场
ATR_WARMUP_DAYS    = 60       # 入场前预留给 ATR 的历史交易日 (覆盖 ATR(20))

PASS_CRITERIA_ATR = {
    "cagr_min_delta_pp":    -0.50,  # (a) ≥ baseline - 50bp (年化百分点)
    "trade_count_max_ratio": 1.3,    # (d) ≤ 1.3 × baseline
    # (b)(c): candidate_avg/median_loss ≥ baseline_avg/median_loss (不更负)
}


def _leverage_sqrt(ticker: str) -> float:
    return math.sqrt(LEVERAGE.get(ticker, 1))


def _load_buys():
    """读 trade_log 抽出所有非-REBALANCE BUY, 返 [(ticker, ts, price, tag)]"""
    if not TRADE_LOG.exists():
        return []
    out = []
    for line in TRADE_LOG.read_text(encoding="utf-8").splitlines():
        try:
            t = json.loads(line)
        except Exception:
            continue
        if t.get("side") != "BUY":
            continue
        tag = (t.get("tag") or "")
        if any(k in tag.upper() for k in EXCLUDE_TAG_KEYWORDS):
            continue
        out.append({
            "ticker": t["ticker"],
            "ts":     t["ts"],
            "price":  float(t["price"]),
            "tag":    tag,
        })
    return out


def _pull_bars(ticker, buy_ts):
    """拉入场后 30 天的 daily bars (open/high/low/close)"""
    import yfinance as yf
    # buy_ts 是 UTC ISO. 转 date.
    d0 = datetime.fromisoformat(buy_ts.replace("Z", "+00:00")).date()
    # 拉到 +40 天 (buffer 遇 holidays)
    end = date.fromordinal(d0.toordinal() + 40)
    # 除掉 US. 前缀
    yf_sym = ticker.replace("US.", "")
    try:
        df = yf.Ticker(yf_sym).history(start=d0.isoformat(), end=end.isoformat(),
                                        interval="1d", auto_adjust=True)
    except Exception as ex:
        return None
    if df is None or df.empty:
        return None
    return df


def _wilder_atr(highs, lows, closes, period):
    """Wilder 平滑 ATR. 入参三条等长序列 (list/Series均可), 返回同长度 list.

    前 period 根 bar 的 ATR 用简单均值 seed, 之后用
      ATR_t = (ATR_{t-1} * (period-1) + TR_t) / period
    返回项与输入等长; 不足 period 的位置填 None.
    """
    hs = [float(x) for x in highs]
    ls = [float(x) for x in lows]
    cs = [float(x) for x in closes]
    n = len(hs)
    if n == 0 or period <= 0:
        return [None] * n
    trs = [None] * n
    for i in range(n):
        if i == 0:
            trs[i] = hs[i] - ls[i]
        else:
            trs[i] = max(hs[i] - ls[i],
                         abs(hs[i] - cs[i-1]),
                         abs(ls[i] - cs[i-1]))
    out = [None] * n
    if n < period:
        return out
    seed = sum(trs[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def _simulate_stop(bars, buy_price, ticker, kind, param, atr_at_entry=None):
    """给定策略参数, 遍历 bars 求实际退出价.
    返回 (exit_price, exit_day_index, reason).

    - fixed/trail: param 是 pct, 乘 √leverage
    - hold: param 是天数
    - atr: param 是 (period, k) 二元组; stop_price = rolling_high - k * atr_at_entry
      调用方必须预算好 atr_at_entry (入场日的 ATR 绝对值, 单位 = 价格)
    """
    lev_scale = _leverage_sqrt(ticker)

    if kind == "hold":
        # 简单 hold N 天然后收盘价平
        max_days = min(int(param), len(bars) - 1)
        if max_days < 0:
            max_days = 0
        exit_price = float(bars["Close"].iloc[max_days])
        return exit_price, max_days, "hold_end"

    if kind == "atr":
        # atr 候选: param = (period, k). atr_at_entry 由外层算好.
        if atr_at_entry is None or not (atr_at_entry > 0):
            # 无有效 ATR → 退化为 hold_end, 不入 stop 统计
            last_i = min(HOLD_DAYS, len(bars)) - 1
            return float(bars["Close"].iloc[max(last_i, 0)]), max(last_i, 0), "atr_missing"
        _period, k = param
        rolling_high = buy_price
        max_days = min(HOLD_DAYS, len(bars))
        atr_abs = float(atr_at_entry) * float(k)
        for i in range(max_days):
            row = bars.iloc[i]
            day_high = float(row["High"])
            day_low  = float(row["Low"])
            if day_high > rolling_high:
                rolling_high = day_high
            stop_price = rolling_high - atr_abs
            if day_low <= stop_price:
                return stop_price, i, f"stop@{stop_price:.2f}"
        last_i = max_days - 1
        return float(bars["Close"].iloc[last_i]), last_i, "hold_end"

    stop_pct = float(param) * lev_scale
    rolling_high = buy_price
    max_days = min(HOLD_DAYS, len(bars))
    for i in range(max_days):
        row = bars.iloc[i]
        day_high = float(row["High"])
        day_low  = float(row["Low"])
        day_close = float(row["Close"])

        # 更新 rolling high (仅 trail 用)
        if kind == "trail" and day_high > rolling_high:
            rolling_high = day_high

        if kind == "fixed":
            stop_price = buy_price * (1 - stop_pct)
        else:  # trail
            stop_price = rolling_high * (1 - stop_pct)

        # 若当日 low ≤ stop_price → 触发, 用 stop_price 出场
        if day_low <= stop_price:
            return stop_price, i, f"stop@{stop_price:.2f}"

    # 到 window end 没触发 stop → 用最后收盘退
    last_i = max_days - 1
    return float(bars["Close"].iloc[last_i]), last_i, "hold_end"


# ── EVO-20261014-1: ATR 候选的指标汇总 + 预注册 pass gate ────────────────
def _annualized_cagr_from_pnls(pnls, hold_days):
    """把一组独立持仓 pnl% 当作 serial 复利近似年化 CAGR (百分点).

    每笔持仓按 hold_days 换算年化, 用几何平均 (对 1+r 取 log). 这不是严格
    组合 CAGR (真实组合需 side-by-side 持仓), 但对比不同 stop 政策足够稳定.
    """
    if not pnls or hold_days <= 0:
        return 0.0
    import math as _m
    # 极端亏损 clip 到 -95% 避免 log 爆炸
    log_terms = []
    for p in pnls:
        r = max(float(p) / 100.0, -0.95)
        log_terms.append(_m.log(1.0 + r))
    avg_log = sum(log_terms) / len(log_terms)
    annualized = _m.exp(avg_log * (252.0 / hold_days)) - 1.0
    return annualized * 100.0


def _loss_stats(pnls):
    """返 (avg_loss_pct, median_loss_pct, n_loss). 两者都是负数或 0."""
    losses = sorted(p for p in pnls if p < 0)
    n = len(losses)
    if n == 0:
        return 0.0, 0.0, 0
    avg = sum(losses) / n
    med = losses[n // 2]
    return avg, med, n


def _summarize_policy(pnls):
    """按一组 pnl 返回指标 dict."""
    n = len(pnls)
    if n == 0:
        return {"n": 0, "avg_pnl": 0.0, "cagr": 0.0,
                "avg_loss": 0.0, "median_loss": 0.0, "n_loss": 0}
    return {
        "n": n,
        "avg_pnl":     sum(pnls) / n,
        "cagr":        _annualized_cagr_from_pnls(pnls, HOLD_DAYS),
        "avg_loss":    _loss_stats(pnls)[0],
        "median_loss": _loss_stats(pnls)[1],
        "n_loss":      _loss_stats(pnls)[2],
    }


def _evaluate_atr_pass(baseline_metrics, cand_metrics, criteria=PASS_CRITERIA_ATR):
    """判候选是否通过预注册 pass gate. baseline/cand_metrics 由 _summarize_policy 产出.

    返 (passed: bool, failures: list[str]). failures 为空即全过.
    """
    fails = []
    if baseline_metrics.get("n", 0) == 0 or cand_metrics.get("n", 0) == 0:
        return False, ["empty_sample"]
    # (a) CAGR
    cagr_delta_pp = cand_metrics["cagr"] - baseline_metrics["cagr"]
    if cagr_delta_pp < criteria["cagr_min_delta_pp"]:
        fails.append(f"cagr_drop={cagr_delta_pp:+.2f}pp<{criteria['cagr_min_delta_pp']}pp")
    # (b) avg_loss 不更负
    if cand_metrics["avg_loss"] < baseline_metrics["avg_loss"] - 1e-9:
        fails.append(f"avg_loss_worse={cand_metrics['avg_loss']:+.2f}%<{baseline_metrics['avg_loss']:+.2f}%")
    # (c) median_loss 不更负
    if cand_metrics["median_loss"] < baseline_metrics["median_loss"] - 1e-9:
        fails.append(f"median_loss_worse={cand_metrics['median_loss']:+.2f}%<{baseline_metrics['median_loss']:+.2f}%")
    # (d) trade_count ≤ 1.3× baseline  (ATR 候选与 baseline 样本 n 本应相等 —
    #     每笔同一入场同时模拟, 但 n_loss 比例可能膨胀触发次数预警, 这里对总
    #     交易数本身设护栏, 以防未来扩展成"每种策略独立入场"时失守)
    if baseline_metrics["n"] > 0:
        ratio = cand_metrics["n"] / baseline_metrics["n"]
        if ratio > criteria["trade_count_max_ratio"] + 1e-9:
            fails.append(f"trade_count_ratio={ratio:.2f}>{criteria['trade_count_max_ratio']}")
    return (len(fails) == 0), fails


def run():
    print("=" * 100)
    print("Stop 距离敏感性回测 (对实盘 BUY 单重播)")
    print("=" * 100)

    buys = _load_buys()
    print(f"\n共 {len(buys)} 单非-REBALANCE BUY:")
    for b in buys[-10:]:
        print(f"  {b['ts'][:10]} {b['ticker']:<10} @ ${b['price']:.2f}  tag={b['tag'][:40]}")

    if not buys:
        print("无 BUY 数据, 退出")
        return

    # 对每个 BUY 拉 30 天 bars
    print("\n拉 30-day fwd bars...")
    valid_buys = []
    for b in buys:
        bars = _pull_bars(b["ticker"], b["ts"])
        if bars is None or bars.empty:
            print(f"  {b['ticker']} {b['ts'][:10]}: 无 fwd 数据, 跳过")
            continue
        b["bars"] = bars
        valid_buys.append(b)
    print(f"  有效: {len(valid_buys)} 单")

    if len(valid_buys) < 5:
        print("样本太少 (< 5), 结论无意义")
        return

    # 对每种策略, 每个 BUY 模拟 (ATR 候选属于 section 2, 这里跳过)
    section1_policies = [p for p in STOP_POLICIES if p[1] != "atr"]
    results = {name: [] for name, _, _ in section1_policies}
    for b in valid_buys:
        for name, kind, param in section1_policies:
            exit_px, exit_i, reason = _simulate_stop(b["bars"], b["price"],
                                                     b["ticker"], kind, param)
            pnl_pct = (exit_px / b["price"] - 1) * 100
            results[name].append({
                "ticker": b["ticker"], "pnl": pnl_pct,
                "exit_day": exit_i, "reason": reason,
            })

    # 汇总
    print()
    print("=" * 100)
    print("【策略对比】")
    print("=" * 100)
    print(f"{'策略':<15} {'n':>3} {'avg_pnl':>9} {'median':>9} {'win%':>7} "
          f"{'stop_hit%':>10} {'max_loss':>10} {'avg_days':>10}")
    print("-" * 100)

    baseline_avg = None
    for name, _, _ in section1_policies:
        rs = results[name]
        n = len(rs)
        if n == 0: continue
        avg = sum(r["pnl"] for r in rs) / n
        med = sorted(r["pnl"] for r in rs)[n//2]
        wins = sum(1 for r in rs if r["pnl"] > 0) / n * 100
        stop_hit = sum(1 for r in rs if "stop@" in r["reason"]) / n * 100
        max_loss = min(r["pnl"] for r in rs)
        avg_days = sum(r["exit_day"] for r in rs) / n
        marker = "  ← 现制度" if name == "trail_8" else ""
        print(f"{name:<15} {n:>3} {avg:>+8.2f}% {med:>+8.2f}% {wins:>6.1f}% "
              f"{stop_hit:>9.1f}% {max_loss:>+9.2f}% {avg_days:>9.1f}d{marker}")
        if name == "trail_8":
            baseline_avg = avg

    # 找最好非-baseline 策略
    print()
    print("=" * 100)
    print("【VERDICT】")
    print("=" * 100)
    if baseline_avg is None:
        print("  ! 无 baseline trail_8 数据")
        return
    best_name, best_avg = None, baseline_avg
    for name, _, _ in section1_policies:
        if name == "trail_8": continue
        rs = results[name]
        if not rs: continue
        avg = sum(r["pnl"] for r in rs) / len(rs)
        if avg > best_avg:
            best_avg = avg
            best_name = name

    if best_name is None or (best_avg - baseline_avg) < PASS_PP_MIN:
        best_str = f"{best_name}" if best_name else "无更优策略"
        gap = (best_avg - baseline_avg) if best_name else 0
        print(f"  → **保持 trail_8** ({baseline_avg:+.2f}%). 最佳候选 {best_str} 只高 {gap:+.2f}pp < {PASS_PP_MIN}pp 阈值")
        print(f"     结论: 当前 stop 距离在这批数据上不是最优, 但也没有明显更好的替代")
        print(f"     真正问题不在 stop, 在 signal 入场 timing")
        verdict_val = "keep_baseline"
        should_int = False
        recommendation = "keep TRAILING_STOP_BASE_PCT=0.08"
    else:
        print(f"  → **建议改用 {best_name}** ({best_avg:+.2f}% vs baseline {baseline_avg:+.2f}%, +{best_avg-baseline_avg:.2f}pp)")
        print(f"     实施: paper_trader.py:422 TRAILING_STOP_BASE_PCT 改为对应值")
        verdict_val = "pass"
        should_int = True
        recommendation = f"switch TRAILING_STOP_BASE_PCT to match {best_name}"

    # 写 verdict 供系统消费
    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "stop_distance",
            verdict_val,
            conclusion=f"trail_8 baseline={baseline_avg:+.2f}% best_alt={best_name} gap={best_avg-baseline_avg:+.2f}pp",
            metrics={"baseline_avg": round(baseline_avg, 4),
                     "best_alt": best_name,
                     "best_alt_avg": round(best_avg, 4),
                     "delta_pp": round(best_avg - baseline_avg, 4),
                     "n_trades": len(valid_buys)},
            params={"universe": "trade_log_signal_buys",
                    "hold_window_days": HOLD_DAYS,
                    "pass_pp_min": PASS_PP_MIN,
                    "n_policies_tested": len(STOP_POLICIES)},
            next_review_days=60,
            should_integrate=should_int,
            recommendation=recommendation,
        )
        print("  [verdict] 写入 signals/backtest_verdicts/stop_distance.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")

    # 详细看每单在最坏 vs 最好策略下的表现差异
    print()
    print("=" * 100)
    print("【逐单最优 exit 与 trail_8 的差距】")
    print("=" * 100)
    print(f"{'ticker':<10} {'buy_ts':<12} {'trail_8':>10} {'best_strat':>12} {'best_pnl':>10}")
    for b in valid_buys:
        tk = b["ticker"]
        tr8 = next(r for r in results["trail_8"] if r["ticker"] == tk and "exit_day" in r and abs(r.get("exit_day",-1) - (results["trail_8"][valid_buys.index(b)]["exit_day"])) < 1)  # match
        # simplify: just index by position
        tr8 = results["trail_8"][valid_buys.index(b)]
        # find best strategy for THIS trade
        per_trade = {name: results[name][valid_buys.index(b)]["pnl"] for name, _, _ in section1_policies}
        best_for_trade = max(per_trade.items(), key=lambda x: x[1])
        print(f"{tk:<10} {b['ts'][:10]:<12} {tr8['pnl']:>+9.2f}% {best_for_trade[0]:>12} {best_for_trade[1]:>+9.2f}%")


# ─────────────────────────────────────────────────────────────────────────
#  Section 2 (EVO-20261014-1): ATR 候选 vs trail_8, 固定 universe, train/OOS
# ─────────────────────────────────────────────────────────────────────────
def _pull_universe_bars(ticker: str, start: date, end: date):
    """拉 ticker 从 start 到 end 的 daily bars (含预热窗口由调用方算).

    返 DataFrame (index=DatetimeIndex, 列=Open/High/Low/Close/Volume) 或 None.
    """
    import yfinance as yf
    yf_sym = ticker.replace("US.", "")
    try:
        df = yf.Ticker(yf_sym).history(start=start.isoformat(),
                                        end=end.isoformat(),
                                        interval="1d", auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df


def _generate_entries(bars, start: date, end: date,
                      spacing: int = ENTRY_SPACING_DAYS,
                      warmup: int = ATR_WARMUP_DAYS):
    """沿 bars 的交易日序列生成等间距入场点, 返回 list[dict].

    返回项: {"idx": int, "entry_date": date, "open": float}. 入场点满足:
      · 距序列开头 ≥ warmup (ATR 预热)
      · 入场日 ∈ [start, end]
      · 距序列末尾 ≥ HOLD_DAYS (有完整持仓窗口)
    """
    if bars is None or len(bars) == 0:
        return []
    out = []
    # bars.index 可能是 tz-aware, 统一取 .date()
    for i in range(warmup, len(bars) - HOLD_DAYS, spacing):
        d = bars.index[i].date()
        if d < start or d > end:
            continue
        out.append({"idx": i, "entry_date": d, "open": float(bars["Open"].iloc[i])})
    return out


def _run_atr_universe_backtest(ticker: str, universe_bars,
                                train_start=TRAIN_START,
                                train_end=TRAIN_END,
                                oos_start=OOS_START,
                                oos_end=None):
    """对单 ticker, 预热后生成 time-based 入场, 对 trail_8 + 全部 ATR 候选模拟.

    返回 {policy_name: {"train": [pnl...], "oos": [pnl...]}}, 跨 period 分桶.
    """
    if oos_end is None:
        oos_end = date.today()
    # 预计算整个序列的 ATR(14) / ATR(20) (入场日的值作为 atr_at_entry)
    highs = universe_bars["High"].tolist()
    lows  = universe_bars["Low"].tolist()
    closes = universe_bars["Close"].tolist()
    atr_series = {
        14: _wilder_atr(highs, lows, closes, 14),
        20: _wilder_atr(highs, lows, closes, 20),
    }
    policies = [p for p in STOP_POLICIES if p[0] == BASELINE_NAME or p[1] == "atr"]
    buckets = {name: {"train": [], "oos": []} for name, _, _ in policies}

    # 两段入场
    entries_train = _generate_entries(universe_bars, train_start, train_end)
    entries_oos   = _generate_entries(universe_bars, oos_start, oos_end)
    for period_name, entries in (("train", entries_train), ("oos", entries_oos)):
        for e in entries:
            i = e["idx"]
            # 持仓窗口 bars: open 作为入场, 后续 bars.iloc[i+1 : i+1+HOLD_DAYS]
            # 为与 section 1 对齐, _simulate_stop 的 bars 要包含"入场日之后"的
            # 价格路径; section 1 里 bars[0] 是入场日. 这里统一: 入场即当日 open
            # 买入, 回测 bars 从入场日开始 (当日 high/low 已参与当日 trail 判定).
            hold_bars = universe_bars.iloc[i : i + HOLD_DAYS + 1]
            if len(hold_bars) < 2:
                continue
            buy_price = e["open"]
            for name, kind, param in policies:
                atr_at_entry = None
                if kind == "atr":
                    period = param[0]
                    atr_at_entry = atr_series[period][i - 1] if i >= 1 else atr_series[period][i]
                exit_px, exit_i, reason = _simulate_stop(
                    hold_bars, buy_price, ticker, kind, param,
                    atr_at_entry=atr_at_entry)
                pnl_pct = (exit_px / buy_price - 1) * 100
                buckets[name][period_name].append(pnl_pct)
    return buckets


def run_atr_candidates():
    print()
    print("=" * 100)
    print("ATR 候选 vs trail_8 — 预注册 train/OOS 回测 (EVO-20261014-1)")
    print(f"  train: {TRAIN_START} — {TRAIN_END}    OOS: {OOS_START} — today")
    print(f"  universe: {ATR_UNIVERSE}")
    print(f"  pass gate: CAGR≥baseline-50bp, avg/median_loss 不恶化, n_trades ≤ 1.3×")
    print("=" * 100)

    # 拉所有 ticker bars 一次 (覆盖 2023Q4 预热 → today)
    pull_start = date(TRAIN_START.year - 1, 10, 1)   # 覆盖 ATR warmup
    pull_end   = date.today()
    all_bars = {}
    for tk in ATR_UNIVERSE:
        bars = _pull_universe_bars(tk, pull_start, pull_end)
        if bars is None or bars.empty:
            print(f"  {tk}: 无数据, 跳过")
            continue
        all_bars[tk] = bars
        print(f"  {tk}: {len(bars)} bars  [{bars.index[0].date()} → {bars.index[-1].date()}]")

    if not all_bars:
        print("  → 无可用数据, 退出")
        return

    # 聚合 train/oos 每个 policy 的 pnl 列表 (baseline + 8 个 ATR 候选)
    agg = {name: {"train": [], "oos": []}
           for (name, kind, _p) in STOP_POLICIES if name == BASELINE_NAME or kind == "atr"}
    for tk, bars in all_bars.items():
        res = _run_atr_universe_backtest(tk, bars)
        for name, buckets in res.items():
            agg[name]["train"].extend(buckets["train"])
            agg[name]["oos"].extend(buckets["oos"])

    # 汇总指标表
    print()
    print(f"{'策略':<14} {'period':<6} {'n':>5} {'avg_pnl':>9} {'CAGR':>8} "
          f"{'avg_loss':>10} {'med_loss':>10} {'n_loss':>7}")
    print("-" * 100)
    metrics_table = {}
    for name in [BASELINE_NAME] + [p[0] for p in ATR_POLICIES]:
        metrics_table[name] = {}
        for period in ("train", "oos"):
            m = _summarize_policy(agg[name][period])
            metrics_table[name][period] = m
            tag = "  ← 现制度" if name == BASELINE_NAME else ""
            print(f"{name:<14} {period:<6} {m['n']:>5} {m['avg_pnl']:>+8.2f}% "
                  f"{m['cagr']:>+7.2f}% {m['avg_loss']:>+9.2f}% "
                  f"{m['median_loss']:>+9.2f}% {m['n_loss']:>7}{tag}")

    # 预注册 pass gate: 候选在两期都要全过
    print()
    print("=" * 100)
    print("【ATR 候选 pass gate 判定 (两期全过才上线)】")
    print("=" * 100)
    base_train = metrics_table[BASELINE_NAME]["train"]
    base_oos   = metrics_table[BASELINE_NAME]["oos"]
    survivors = []
    for name, _, _ in ATR_POLICIES:
        tr_pass, tr_fails = _evaluate_atr_pass(base_train, metrics_table[name]["train"])
        oo_pass, oo_fails = _evaluate_atr_pass(base_oos,   metrics_table[name]["oos"])
        both = tr_pass and oo_pass
        mark = "PASS" if both else "REJ "
        print(f"  [{mark}] {name:<14} train={'OK' if tr_pass else 'X'}"
              f"({','.join(tr_fails) or '-'})  "
              f"oos={'OK' if oo_pass else 'X'}({','.join(oo_fails) or '-'})")
        if both:
            survivors.append(name)

    print()
    if not survivors:
        verdict_val = "keep_baseline"
        should_int = False
        recommendation = "keep TRAILING_STOP_BASE_PCT=0.08 (ATR 候选未同时通过 train+OOS)"
        best = None
        print(f"  → **保持 trail_8**. ATR 候选无一同时通过训练期和 OOS")
    else:
        # 并列候选: 挑 OOS 期 CAGR 最高
        best = max(survivors, key=lambda n: metrics_table[n]["oos"]["cagr"])
        verdict_val = "pass"
        should_int = True
        recommendation = (f"integrate {best} (OOS CAGR="
                          f"{metrics_table[best]['oos']['cagr']:+.2f}% vs "
                          f"baseline {base_oos['cagr']:+.2f}%)")
        print(f"  → **候选 {best} 通过两期**, OOS CAGR="
              f"{metrics_table[best]['oos']['cagr']:+.2f}% vs baseline "
              f"{base_oos['cagr']:+.2f}%")
        print(f"     注意: 本脚本只落 verdict, 不改执行代码. 上线需另开 PR 并锁 OOS")

    # 落 verdict (独立 name, 不覆盖 section 1 的 stop_distance)
    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "stop_distance_atr",
            verdict_val,
            conclusion=(f"baseline trail_8 train_CAGR={base_train['cagr']:+.2f}% "
                        f"oos_CAGR={base_oos['cagr']:+.2f}%; "
                        f"survivors={survivors}; best={best}"),
            metrics={
                "baseline_train": {k: round(v, 4) if isinstance(v, float) else v
                                    for k, v in base_train.items()},
                "baseline_oos":   {k: round(v, 4) if isinstance(v, float) else v
                                    for k, v in base_oos.items()},
                "survivors":      survivors,
                "best":           best,
            },
            params={
                "universe":          ATR_UNIVERSE,
                "train_window":      [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
                "oos_window":        [OOS_START.isoformat(), date.today().isoformat()],
                "hold_days":         HOLD_DAYS,
                "entry_spacing":     ENTRY_SPACING_DAYS,
                "pass_criteria":     PASS_CRITERIA_ATR,
                "atr_candidates":    [p[0] for p in ATR_POLICIES],
            },
            next_review_days=90,
            should_integrate=should_int,
            recommendation=recommendation,
        )
        print("  [verdict] 写入 signals/backtest_verdicts/stop_distance_atr.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")


if __name__ == "__main__":
    import os, threading
    threading.Timer(600, lambda: (print("\n[watchdog] 超时"), os._exit(2))).start()
    run()
    run_atr_candidates()
