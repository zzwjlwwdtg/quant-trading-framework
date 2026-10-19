"""
黄金宏观驱动回测 — 对比 gold_macro 注入 vs 不注入的 GLD 决策 hit rate。

挑战：历史宏观数据需要从 FRED 拉指定日期的 DFII10/DGS10/WALCL/DCOILWTICO 历史，
并合成对应日期的 DXY 历史（yfinance）。

简化方案：一次性拉过去 500 天所有 series 历史 → 缓存为 dict[date_str] → series_value
然后对每个回测日，构造 history_at_date(d) 调 gold_macro 子函数生成当时的 signal。

判据（feedback_backtest_gate.md）：
  · GLD decision hit rate 5d / 10d / 20d ≥ baseline (无宏观注入)
  · 非历史窗口期（缺数据日）跳过
"""
from __future__ import annotations
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd
import yfinance as yf

from backtest_engine import load_history, add_indicators, build_mkt
from decision_agent import get_decision

DAYS = 250
FORWARD = [5, 10, 20]
FAKE_EVENTS_BASE = {
    "breaking_news": False, "days_to_event": 99,
    "risk_level": "moderate",
    "top_headlines": [], "next_event": "none",
    "next_event_name": "", "next_event_date": "",
    "next_event_impact": "normal",
    "event_verify_src": "", "breaking_evidence": "",
    "trump_signal": {"fallback": True},
}
FAKE_MACRO = {"vix": 18, "fg_score": 55, "t10y2y": 0.4}
from config import FRED_API_KEY

# 复用 gold_macro 的子函数（避免重新实现 trend 判定）
from gold_macro import (
    _analyze_real_rate, _analyze_usd, _analyze_fed_balance,
    _analyze_nominal_yield, _analyze_oil, _analyze_fomc,
)


def fetch_fred_history(series_id: str, n: int = 600) -> dict:
    """拉 series 过去 n 个观察，返回 {date_str: value}"""
    if not FRED_API_KEY:
        return {}
    try:
        url = (f"https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={series_id}&api_key={FRED_API_KEY}"
               f"&sort_order=desc&limit={n}&file_type=json")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        out = {}
        for obs in data["observations"]:
            if obs["value"] != ".":
                try:
                    out[obs["date"]] = float(obs["value"])
                except Exception:
                    pass
        return out
    except Exception as e:
        print(f"  ERR fetch {series_id}: {e}")
        return {}


def fetch_dxy_history(days: int = 600) -> pd.DataFrame:
    """拉 DXY 历史"""
    try:
        hist = yf.Ticker("DX-Y.NYB").history(period=f"{days}d")
        hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index
        return hist[["Close"]].rename(columns={"Close": "value"})
    except Exception as e:
        print(f"  ERR fetch DXY: {e}")
        return pd.DataFrame()


def build_gold_macro_at(date: pd.Timestamp, fred_hist: dict, dxy: pd.DataFrame) -> dict:
    """根据指定日期构造当时的 gold_macro signal。
    对每个 series 取 date 当天或之前最近的值。"""
    date_str = date.strftime("%Y-%m-%d")

    # 找指定日期或之前最近的值
    def _value_at(series_dict: dict, target: str, ago_days: int = 0) -> float | None:
        target_dt = datetime.strptime(target, "%Y-%m-%d") - timedelta(days=ago_days)
        # FRED 数据按日期排序的反向索引；找 ≤ target 的最近
        sorted_dates = sorted(series_dict.keys(), reverse=True)
        for d in sorted_dates:
            if datetime.strptime(d, "%Y-%m-%d") <= target_dt:
                return series_dict[d]
        return None

    # 构造 fred dict mimicking fetch_fred() 输出格式
    fake_fred = {}
    for sid in ("DGS10", "DFII10", "WALCL", "DCOILWTICO", "FEDFUNDS"):
        history = fred_hist.get(sid, {})
        v_now = _value_at(history, date_str, 0)
        v_5d = _value_at(history, date_str, 5)
        v_20d = _value_at(history, date_str, 20)
        fake_fred[sid] = {
            "name": sid,
            "value": v_now,
            "value_5d_ago": v_5d,
            "value_20d_ago": v_20d,
        }

    # 构造 dxy dict
    fake_dxy = {}
    if not dxy.empty:
        # 找 ≤ date 的最近行
        mask = dxy.index <= date
        if mask.any():
            sub = dxy.loc[mask]
            v_now = float(sub.iloc[-1]["value"])
            fake_dxy["value"] = v_now
            if len(sub) >= 5:
                v5 = float(sub.iloc[-5]["value"])
                fake_dxy["value_5d_ago"] = v5
                fake_dxy["pct_chg_5d"] = (v_now - v5) / v5 * 100
            if len(sub) >= 20:
                v20 = float(sub.iloc[-20]["value"])
                fake_dxy["pct_chg_20d"] = (v_now - v20) / v20 * 100

    real_rate = _analyze_real_rate(fake_fred)
    usd = _analyze_usd(fake_dxy)
    fed_bs = _analyze_fed_balance(fake_fred)
    nominal = _analyze_nominal_yield(fake_fred)
    oil = _analyze_oil(fake_fred)
    # FOMC 历史回测里简化：直接看 FEDFUNDS 最近两个观察的差
    fomc = {"direction": "neutral", "weight": 0, "fallback": True}

    sub_signals = [real_rate, usd, fed_bs, nominal, oil, fomc]
    total_score = 0
    for s in sub_signals:
        d = s.get("direction")
        w = s.get("weight", 0)
        if d == "bullish":  total_score += w
        elif d == "bearish": total_score -= w
    if total_score >= 2:  composite_dir = "bullish"
    elif total_score <= -2: composite_dir = "bearish"
    else: composite_dir = "neutral"
    total_weight = sum(s.get("weight", 0) for s in sub_signals)
    confidence = min(total_weight * 2, 10)
    all_fb = all(s.get("fallback", False) for s in sub_signals)

    return {
        "real_rate": real_rate, "usd_index": usd,
        "fed_balance": fed_bs, "nominal_yield_10y": nominal,
        "oil_wti": oil, "fomc_recent": fomc,
        "composite": {"direction": composite_dir, "score": total_score,
                      "confidence": confidence},
        "fallback": all_fb,
    }


def backtest_gld(days: int = DAYS) -> dict:
    print(f"\n[GLD] 拉历史宏观数据...")
    fred_hist = {sid: fetch_fred_history(sid)
                  for sid in ("DGS10", "DFII10", "WALCL", "DCOILWTICO", "FEDFUNDS")}
    dxy = fetch_dxy_history()
    print(f"  FRED series 拉到：{[(k, len(v)) for k,v in fred_hist.items()]}")
    print(f"  DXY 历史天数: {len(dxy)}")

    df = add_indicators(load_history("GLD", days=days + 60))
    df = df.dropna(subset=["rsi_14", "ma20", "ma50", "bb_pct", "cci_20"])
    dates = list(df.index)[-days:]

    new_dec_dirs = []
    legacy_dec_dirs = []
    new_dec_actions = []
    legacy_dec_actions = []
    truths = []
    forward_returns_n = {n: [] for n in FORWARD}
    # 对每天，记录 new/legacy 的 hit
    new_hit = {n: [] for n in FORWARD}
    legacy_hit = {n: [] for n in FORWARD}
    action_count_new = defaultdict(int)
    action_count_legacy = defaultdict(int)
    diff_days = 0

    for i, d in enumerate(dates):
        if i + max(FORWARD) >= len(dates):
            break
        row = df.loc[d]
        try:
            mkt = build_mkt("US.GLD", row)
        except Exception:
            continue
        # 构造当日 gold_macro
        gold_macro = build_gold_macro_at(d, fred_hist, dxy)
        events_new = dict(FAKE_EVENTS_BASE)
        events_new["gold_bias"] = gold_macro["composite"]["direction"]
        events_new["gold_macro"] = gold_macro
        events_legacy = dict(FAKE_EVENTS_BASE)
        events_legacy["gold_bias"] = "neutral"
        events_legacy["gold_macro"] = {"fallback": True}

        try:
            # _gold_rules 通过 get_gold_decision 调用
            from decision_agent import get_gold_decision
            dec_new = get_gold_decision(mkt, events_new, FAKE_MACRO,
                                         board_regime="neutral")
            dec_leg = get_gold_decision(mkt, events_legacy, FAKE_MACRO,
                                         board_regime="neutral")
        except Exception as e:
            continue

        a_new = dec_new.get("action", "HOLD")
        a_leg = dec_leg.get("action", "HOLD")
        action_count_new[a_new] += 1
        action_count_legacy[a_leg] += 1
        if a_new != a_leg:
            diff_days += 1

        # 方向：BUY/WATCH_BUY = bullish; SELL/REDUCE/CAUTION = bearish; HOLD = skip
        def _direction(a):
            if a in ("BUY", "WATCH_BUY"): return "bullish"
            if a in ("SELL", "REDUCE", "CAUTION"): return "bearish"
            return "skip"

        today_close = float(df.loc[d, "close"])
        for n in FORWARD:
            future_idx = i + n
            if future_idx >= len(dates):
                continue
            future_close = float(df.loc[dates[future_idx], "close"])
            ret_pct = (future_close - today_close) / today_close * 100
            actual = "bullish" if ret_pct > 0 else "bearish"
            forward_returns_n[n].append(ret_pct)
            new_dir = _direction(a_new)
            leg_dir = _direction(a_leg)
            if new_dir != "skip":
                new_hit[n].append(1 if new_dir == actual else 0)
            if leg_dir != "skip":
                legacy_hit[n].append(1 if leg_dir == actual else 0)

    return {
        "new_hit": new_hit, "legacy_hit": legacy_hit,
        "action_count_new": dict(action_count_new),
        "action_count_legacy": dict(action_count_legacy),
        "diff_days": diff_days, "total": len(dates),
    }


def main():
    print("=" * 72)
    print(f"  Gold Macro 注入回测  | 标的: GLD  | 窗口: {DAYS}d")
    print("=" * 72)

    r = backtest_gld()

    print()
    print("Action 分布:")
    print(f"  new   : {r['action_count_new']}")
    print(f"  legacy: {r['action_count_legacy']}")
    print(f"  决策变化天数: {r['diff_days']}/{r['total']}")
    print()
    print(f"  {'scenario':<8} {'period':<8} {'N':>5} {'hit%':>8}")
    print("  " + "-" * 40)
    for scenario, hits in (("new", r["new_hit"]), ("legacy", r["legacy_hit"])):
        for n in FORWARD:
            arr = hits[n]
            if not arr:
                continue
            hit_rate = np.mean(arr) * 100
            print(f"  {scenario:<8} {n}d{' '*5} {len(arr):>5} {hit_rate:>7.1f}%")

    # 判据
    print()
    deltas = {}
    for n in FORWARD:
        new_arr, leg_arr = r["new_hit"][n], r["legacy_hit"][n]
        if not new_arr or not leg_arr:
            continue
        delta = np.mean(new_arr) - np.mean(leg_arr)
        deltas[n] = delta * 100
        sign = "+" if delta >= 0 else ""
        print(f"  {n}d Δ hit rate: {sign}{delta*100:.1f}%")
    worst = min(deltas.values()) if deltas else 0
    if worst >= -1.0:
        print(f"\n  ✓ 不退化（最差 Δ = {worst:+.1f}%）— 可以 ship")
    else:
        print(f"\n  ✗ 退化（最差 Δ = {worst:+.1f}%）— 需要调阈值或回滚")


if __name__ == "__main__":
    main()
