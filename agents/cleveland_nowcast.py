"""cleveland_nowcast.py — Cleveland Fed InflationNowcast 数据源.

拉 https://www.clevelandfed.org 的 CPI/PCE/Core CPI/Core PCE MoM 实时预测,
用来给 thesis_forecast 的场景 probability 提供数据支撑 (替代 hardcoded prior)。

数据结构:
  list of 158 monthly items, 每个含:
    chart.subcaption = "YYYY-M" (例 "2026-9")
    dataset = [
      {seriesname: "CPI Inflation", data: [{value: "0.16"}, ...]},
      {seriesname: "Core CPI Inflation", data: [...]},
      {seriesname: "PCE Inflation", data: [...]},
      {seriesname: "Core PCE Inflation", data: [...]},
    ]
  data 数组的最后一个非空 value 是最新预测

更新频率: 每周二 (Weekly)
免费, 无 API key.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

_NOWCAST_URL = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json"
_CACHE_PATH = Path(__file__).parent / ".webui_cache" / "cleveland_nowcast.json"
_CACHE_TTL_HOURS = 12  # 每 12h 拉一次


def _fetch_raw() -> Optional[list]:
    """Fetch raw nowcast JSON (list of monthly items)."""
    try:
        req = urllib.request.Request(_NOWCAST_URL,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _get_latest_value(dataset_series: dict) -> Optional[float]:
    """从 series data 找最后一个非空 value."""
    for item in reversed(dataset_series.get("data", [])):
        v = item.get("value", "")
        if v and v.strip():
            try:
                return float(v)
            except ValueError:
                continue
    return None


def _parse_monthly_forecasts(items: list) -> dict:
    """
    Parse nowcast items → find current + next month CPI/PCE MoM forecasts.
    Returns:
      {
        "current_month": {
          "subcaption": "2026-8",  # YYYY-M
          "cpi_mom": 0.16,
          "core_cpi_mom": 0.21,
          "pce_mom": 0.13,
          "core_pce_mom": 0.18,
        },
        "next_month": {...},  # 如果有的话
      }
    """
    today = datetime.now()
    cur_key = f"{today.year}-{today.month}"
    # 找 current + next 月
    result = {"current_month": None, "next_month": None}
    for item in items:
        sub = (item.get("chart") or {}).get("subcaption", "")
        if not sub:
            continue
        dataset = item.get("dataset", [])
        parsed = {"subcaption": sub}
        for series in dataset:
            name = (series.get("seriesname") or "").lower()
            val = _get_latest_value(series)
            if val is None:
                continue
            if "core cpi" in name:
                parsed["core_cpi_mom"] = round(val, 3)
            elif "core pce" in name:
                parsed["core_pce_mom"] = round(val, 3)
            elif "cpi" in name and "core" not in name:
                parsed["cpi_mom"] = round(val, 3)
            elif "pce" in name and "core" not in name:
                parsed["pce_mom"] = round(val, 3)
        # 只关注最近的几个月
        if sub == cur_key:
            result["current_month"] = parsed
        else:
            # 试 next_month (year, month+1)
            nxt_month = today.month + 1
            nxt_year = today.year
            if nxt_month > 12:
                nxt_month -= 12
                nxt_year += 1
            if sub == f"{nxt_year}-{nxt_month}":
                result["next_month"] = parsed
    return result


def get_nowcast() -> dict:
    """入口: 拉 nowcast + 缓存 12h. 返 current/next month forecasts."""
    # 检查缓存
    if _CACHE_PATH.exists():
        try:
            cached = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            fetched_at = cached.get("_fetched_at", 0)
            if time.time() - fetched_at < _CACHE_TTL_HOURS * 3600:
                return cached
        except Exception:
            pass

    # 拉新数据
    raw = _fetch_raw()
    if not raw:
        return {"error": "fetch_failed", "current_month": None, "next_month": None}

    parsed = _parse_monthly_forecasts(raw)
    parsed["_fetched_at"] = time.time()
    parsed["_source"] = "cleveland_fed"
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    except Exception:
        pass
    return parsed


def scenario_probabilities_from_nowcast(
    nowcast_mom_value: float,
    scenario_ranges: list[tuple[str, float, float]],
    sd: float = 0.10,
) -> dict:
    """
    输入: nowcast 预测 (例 CPI MoM +0.25%) + 场景范围列表
    输出: 各场景概率 (基于 nowcast 为中心 + normal distribution)

    scenario_ranges: [
      ("dovish", -inf, -0.15),
      ("mild_dovish", -0.15, -0.05),
      ("base", -0.05, 0.05),
      ("mild_hawkish", 0.05, 0.15),
      ("hawkish", 0.15, inf),
    ]

    用 normal(nowcast, sd) 积分近似.
    历史 CPI MoM surprise std ~0.10%.
    """
    from math import erf, sqrt
    def _cdf(x, mu, sigma):
        return 0.5 * (1 + erf((x - mu) / (sigma * sqrt(2))))

    result = {}
    total = 0.0
    for name, low, high in scenario_ranges:
        p_low = 0 if low == float("-inf") else _cdf(low, nowcast_mom_value, sd)
        p_high = 1 if high == float("inf") else _cdf(high, nowcast_mom_value, sd)
        p = max(0, p_high - p_low)
        result[name] = p
        total += p
    # 归一化 (对 rounding 误差)
    if total > 0:
        result = {k: round(v / total, 3) for k, v in result.items()}
    return result


# 场景范围表 (对应 thesis_forecast._RULES 的 delta buckets)
CPI_RANGES = [
    ("dovish", float("-inf"), -0.15),
    ("mild_dovish", -0.15, -0.05),
    ("base", -0.05, 0.05),
    ("mild_hawkish", 0.05, 0.15),
    ("hawkish", 0.15, float("inf")),
]
PCE_RANGES = [
    ("dovish", float("-inf"), 0.15),
    ("base", 0.15, 0.25),
    ("hawkish", 0.25, 0.30),
    ("very_hawkish", 0.30, float("inf")),
]


if __name__ == "__main__":
    d = get_nowcast()
    print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    print()
    print("=== 场景概率演示 ===")
    cm = d.get("current_month") or {}
    if cm.get("cpi_mom") is not None:
        probs = scenario_probabilities_from_nowcast(cm["cpi_mom"], CPI_RANGES)
        print(f"CPI nowcast MoM={cm['cpi_mom']:+.3f}%:")
        for k, p in probs.items():
            print(f"  {k:<15s} {p*100:>5.1f}%")
    if cm.get("core_pce_mom") is not None:
        probs = scenario_probabilities_from_nowcast(cm["core_pce_mom"], PCE_RANGES)
        print(f"\nCore PCE nowcast MoM={cm['core_pce_mom']:+.3f}%:")
        for k, p in probs.items():
            print(f"  {k:<15s} {p*100:>5.1f}%")
