"""
_calibrate_confidence.py
────────────────────────
方案 2：回测加权 + 百分位映射。

跑 ~250 天历史，对 confluence 每个独立技术信号回测**自己的 5d 方向命中率**，
然后：
  1. 权重 = max(0, (hit_rate - 0.5) × 4)，命中率 50% → 0；60% → 0.4；70% → 0.8 …
  2. 用权重重放历史，得 bull/bear 加权 raw 分布
  3. 用 raw 分布的分位数（p20 / p40 / p60 / p80）做 5 档置信度阈值

输出 signals/confidence_calibration.json，给 confluence + decision_agent 读。

用法：
  python _calibrate_confidence.py            # 全部 TICKERS + GLD
  python _calibrate_confidence.py --days 500 # 加长样本
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_engine import (
    load_history,
    add_indicators,
    _classify_rsi_zone,
    _classify_cci_zone,
    _classify_vol_zone,
    _classify_bb_zone,
    _classify_pct_zone,
)
from atomic_io import atomic_write_json
from config import TICKERS, SIGNALS_DIR, get_asset_class

CALIB_PATH = Path(SIGNALS_DIR) / "confidence_calibration.json"
FORWARD_DAYS = 5  # 命中率定义：T+5 收盘 vs T+0 收盘 的方向


# ── 信号条件（key → 评估函数 row → bool）─────────────────────────────────
# Bull / bear 镜像。key 稳定且会写进 JSON，confluence.py 用同样 key 查权重。
BULL_RULES = {
    "trend_up":          lambda r: r["close"] > r["ma20"],
    "ma_stack_bull":     lambda r: r["ma20"]  > r["ma50"],
    "ma_cross_golden":   lambda r: r["ma_cross"] == "golden",
    "rsi_oversold":      lambda r: r["rsi_zone"] == "oversold",
    "rsi_cross_dn30":    lambda r: r["rsi_cross"] == "dn_30",
    "cci_oversold":      lambda r: r["cci_zone"] == "oversold",
    "cci_cross_dn100":   lambda r: r["cci_cross"] == "dn_100",
    "vol_expand_up":     lambda r: r["vol_zone"] == "expand" and r["trend"] == "up",
    "new_high_healthy":  lambda r: bool(r["is_new_52w_high"]) and r["rsi_zone"] != "overbought" and r["vol_zone"] != "shrink",
    "bb_below":          lambda r: r["bb_zone"] == "below",
    "pct_surge":         lambda r: r["pct_zone"] == "surge",
    "pct_pop":           lambda r: r["pct_zone"] == "pop",
    "pct_mild_pop":      lambda r: r["pct_zone"] == "mild_pop",
}

BEAR_RULES = {
    "trend_down":        lambda r: r["close"] < r["ma20"],
    "ma_stack_bear":     lambda r: r["ma20"]  < r["ma50"],
    "ma_cross_death":    lambda r: r["ma_cross"] == "death",
    "rsi_overbought":    lambda r: r["rsi_zone"] == "overbought",
    "rsi_cross_up70":    lambda r: r["rsi_cross"] == "up_70",
    "cci_overbought":    lambda r: r["cci_zone"] == "overbought",
    "cci_cross_up100":   lambda r: r["cci_cross"] == "up_100",
    "vol_expand_down":   lambda r: r["vol_zone"] == "expand" and r["trend"] == "down",
    "new_high_diverge":  lambda r: bool(r["is_new_52w_high"]) and r["rsi_zone"] == "overbought" and r["vol_zone"] == "shrink",
    "bb_above":          lambda r: r["bb_zone"] == "above",
    "pct_crash":         lambda r: r["pct_zone"] == "crash",
    "pct_drop":          lambda r: r["pct_zone"] == "drop",
    "pct_mild_drop":     lambda r: r["pct_zone"] == "mild_drop",
}


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """加 zone 列 + ma_cross / rsi_cross / cci_cross + 前向收益。"""
    d = df.copy()
    d["trend"]    = np.where(d["close"] > d["ma20"], "up", "down")
    d["rsi_zone"] = d["rsi_14"].apply(_classify_rsi_zone)
    d["cci_zone"] = d["cci_20"].apply(_classify_cci_zone)
    d["vol_zone"] = d["vol_ratio"].apply(_classify_vol_zone)
    d["bb_zone"]  = d["bb_pct"].apply(_classify_bb_zone)
    d["pct_zone"] = d["pct_chg"].apply(_classify_pct_zone)

    # MA cross（近 3 日内 MA5/MA20 交叉）
    ma5 = d["close"].rolling(5).mean()
    cross_up = (ma5.shift(1) < d["ma20"].shift(1)) & (ma5 >= d["ma20"])
    cross_dn = (ma5.shift(1) > d["ma20"].shift(1)) & (ma5 <= d["ma20"])
    d["ma_cross"] = "none"
    for i in range(3):
        d.loc[cross_up.shift(i, fill_value=False), "ma_cross"] = "golden"
        d.loc[cross_dn.shift(i, fill_value=False), "ma_cross"] = "death"

    # RSI 30 / 70 cross
    d["rsi_cross"] = "none"
    rsi_dn = (d["rsi_14"].shift(1) >= 30) & (d["rsi_14"] < 30)
    rsi_up = (d["rsi_14"].shift(1) <= 70) & (d["rsi_14"] > 70)
    for i in range(3):
        d.loc[rsi_dn.shift(i, fill_value=False), "rsi_cross"] = "dn_30"
        d.loc[rsi_up.shift(i, fill_value=False), "rsi_cross"] = "up_70"

    # CCI -100 / +100 cross
    d["cci_cross"] = "none"
    cci_dn = (d["cci_20"].shift(1) >= -100) & (d["cci_20"] < -100)
    cci_up = (d["cci_20"].shift(1) <= 100)  & (d["cci_20"] > 100)
    for i in range(3):
        d.loc[cci_dn.shift(i, fill_value=False), "cci_cross"] = "dn_100"
        d.loc[cci_up.shift(i, fill_value=False), "cci_cross"] = "up_100"

    # 前向 N 日收益率
    d["fwd_ret"] = (d["close"].shift(-FORWARD_DAYS) / d["close"] - 1) * 100
    return d


def _eval_signals(d: pd.DataFrame, rules: dict) -> dict[str, dict]:
    """对每条规则数 fire 次数 + bull/bear 方向命中率。返回 {key: {fires, hits, hit_rate}}。"""
    out = {}
    for key, fn in rules.items():
        # 应用规则
        mask = d.apply(fn, axis=1)
        fires = int(mask.sum())
        if fires == 0:
            out[key] = {"fires": 0, "hits": 0, "hit_rate": 0.5, "weight": 0.0}
            continue
        # 命中判定：bull → fwd_ret > 0；bear → fwd_ret < 0
        is_bull = rules is BULL_RULES
        fwd = d.loc[mask & d["fwd_ret"].notna(), "fwd_ret"]
        n_valid = len(fwd)
        if n_valid == 0:
            out[key] = {"fires": fires, "hits": 0, "hit_rate": 0.5, "weight": 0.0}
            continue
        hits = int((fwd > 0).sum() if is_bull else (fwd < 0).sum())
        hit_rate = hits / n_valid
        # 权重：边际相对 50% 的提升 × 4，最低 0 最高 2
        weight = max(0.0, round((hit_rate - 0.5) * 4, 2))
        weight = min(weight, 2.0)
        out[key] = {
            "fires":    fires,
            "n_valid":  n_valid,
            "hits":     hits,
            "hit_rate": round(hit_rate, 3),
            "weight":   weight,
            "avg_fwd_ret": round(float(fwd.mean()), 2),
        }
    return out


def _weighted_raw_distribution(d: pd.DataFrame,
                                bull_weights: dict,
                                bear_weights: dict) -> tuple[list[float], list[float]]:
    """用计算出的权重重放每日加权 bull/bear raw，返回分位数 [p20,p40,p60,p80]。"""
    bull_raws, bear_raws = [], []
    for _, r in d.iterrows():
        b = sum(w["weight"] for k, w in bull_weights.items() if BULL_RULES[k](r))
        s = sum(w["weight"] for k, w in bear_weights.items() if BEAR_RULES[k](r))
        bull_raws.append(b)
        bear_raws.append(s)
    bull_pcts = [float(np.percentile(bull_raws, p)) for p in (20, 40, 60, 80)]
    bear_pcts = [float(np.percentile(bear_raws, p)) for p in (20, 40, 60, 80)]
    return bull_pcts, bear_pcts


def _combine(per_ticker: dict, rules: dict) -> dict:
    """按 total fires/hits 合并 per-ticker 结果 → 单套权重。"""
    merged = {}
    for key in rules.keys():
        total_fires = sum(d[key]["fires"] for d in per_ticker.values())
        total_hits  = sum(d[key].get("hits", 0) for d in per_ticker.values())
        total_valid = sum(d[key].get("n_valid", 0) for d in per_ticker.values())
        if total_valid == 0:
            merged[key] = {"fires": 0, "hits": 0, "n_valid": 0,
                            "hit_rate": 0.5, "weight": 0.0}
            continue
        hit_rate = total_hits / total_valid
        weight = max(0.0, round((hit_rate - 0.5) * 4, 2))
        weight = min(weight, 2.0)
        merged[key] = {
            "fires":    total_fires,
            "n_valid":  total_valid,
            "hits":     total_hits,
            "hit_rate": round(hit_rate, 3),
            "weight":   weight,
        }
    return merged


def calibrate(tickers: list[str], days: int = 250) -> dict:
    """跑全部 ticker，得到 per-asset-class 权重 + 全局 default 权重 + 分位数阈值。

    Output schema:
      {
        "ts", "lookback_days", "forward_days", "tickers",
        "bull_weights", "bear_weights",       ← default (全局合并)
        "bull_percentiles", "bear_percentiles",  ← default
        "per_class": {
          "commodity": {"bull_weights": {...}, "bear_weights": {...},
                        "bull_percentiles": {...}, "bear_percentiles": {...},
                        "tickers": [...], "n_valid_days": N},
          "equity_leveraged": {...},
          "bond": {...},
          "equity_single": {...},
        }
      }
    """
    print(f"=== 校准 confluence 权重 + 置信度分位（lookback={days}d, fwd={FORWARD_DAYS}d）===\n")
    per_ticker_bull = {}
    per_ticker_bear = {}
    per_ticker_df   = {}
    ticker_class    = {}

    for tk_full in tickers:
        tk = tk_full.replace("US.", "")
        cls = get_asset_class(tk_full)
        ticker_class[tk] = cls
        print(f"[{tk}] class={cls} 拉历史...")
        df = load_history(tk, days=days + 60)
        if df.empty:
            print(f"  {tk}: 无历史，跳过")
            continue
        df = add_indicators(df).dropna(subset=["rsi_14", "ma20", "ma50", "bb_pct", "cci_20", "vol_ratio"])
        df = df.iloc[-days:] if len(df) > days else df
        d = _enrich(df)
        per_ticker_bull[tk] = _eval_signals(d, BULL_RULES)
        per_ticker_bear[tk] = _eval_signals(d, BEAR_RULES)
        per_ticker_df[tk]   = d
        print(f"  {tk}: {len(d)} 个有效日")

    # ── Default（全部 ticker 合并）───────────────────────────
    bull_w = _combine(per_ticker_bull, BULL_RULES)
    bear_w = _combine(per_ticker_bear, BEAR_RULES)
    combined_df = pd.concat(per_ticker_df.values()) if per_ticker_df else pd.DataFrame()
    if combined_df.empty:
        bull_pcts = [1, 2, 3, 4]
        bear_pcts = [1, 2, 3, 4]
    else:
        bull_pcts, bear_pcts = _weighted_raw_distribution(combined_df, bull_w, bear_w)

    # ── Per-asset-class（分类合并）── 每类样本 ≥ 100 天才输出，否则回退 default
    per_class = {}
    classes = set(ticker_class.values())
    for cls in classes:
        tks_in_cls = [tk for tk, c in ticker_class.items() if c == cls and tk in per_ticker_bull]
        if not tks_in_cls:
            continue
        cls_bull_per = {tk: per_ticker_bull[tk] for tk in tks_in_cls}
        cls_bear_per = {tk: per_ticker_bear[tk] for tk in tks_in_cls}
        cls_df = pd.concat([per_ticker_df[tk] for tk in tks_in_cls])
        n_days = len(cls_df)
        if n_days < 100:
            print(f"  ⚠ class={cls} 样本仅 {n_days} 天 (<100)，跳过 per-class 权重，走 default")
            continue
        cls_bull_w = _combine(cls_bull_per, BULL_RULES)
        cls_bear_w = _combine(cls_bear_per, BEAR_RULES)
        cls_bull_pcts, cls_bear_pcts = _weighted_raw_distribution(cls_df, cls_bull_w, cls_bear_w)
        per_class[cls] = {
            "tickers":     tks_in_cls,
            "n_valid_days": n_days,
            "bull_weights": cls_bull_w,
            "bear_weights": cls_bear_w,
            "bull_percentiles": {
                "p20": round(cls_bull_pcts[0], 2),
                "p40": round(cls_bull_pcts[1], 2),
                "p60": round(cls_bull_pcts[2], 2),
                "p80": round(cls_bull_pcts[3], 2),
            },
            "bear_percentiles": {
                "p20": round(cls_bear_pcts[0], 2),
                "p40": round(cls_bear_pcts[1], 2),
                "p60": round(cls_bear_pcts[2], 2),
                "p80": round(cls_bear_pcts[3], 2),
            },
        }

    calibration = {
        "ts":            datetime.now().isoformat(),
        "lookback_days": days,
        "forward_days":  FORWARD_DAYS,
        "tickers":       tickers,
        # default（全 ticker 合并）— 无 per-class 匹配时的 fallback
        "bull_weights":  bull_w,
        "bear_weights":  bear_w,
        "bull_percentiles": {
            "p20": round(bull_pcts[0], 2),
            "p40": round(bull_pcts[1], 2),
            "p60": round(bull_pcts[2], 2),
            "p80": round(bull_pcts[3], 2),
        },
        "bear_percentiles": {
            "p20": round(bear_pcts[0], 2),
            "p40": round(bear_pcts[1], 2),
            "p60": round(bear_pcts[2], 2),
            "p80": round(bear_pcts[3], 2),
        },
        # 分类权重（新，per-class 校准）
        "per_class":     per_class,
    }
    return calibration


def _print_report(calib: dict) -> None:
    def _dump_side(title: str, weights: dict) -> None:
        print(f"  {title}")
        print(f"  {'key':<22} {'fires':>6} {'n_val':>6} {'hits':>5}  {'hit_rate':>9}  {'weight':>6}")
        for k, v in sorted(weights.items(), key=lambda x: -x[1]["weight"]):
            print(f"  {k:<22} {v['fires']:>6} {v['n_valid']:>6} {v['hits']:>5}  "
                  f"{v['hit_rate']:>8.1%}  {v['weight']:>6}")

    print()
    print("=" * 72)
    print("  DEFAULT（全 ticker 合并）— 用于无匹配 asset_class 的兜底")
    print("=" * 72)
    _dump_side("Bull 权重", calib["bull_weights"])
    print()
    _dump_side("Bear 权重", calib["bear_weights"])
    print()
    print("  Bull 加权 raw 分位:", ", ".join(f"{k}={v}" for k, v in calib["bull_percentiles"].items()))
    print("  Bear 加权 raw 分位:", ", ".join(f"{k}={v}" for k, v in calib["bear_percentiles"].items()))

    per_class = calib.get("per_class", {})
    if per_class:
        for cls, cd in sorted(per_class.items()):
            print()
            print("=" * 72)
            print(f"  CLASS = {cls}  ({cd['n_valid_days']} 天, tickers={cd['tickers']})")
            print("=" * 72)
            _dump_side("Bull 权重", cd["bull_weights"])
            print()
            _dump_side("Bear 权重", cd["bear_weights"])
            print()
            print("  Bull 分位:", ", ".join(f"{k}={v}" for k, v in cd["bull_percentiles"].items()))
            print("  Bear 分位:", ", ".join(f"{k}={v}" for k, v in cd["bear_percentiles"].items()))


def _side_problem(weights: dict, percentiles: dict) -> str | None:
    """Return why one bull/bear side is unsafe to publish, else None."""
    values = [float(v.get("weight") or 0) for v in (weights or {}).values()
              if isinstance(v, dict)]
    if not values:
        return "no weights"
    if max(values) <= 0:
        return "all weights are zero"
    try:
        pcts = [float(v) for v in (percentiles or {}).values()]
    except (TypeError, ValueError):
        return "percentiles are not numeric"
    if len(pcts) < 4 or not all(math.isfinite(p) for p in pcts):
        return f"invalid percentiles {pcts}"
    if len({round(p, 6) for p in pcts}) == 1:
        return f"degenerate percentiles {pcts}"
    if pcts[-1] <= 0:
        return f"top percentile <= 0 {pcts}"
    return None


def validate_calibration(calib: dict) -> list[str]:
    """Reject all-zero sides / collapsed percentile maps (P0 audit 2026-10-20).

    A calibration window that contains only an uptrend can zero every bear
    weight, which silently disables all technical sell rules. Publishing such a
    file is worse than keeping the previous one, so main() refuses the write.
    """
    problems: list[str] = []
    for side in ("bull", "bear"):
        problem = _side_problem(calib.get(f"{side}_weights"),
                                calib.get(f"{side}_percentiles"))
        if problem:
            problems.append(f"default {side}: {problem}")
    for cls, data in (calib.get("per_class") or {}).items():
        if not isinstance(data, dict):
            continue
        for side in ("bull", "bear"):
            problem = _side_problem(data.get(f"{side}_weights"),
                                    data.get(f"{side}_percentiles"))
            if problem:
                problems.append(f"{cls} {side}: {problem}")
    return problems


def _alert_rejected(problems: list[str]) -> None:
    msg = ("校准写入被拒绝 (保留旧文件): " + "; ".join(problems)
           + f" | {CALIB_PATH}")
    print("\n" + msg)
    try:
        from notifications import send_alert
        send_alert(msg, level="warning")
    except Exception as exc:
        print(f"警告推送失败: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--tickers", nargs="+", default=None)
    args = parser.parse_args()

    tickers = args.tickers or (list(TICKERS) + ["US.GLD"])
    calib = calibrate(tickers, days=args.days)
    _print_report(calib)

    problems = validate_calibration(calib)
    if problems:
        _alert_rejected(problems)
        return 2

    Path(SIGNALS_DIR).mkdir(parents=True, exist_ok=True)
    atomic_write_json(CALIB_PATH, calib)
    print(f"\n校准结果已保存: {CALIB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
