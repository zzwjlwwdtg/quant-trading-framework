"""capital_flow.py — 大单/散户资金流向 unified adapter.

美股：moomoo OpenD `get_capital_distribution` — 实时（≤15min 延迟）super/big/mid/small
      净流入分解。需要 moomoo 已订阅相应市场行情。

日股：yfinance 5min K 线 volume anomaly proxy — moomoo JP 需付费订阅，暂用
      成交量代理：某 5min bar 成交量 > 20 日同时间段均值 × 3 → 可疑大单执行。
      T+1 用 JPX ToSTNeT 印证（jp_tostnet.py）。

统一返回结构：
{
  "ticker":       "NVDA",
  "asof":         "2026-08-05T15:20 ET",
  "source":       "moomoo" | "yfinance_intraday" | "unavailable",
  "flow": {          # 有 source=moomoo 时才有完整字段
    "super_net":  270_000_000.0,   # USD
    "big_net":    ...,
    "mid_net":    ...,
    "small_net":  ...,
    "total_net":  ...,
    "smart_pct":  0.42,            # (super_net+big_net)/total_turnover
  },
  "anomaly": {                     # 通用异常标记（两种 source 都有）
    "detected":   True,
    "reason":     "10:38 5min bar 3.2× avg volume",
    "score":      0.85,            # 0-1
    "peak_bars":  [ {time, vol, ratio, price}, ... ]   # 仅 JP intraday 有
  }
}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

# JP 市场对 US.xxx 前缀 tickers 用 moomoo；对 JP.xxxx / xxxx.T 用 yfinance intraday
_JP_TICKER_SUFFIX = ".T"


def _is_jp(ticker: str) -> bool:
    t = (ticker or "").upper().strip()
    return t.endswith(_JP_TICKER_SUFFIX) or t.startswith("JP.")


def _to_yf_symbol(ticker: str) -> str:
    """JP → 6981.T；US.NVDA → NVDA；NVDA → NVDA."""
    t = (ticker or "").upper().strip()
    if t.startswith("JP.") and not t.endswith(".T"):
        return f"{t[3:]}.T"
    if t.startswith("US."):
        return t[3:]
    return t


# ────────────── US 分支 (moomoo) ──────────────

def _fetch_us_flow(ticker: str) -> Optional[dict]:
    try:
        from moomoo_data import get_capital_distribution_via_openD
    except Exception:
        return None
    raw = get_capital_distribution_via_openD(ticker)
    if not raw:
        return None
    total_turnover = raw["total_in"] + raw["total_out"]
    smart_net = raw["super_net"] + raw["big_net"]
    result = {
        "super_net": raw["super_net"],
        "big_net":   raw["big_net"],
        "mid_net":   raw["mid_net"],
        "small_net": raw["small_net"],
        "total_net": raw["net_total"],
        "smart_pct": (smart_net / total_turnover) if total_turnover > 0 else 0,
    }
    # anomaly: super+big net 占比 >±15% 且绝对值 >$50M → 强信号
    smart_pct = float(result["smart_pct"])
    detected = bool(abs(smart_pct) >= 0.15 and abs(smart_net) >= 5e7)
    reason = ""
    if detected:
        direction = "净流入" if smart_net > 0 else "净流出"
        reason = f"主力（super+big）{direction} ${smart_net/1e6:+.1f}M（占总成交额 {smart_pct*100:+.1f}%）"
    return {
        "flow":    {k: float(v) for k, v in result.items()},
        "anomaly": {
            "detected": detected,
            "reason":   reason,
            "score":    float(min(abs(smart_pct) / 0.30, 1.0)) if detected else 0.0,
        },
        "asof":    raw.get("update_time"),
        "source":  "moomoo",
    }


# ────────────── JP 分支 (yfinance intraday volume) ──────────────

def _fetch_jp_flow(ticker: str) -> Optional[dict]:
    """yfinance 5min K → 检测量能异常 bar。"""
    try:
        import yfinance as yf
    except Exception:
        return None
    sym = _to_yf_symbol(ticker)
    try:
        # 拉最近 60 天 5min（yfinance 限制 60 天），需要 20 日基线
        df = yf.Ticker(sym).history(period="60d", interval="5m",
                                    auto_adjust=False)
        if df is None or df.empty:
            return None
    except Exception:
        return None

    import pandas as pd
    df = df.copy()
    # 时区归一化（yfinance 返 Tokyo 或 UTC 混，转 Asia/Tokyo）
    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert("Asia/Tokyo")
    except Exception:
        pass

    df["time_of_day"] = df.index.strftime("%H:%M")
    df["date"] = df.index.strftime("%Y-%m-%d")

    # 计算每个时段过去 20 交易日的平均 volume（同时段对比，剔除盘前盘后）
    baseline = df.groupby("time_of_day")["Volume"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=5).mean()
    )
    df["baseline_vol"] = baseline
    df["vol_ratio"] = df["Volume"] / df["baseline_vol"].replace(0, float("nan"))

    # 计算 daily 均量（用来算每天的 day_ratio）
    daily_totals = df.groupby("date")["Volume"].sum()
    daily_avg = float(daily_totals.tail(20).mean()) if len(daily_totals) else 0

    # === 两级检测 ===
    # Tier 1 · 单笔大单（peak_bars）：ratio ≥5× AND ¥1億 (小盘 spike)
    #                            OR value ≥¥30億 (大盘绝对量，不管 ratio)
    #                            —— catch 未拆的粗放大单，MURATA 那种 ¥150亿单笔
    # Tier 2 · 拆单模式（slices）：连续 ≥3 根同方向 elevated bar (2×+ ¥0.2億+, 累计 ¥1.5億+)
    #                             OR 连续 2 根同方向巨型 bar (累计 ¥30億+)
    #                             —— catch VWAP/TWAP algo 拆单
    RATIO_MIN           = 5.0    # 单 bar 相对倍数
    VALUE_OKU_MIN       = 1.0    # 单 bar 最低金额
    ABS_HUGE_OKU_MIN    = 30.0   # 绝对巨量 tier: ≥¥30億 (~$200M) 无视 ratio
    # Slice
    SLICE_RATIO_MIN     = 2.0
    SLICE_VALUE_MIN_OKU = 0.2
    SLICE_MIN_BARS      = 3
    SLICE_TOTAL_OKU_MIN = 1.5    # 3+ bars 累计门槛
    SLICE_2BAR_HUGE_OKU = 30.0   # 2-bar tier: 累计 ≥¥30億 也算

    def _analyze_one_day(day_df, date_str: str) -> dict:
        """给定一天的 5min bars → 该日的 anomaly + aggregate summary。"""
        # 两 tier 初筛：(ratio ≥5× AND vol ≥10K) OR (value ≥¥30億 无视 ratio)
        # 大盘票（MURATA/TDK）2× ratio 但 ¥100亿+ 是妥妥机构级
        day_df = day_df.copy()
        day_df["_value_oku"] = day_df["Volume"] * day_df["Close"] / 1e8
        raw_bars = day_df[
            ((day_df["vol_ratio"] >= RATIO_MIN) & (day_df["Volume"] >= 10_000))
            | (day_df["_value_oku"] >= ABS_HUGE_OKU_MIN)
        ].sort_values("_value_oku", ascending=False)   # 按绝对规模排（不是 ratio）

        peak_bars = []
        for ts, row in raw_bars.head(8).iterrows():
            o = float(row["Open"])
            c = float(row["Close"])
            chg_pct = (c - o) / o * 100 if o > 0 else 0
            if chg_pct > 0.10:
                direction = "buy"
            elif chg_pct < -0.10:
                direction = "sell"
            else:
                direction = "flat"
            vol = int(row["Volume"])
            value_oku = round(float(row["_value_oku"]), 2)
            # 至少满足 VALUE_OKU_MIN (¥1亿) 或 ABS_HUGE_OKU_MIN 之一
            if value_oku < VALUE_OKU_MIN and value_oku < ABS_HUGE_OKU_MIN:
                continue
            peak_bars.append({
                "time":      ts.strftime("%H:%M"),
                "vol":       vol,
                "ratio":     round(float(row["vol_ratio"]), 2),
                "open":      round(o, 2),
                "price":     round(c, 2),
                "chg_pct":   round(chg_pct, 2),
                "direction": direction,
                "value_oku": value_oku,
                "date":      date_str,
                "tier":      "huge_abs" if value_oku >= ABS_HUGE_OKU_MIN else "ratio_spike",
            })

        buy_shares  = sum(b["vol"] for b in peak_bars if b["direction"] == "buy")
        sell_shares = sum(b["vol"] for b in peak_bars if b["direction"] == "sell")
        buy_value  = round(sum(b["value_oku"] for b in peak_bars if b["direction"] == "buy"), 2)
        sell_value = round(sum(b["value_oku"] for b in peak_bars if b["direction"] == "sell"), 2)
        net_bias = "neutral"
        if buy_shares >= sell_shares * 2 and buy_shares >= 20_000:
            net_bias = "buy"
        elif sell_shares >= buy_shares * 2 and sell_shares >= 20_000:
            net_bias = "sell"

        # 拆单模式检测（VWAP/TWAP algo trace）：连续 N 根同方向 elevated bar
        # 累计规模 ≥¥1.5億 才认真 —— 单笔藏得住但累积藏不住
        slices = []
        current_streak: list[dict] = []
        def _finalize_streak(streak):
            n = len(streak)
            if n < 2:
                return None
            total_v = sum(b["value_oku"] for b in streak)
            # 3+ bars: 累计 ≥¥1.5億
            # 2 bars: 只在累计 ≥¥30億 才算（大盘级机构 VWAP 拆 2 段）
            if n >= SLICE_MIN_BARS:
                if total_v < SLICE_TOTAL_OKU_MIN:
                    return None
                tier = "multi_bar"
            else:  # n == 2
                if total_v < SLICE_2BAR_HUGE_OKU:
                    return None
                tier = "huge_2bar"
            avg_ratio = sum(b["ratio"] for b in streak) / n
            return {
                "start":       streak[0]["time"],
                "end":         streak[-1]["time"],
                "direction":   streak[0]["direction"],
                "n_bars":      n,
                "total_value_oku": round(total_v, 2),
                "total_shares":    sum(b["vol"] for b in streak),
                "avg_ratio":       round(avg_ratio, 2),
                "duration_min":    n * 5,
                "avg_price":       round(sum(b["price"] * b["vol"] for b in streak) / max(sum(b["vol"] for b in streak), 1), 2),
                "tier":            tier,
            }

        for ts, row in day_df.iterrows():
            o = float(row["Open"])
            c = float(row["Close"])
            if o <= 0:
                continue
            chg = (c - o) / o * 100
            direction = "buy" if chg > 0.05 else ("sell" if chg < -0.05 else None)
            vol = int(row["Volume"])
            value_oku = vol * c / 1e8
            ratio = float(row["vol_ratio"]) if row["vol_ratio"] == row["vol_ratio"] else 0
            is_elevated = ratio >= SLICE_RATIO_MIN and value_oku >= SLICE_VALUE_MIN_OKU

            # Extending same-direction streak
            if is_elevated and direction and (
                not current_streak or current_streak[-1]["direction"] == direction
            ):
                current_streak.append({
                    "time":      ts.strftime("%H:%M"),
                    "direction": direction,
                    "vol":       vol,
                    "value_oku": round(value_oku, 2),
                    "ratio":     round(ratio, 2),
                    "price":     round(c, 2),
                })
            else:
                # Streak broken → finalize
                s = _finalize_streak(current_streak)
                if s:
                    slices.append(s)
                # Start new streak if this bar itself qualifies
                current_streak = [{
                    "time": ts.strftime("%H:%M"),
                    "direction": direction,
                    "vol": vol,
                    "value_oku": round(value_oku, 2),
                    "ratio": round(ratio, 2),
                    "price": round(c, 2),
                }] if (is_elevated and direction) else []

        # Finalize trailing streak
        s = _finalize_streak(current_streak)
        if s:
            slices.append(s)

        # 该日全部 bar 的 tick-rule 净方向（不只 anomaly，用于对齐"主力流出"这种定义）
        day_up_bars   = day_df[day_df["Close"] > day_df["Open"]]
        day_down_bars = day_df[day_df["Close"] < day_df["Open"]]
        full_up_value   = round(float((day_up_bars["Volume"]   * day_up_bars["Close"]).sum())   / 1e8, 2)
        full_down_value = round(float((day_down_bars["Volume"] * day_down_bars["Close"]).sum()) / 1e8, 2)
        full_up_shares   = int(day_up_bars["Volume"].sum())
        full_down_shares = int(day_down_bars["Volume"].sum())
        full_bias = "neutral"
        if full_up_value >= full_down_value * 1.5:
            full_bias = "buy"
        elif full_down_value >= full_up_value * 1.5:
            full_bias = "sell"

        # 日 K 涨跌
        day_open  = float(day_df.iloc[0]["Open"])
        day_close = float(day_df.iloc[-1]["Close"])
        day_chg_pct = round((day_close / day_open - 1) * 100, 2) if day_open > 0 else 0

        day_total_vol = float(day_df["Volume"].sum())
        day_ratio = float(round(day_total_vol / daily_avg, 2)) if daily_avg > 0 else 0

        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            date_short = f"{d.month}/{d.day}"
        except Exception:
            date_short = date_str

        return {
            "trading_date":   date_str,
            "date_short":     date_short,
            "peak_bars":      peak_bars,
            "slices":         slices,           # 拆单模式（VWAP/TWAP algo trace）
            "day_ratio":      day_ratio,
            "day_chg_pct":    day_chg_pct,
            "day_open":       round(day_open, 2),
            "day_close":      round(day_close, 2),
            # anomaly-only aggregates
            "net_bias":       net_bias,
            "buy_shares":     buy_shares,
            "sell_shares":    sell_shares,
            "buy_value_oku":  buy_value,
            "sell_value_oku": sell_value,
            # full-day tick-rule aggregates (all bars, 更贴近 app 的"主力"定义)
            "full_bias":            full_bias,
            "full_up_shares":       full_up_shares,
            "full_down_shares":     full_down_shares,
            "full_up_value_oku":    full_up_value,
            "full_down_value_oku":  full_down_value,
        }

    # 最近 N 个交易日（不足则有多少给多少）
    n_days = 5
    unique_dates = sorted(df["date"].unique(), reverse=True)[:n_days]
    days_out = []
    for dstr in unique_dates:
        day_df = df[df["date"] == dstr].copy()
        if day_df.empty:
            continue
        days_out.append(_analyze_one_day(day_df, dstr))
    if not days_out:
        return None

    # 大单精选：从 5 日全部 anomaly bars 里挑 top 3（按 ratio × value_oku 综合评分）
    all_bars = []
    for d in days_out:
        for b in d.get("peak_bars", []):
            score = b["ratio"] * b["value_oku"]   # 简单综合分：既大又异常
            all_bars.append({**b, "_score": round(score, 2), "date_short": d["date_short"]})
    highlights = sorted(all_bars, key=lambda x: x["_score"], reverse=True)[:3]

    top_day = days_out[0]   # 最近一日 = 顶层 backward-compat 字段
    detected = bool(len(top_day["peak_bars"]) >= 1 or top_day["day_ratio"] >= 1.8)
    reason = ""
    if top_day["peak_bars"]:
        top_bar = top_day["peak_bars"][0]
        dir_zh = {"buy": "主动买", "sell": "主动卖", "flat": "换手"}[top_bar["direction"]]
        reason = (f"{top_day['date_short']} {top_bar['time']} {top_bar['ratio']}× 均值 · "
                  f"{dir_zh} ¥{top_bar['value_oku']}億 (@¥{top_bar['price']})")
    elif top_day["day_ratio"] >= 1.8:
        reason = f"{top_day['date_short']} 当日总量 {top_day['day_ratio']:.2f}× 20 日日均"

    return {
        "flow": None,
        "anomaly": {
            "detected":     detected,
            "reason":       reason,
            "score":        float(min((max(top_day["day_ratio"] - 1, 0)
                                       + len(top_day["peak_bars"]) * 0.2), 1.0)),
            # backward-compat: 顶层字段 = 最近一日（原有 UI 无缝）
            "peak_bars":    top_day["peak_bars"],
            "day_ratio":    top_day["day_ratio"],
            "trading_date": top_day["trading_date"],
            "date_short":   top_day["date_short"],
            "net_bias":     top_day["net_bias"],
            "buy_shares":   top_day["buy_shares"],
            "sell_shares":  top_day["sell_shares"],
            "buy_value_oku":  top_day["buy_value_oku"],
            "sell_value_oku": top_day["sell_value_oku"],
            # 新增：5 日历史，前端可展开对比
            "days":         days_out,
            # 5 日大单精选（top 3 by ratio*value_oku）
            "highlights":   highlights,
            # 阈值参数（前端展示 tooltip）
            "thresholds":   {"ratio_min": RATIO_MIN, "value_oku_min": VALUE_OKU_MIN},
        },
        "asof":   df.index.max().strftime("%Y-%m-%d %H:%M %Z"),
        "source": "yfinance_intraday",
    }


# ────────────── 统一入口 ──────────────

def get_capital_flow(ticker: str) -> dict:
    """入口：US → moomoo；JP → yfinance intraday。失败返 source=unavailable。"""
    t = (ticker or "").upper().strip()
    if _is_jp(t):
        r = _fetch_jp_flow(t)
    else:
        r = _fetch_us_flow(t)
    if not r:
        return {
            "ticker": t,
            "source": "unavailable",
            "flow":   None,
            "anomaly": {"detected": False, "reason": "", "score": 0},
            "asof":   None,
        }
    return {"ticker": t, **r}


if __name__ == "__main__":
    import json as _json
    import sys
    for tk in (sys.argv[1:] or ["NVDA", "6981.T", "TSLA"]):
        print(f"\n=== {tk} ===")
        print(_json.dumps(get_capital_flow(tk), ensure_ascii=False, indent=2, default=str))
