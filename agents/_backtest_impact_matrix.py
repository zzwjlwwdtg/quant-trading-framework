"""
_backtest_impact_matrix.py — impact_matrix 量级参数历史校准回测

拉过去 5 个关键 FOMC / panic 事件, 用 yfinance + FRED 拉真实价格,
比对模型预测 vs 实际市场反应, 输出偏差 → 建议 magnitude 调整.

事件选择原则:
  - 覆盖 hawkish shock (2022 hike cycle 大 hike 日)
  - 覆盖 dovish surprise (2020-03 emergency cut)
  - 覆盖 panic flight-to-quality (2023 SVB)
  - 覆盖 base neutral (2024 hold day)

数据源:
  UST 10Y: yfinance ^TNX (值 × 10, unit %)
  SPX: yfinance SPY (unit %)
  HY OAS: FRED BAMLH0A0HYM2 (unit %)
  GLD: yfinance GLD (unit %)

CLI: python _backtest_impact_matrix.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta

sys.stdout.reconfigure(line_buffering=True)

from config import FRED_API_KEY
from thesis_forecast import _impact_matrix

# 历史事件 (校准用)
# scenario_actual: 事件实际归属的 model scenario (人工标注 based on 事件性质)
# mkt_implied_prob: 事前市场对 dominant scenario 的定价 (人工估算 based on 当时环境)
HISTORICAL_EVENTS = [
    {
        "date": "2022-03-16",
        "label": "FOMC 首次 hike (25bp, 通胀升温)",
        "event_type": "FOMC",
        "scenario_actual": "hawkish_shock",
        "mkt_implied_prob": 0.90,  # 大概率 pricing (但通胀刚爆)
        "context": "Fed 加息周期起点, 通胀已达 7.9%",
    },
    {
        "date": "2022-05-04",
        "label": "FOMC 50bp hike (加速)",
        "event_type": "FOMC",
        "scenario_actual": "hawkish_shock",
        "mkt_implied_prob": 0.85,
        "context": "首次 >25bp hike, 通胀 8.5%",
    },
    {
        "date": "2022-06-15",
        "label": "FOMC 75bp hike (最大 hike 自 1994)",
        "event_type": "FOMC",
        "scenario_actual": "hawkish_shock",
        "mkt_implied_prob": 0.50,  # market 定价 50bp, hike 75bp = 大 surprise
        "context": "CPI 上周超预期, hike 75bp 是大 surprise",
    },
    {
        "date": "2023-03-10",
        "label": "SVB 崩溃 (panic flight-to-quality)",
        "event_type": "FOMC",  # 分类为 FOMC scenario matrix
        "scenario_actual": "panic_flight_to_quality",
        "mkt_implied_prob": 0.02,  # SVB 崩溃是完全意外
        "context": "SVB 关闭, 波及区域银行, Fed 紧急启动 BTFP",
    },
    {
        "date": "2020-03-15",
        "label": "FOMC 紧急 100bp cut (COVID)",
        "event_type": "FOMC",
        "scenario_actual": "dovish_surprise",
        "mkt_implied_prob": 0.30,  # 市场 pricing 部分 cut, 但 100bp 是 surprise
        "context": "COVID lockdown 前夜, 100bp cut + QE 无限量",
    },
    {
        "date": "2024-01-31",
        "label": "FOMC hold (中性, 期望之内)",
        "event_type": "FOMC",
        "scenario_actual": "base_neutral",
        "mkt_implied_prob": 0.95,  # 完全 pricing hold
        "context": "无政策变化, 声明轻微 dovish tilt",
    },
]


def _get_fred(sid: str, start: str, end: str) -> dict:
    """FRED 拉一段时期 daily 数据."""
    url = (f"https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={sid}&api_key={FRED_API_KEY}&file_type=json"
           f"&observation_start={start}&observation_end={end}&sort_order=asc")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.loads(r.read())
    except Exception as exc:
        print(f"  FRED {sid} err: {exc}")
        return {}
    result = {}
    for o in d.get("observations", []):
        if o.get("value") in ("", ".", None):
            continue
        try:
            result[date.fromisoformat(o["date"])] = float(o["value"])
        except Exception:
            continue
    return result


def _get_yf(ticker: str, start: str, end: str) -> dict:
    """yfinance 拉一段时期 daily close."""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(start=start, end=end)
        return {ts.date(): float(px) for ts, px in h["Close"].items()}
    except Exception as exc:
        print(f"  yf {ticker} err: {exc}")
        return {}


def _compute_actual_moves(event_date: date, horizons_days: dict[str, int]) -> dict:
    """对某事件日期, 拉相关价格 + 算 UST/SPX/HY/GLD 5 horizons 实际变化."""
    start = (event_date - timedelta(days=10)).isoformat()
    end = (event_date + timedelta(days=120)).isoformat()
    tnx = _get_yf("^TNX", start, end)      # UST 10Y (值 × 10, %)
    spy = _get_yf("SPY", start, end)
    gld = _get_yf("GLD", start, end)
    hy  = _get_fred("BAMLH0A0HYM2", start, end)  # HY OAS %

    # 找 baseline (事件前 1 trading day 的 close)
    def _baseline(prices: dict) -> tuple[date, float] | None:
        for i in range(1, 8):
            d = event_date - timedelta(days=i)
            if d in prices:
                return d, prices[d]
        return None

    # 找 T+n horizon 的 close (n 是 calendar days, 找最近的交易日)
    def _at_horizon(prices: dict, cal_days: int) -> tuple[date, float] | None:
        target = event_date + timedelta(days=cal_days)
        for i in range(cal_days, cal_days + 8):
            d = event_date + timedelta(days=i)
            if d in prices:
                return d, prices[d]
        return None

    tnx_base = _baseline(tnx)
    spy_base = _baseline(spy)
    gld_base = _baseline(gld)
    hy_base  = _baseline(hy)

    result = {}
    for h_label, cal_days in horizons_days.items():
        tnx_h = _at_horizon(tnx, cal_days)
        spy_h = _at_horizon(spy, cal_days)
        gld_h = _at_horizon(gld, cal_days)
        hy_h  = _at_horizon(hy, cal_days)
        row = {"horizon": h_label}
        # ^TNX 是 10x, 差值 × 10 = bps
        if tnx_base and tnx_h:
            row["ust_10y_bps"] = round((tnx_h[1] - tnx_base[1]) * 10, 1)
        if spy_base and spy_h:
            row["spx_pct"] = round((spy_h[1] / spy_base[1] - 1) * 100, 2)
        if hy_base and hy_h:
            # FRED BAMLH0A0HYM2 单位 %, × 100 = bps
            row["hy_oas_bps"] = round((hy_h[1] - hy_base[1]) * 100, 1)
        if gld_base and gld_h:
            row["gld_pct"] = round((gld_h[1] / gld_base[1] - 1) * 100, 2)
        result[h_label] = row
    return result


HORIZONS = {"T+0": 1, "T+1D": 2, "T+1W": 7, "T+1M": 30, "T+3M": 90}


def run():
    print("=" * 100)
    print("Impact Matrix 历史校准回测 (model prediction vs 实际市场反应)")
    print("=" * 100)

    diff_by_horizon = {h: {"ust_10y_bps": [], "spx_pct": [], "hy_oas_bps": [], "gld_pct": []} for h in HORIZONS}

    for ev in HISTORICAL_EVENTS:
        print(f"\n{'─' * 100}")
        print(f"📅 {ev['date']}  {ev['label']}")
        print(f"   scenario: {ev['scenario_actual']}  ·  mkt_implied_prob: {ev['mkt_implied_prob']}")
        print(f"   context: {ev['context']}")

        # 拉实际数据
        ev_date = date.fromisoformat(ev["date"])
        actual = _compute_actual_moves(ev_date, HORIZONS)

        # 模型预测
        # For hike events: expected_pp negative (fewer cuts expected); for cuts: positive
        # 简化: 用 sign 表示 scenario 方向
        expected_pp = -8 if "hawkish" in ev["scenario_actual"] else (+8 if "dovish" in ev["scenario_actual"] else 0)
        model = _impact_matrix(ev["event_type"], expected_pp, mkt_implied_prob=ev["mkt_implied_prob"])
        scen_data = model["scenarios"].get(ev["scenario_actual"], {})
        model_horizons = scen_data.get("horizons", {})

        print(f"\n   {'horizon':<7} | {'metric':<12} | {'model':<10} | {'actual':<10} | diff")
        print(f"   {'-'*7} | {'-'*12} | {'-'*10} | {'-'*10} | ----")
        for h in HORIZONS:
            a = actual.get(h, {})
            m = model_horizons.get(h, {})
            for metric in ["ust_10y_bps", "spx_pct", "hy_oas_bps", "gld_pct"]:
                if metric not in a or metric not in m:
                    continue
                unit = "bps" if "bps" in metric else "%"
                mv = m[metric]
                av = a[metric]
                d = av - mv
                d_str = f"{d:+.1f}{unit}"
                mark = " ★" if abs(d) > (max(abs(mv), 1) * 2) else ""
                print(f"   {h:<7} | {metric:<12} | {mv:>+7.1f}{unit} | {av:>+7.1f}{unit} | {d_str:<10}{mark}")
                diff_by_horizon[h][metric].append({"event": ev["label"][:30], "diff": d, "model": mv, "actual": av})

    # 汇总统计
    print("\n" + "=" * 100)
    print("📊 汇总: 平均偏差 (actual - model), n = 事件数")
    print("=" * 100)
    print(f"{'horizon':<7} | {'metric':<12} | {'n':<3} | {'avg diff':<15} | {'median diff':<15} | 建议")
    print("-" * 100)
    for h in HORIZONS:
        for metric in ["ust_10y_bps", "spx_pct", "hy_oas_bps", "gld_pct"]:
            diffs = [x["diff"] for x in diff_by_horizon[h][metric]]
            if not diffs:
                continue
            avg_d = sum(diffs) / len(diffs)
            med_d = sorted(diffs)[len(diffs) // 2]
            unit = "bps" if "bps" in metric else "%"
            # 建议
            avg_abs = abs(avg_d)
            if avg_abs < 5 if "bps" in metric else avg_abs < 0.5:
                hint = "✓ 校准 OK"
            elif avg_d > 0:
                hint = f"⚠ model UNDER-estimate → 调大量级"
            else:
                hint = f"⚠ model OVER-estimate → 调小量级"
            print(f"{h:<7} | {metric:<12} | {len(diffs):<3} | {avg_d:>+7.1f}{unit:<6} | {med_d:>+7.1f}{unit:<6} | {hint}")


if __name__ == "__main__":
    run()
