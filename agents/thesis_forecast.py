"""thesis_forecast.py — 未来 45 天事件对 Fed 降息概率的场景预测.

对每个即将发生的重大事件 (CPI/PCE/NFP/FOMC 等), 用规则输出 3 个场景:
  · dovish (数据偏软, 支持降息) — 预测 cut_prob 会升多少 pp
  · base   (数据符合共识)      — 预测 cut_prob 变化 ~0
  · hawkish (数据偏强, 阻碍降息) — 预测 cut_prob 会降多少 pp

用途:
  1. 事件日历 forward-looking view (系统"预测"未来 X 天有哪些窗口)
  2. 用户提前知道: 下次 CPI 如果 dovish 概率能升 +8pp; hawkish 会跌 -8pp
  3. 事件发布当天可对照实际数据落在哪个场景, 追加验证 to jsonl

规则表基于历史反应, 不用 AI (确保 reproducible):
  CPI YoY   MoM (共识)  →  规则 delta:
    dovish  < -0.15pp    → +8pp
    dovish  -0.15 to -0.05 → +3pp
    base    -0.05 to +0.05 → 0
    hawkish +0.05 to +0.15 → -3pp
    hawkish > +0.15       → -8pp

  NFP:
    dovish  < 100K   → +5pp
    dovish  100-150K → +2pp
    base    150-250K → 0
    hawkish 250-350K → -3pp
    hawkish > 350K   → -6pp

  FOMC:
    cut 25bp             → +30pp (已 priced 但发生就是 confirm)
    hold + dovish signal → +5pp
    hold + neutral       → 0
    hold + hawkish       → -5pp
    hike (surprise)      → -20pp

  PCE Core MoM:
    dovish < 0.15%   → +5pp
    base 0.15-0.25%  → 0
    hawkish > 0.30%  → -5pp
    hawkish > 0.40%  → -10pp

  Retail Sales / PPI: impact 较小, ±2-3pp
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


# ── 事件反应规则表 ──────────────────────────────────────────────
# 每个规则: (event_type_pattern, scenarios_dict)
# scenarios_dict = {"dovish": {desc, delta_pp}, "base": {...}, "hawkish": {...}}
_RULES = {
    "CPI": {
        "dovish": {
            "desc": "CPI MoM < -0.15% (通胀明显冷却)",
            "delta_pp": +8,
            "prob_note": "让 Fed 有明确借口降息",
        },
        "mild_dovish": {
            "desc": "CPI MoM -0.15% ~ -0.05% (略冷)",
            "delta_pp": +3,
            "prob_note": "略微支持降息",
        },
        "base": {
            "desc": "CPI MoM -0.05% ~ +0.05% (符合共识)",
            "delta_pp": 0,
            "prob_note": "无明显方向",
        },
        "mild_hawkish": {
            "desc": "CPI MoM +0.05% ~ +0.15% (略强)",
            "delta_pp": -3,
            "prob_note": "略微推迟降息",
        },
        "hawkish": {
            "desc": "CPI MoM > +0.15% (通胀反弹)",
            "delta_pp": -8,
            "prob_note": "Fed 会推迟降息",
        },
    },
    "NFP": {
        "dovish": {
            "desc": "NFP < 100K (就业明显放缓)",
            "delta_pp": +5,
            "prob_note": "推动 Fed 降息保就业",
        },
        "mild_dovish": {
            "desc": "NFP 100-150K (温和放缓)",
            "delta_pp": +2,
            "prob_note": "略微支持降息",
        },
        "base": {
            "desc": "NFP 150-250K (符合共识)",
            "delta_pp": 0,
            "prob_note": "无明显方向",
        },
        "hawkish": {
            "desc": "NFP 250-350K (就业强劲)",
            "delta_pp": -3,
            "prob_note": "推迟降息",
        },
        "very_hawkish": {
            "desc": "NFP > 350K (就业过热)",
            "delta_pp": -6,
            "prob_note": "Fed 更不敢降息",
        },
    },
    "PCE": {
        "dovish": {
            "desc": "核心 PCE MoM < 0.15% (通胀冷却)",
            "delta_pp": +5,
            "prob_note": "Fed 主看这个数据",
        },
        "base": {
            "desc": "核心 PCE MoM 0.15-0.25% (中性)",
            "delta_pp": 0,
            "prob_note": "无明显方向",
        },
        "hawkish": {
            "desc": "核心 PCE MoM 0.25-0.30% (略强)",
            "delta_pp": -3,
            "prob_note": "略推迟降息",
        },
        "very_hawkish": {
            "desc": "核心 PCE MoM > 0.30% (通胀反弹)",
            "delta_pp": -8,
            "prob_note": "Fed 会明确 pushback",
        },
    },
    "FOMC": {
        "surprise_cut": {
            "desc": "降息 25bp (远超预期)",
            "delta_pp": +30,
            "prob_note": "已发生就是 confirm, 后续概率 100%",
        },
        "dovish_hold": {
            "desc": "维持 + dot plot 转鸽 / 释放降息信号",
            "delta_pp": +8,
            "prob_note": "Powell 发言暗示降息临近",
        },
        "base": {
            "desc": "维持 + 中性发言",
            "delta_pp": 0,
            "prob_note": "无明显方向",
        },
        "hawkish_hold": {
            "desc": "维持 + 强调通胀风险 / 推迟降息",
            "delta_pp": -8,
            "prob_note": "Powell 明确 pushback",
        },
        "surprise_hike": {
            "desc": "加息 (极端反常)",
            "delta_pp": -25,
            "prob_note": "降息 thesis 彻底破产",
        },
    },
    "PPI": {
        "dovish": {"desc": "PPI MoM < 0% (生产端通胀降)", "delta_pp": +3, "prob_note": "通胀领先指标降"},
        "base": {"desc": "PPI MoM 符合预期", "delta_pp": 0, "prob_note": "无方向"},
        "hawkish": {"desc": "PPI MoM > +0.3%", "delta_pp": -3, "prob_note": "上游通胀有压力"},
    },
    "Retail Sales": {
        "dovish": {"desc": "零售 MoM < 0% (消费明显走弱)", "delta_pp": +3, "prob_note": "支持降息保消费"},
        "base": {"desc": "零售 MoM 0-0.5% (中性)", "delta_pp": 0, "prob_note": "无方向"},
        "hawkish": {"desc": "零售 MoM > 0.5% (消费强)", "delta_pp": -2, "prob_note": "略微推迟降息"},
    },
}


def _classify_event(event_name: str) -> Optional[str]:
    """Event name → rule table key."""
    en = (event_name or "").upper()
    if "CPI" in en: return "CPI"
    if "NFP" in en or "NONFARM" in en: return "NFP"
    if "PCE" in en: return "PCE"
    if "FOMC" in en: return "FOMC"
    if "PPI" in en: return "PPI"
    if "RETAIL" in en: return "Retail Sales"
    return None


def get_forecast(days_ahead: int = 45) -> dict:
    """
    返回未来 N 天的重大事件 + 每个事件的 3 场景预测.
    格式:
      {
        "as_of": today ISO,
        "events": [
          {
            "date": "2026-09-11",
            "days_until": 18,
            "event": "CPI Release",
            "impact": "critical",
            "event_type": "CPI",
            "scenarios": {
              "dovish": {desc, delta_pp, prob_note},
              "base": {...},
              "hawkish": {...},
            },
            "delta_range": "(-8pp ~ +8pp)"
          }, ...
        ]
      }
    """
    try:
        from events_watch import EQUITY_CALENDAR, _classify_event_impact
    except Exception as e:
        return {"error": f"events_watch import failed: {str(e)[:80]}"}

    today = date.today()
    events_out = []
    for ev in EQUITY_CALENDAR:
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        days_until = (ev_date - today).days
        if days_until < 0 or days_until > days_ahead:
            continue
        impact = _classify_event_impact(ev["event"])
        # 只挑 critical / high 的 (moderate/normal 忽略, 减少噪音)
        if impact not in ("critical", "high"):
            continue
        event_type = _classify_event(ev["event"])
        scenarios = _RULES.get(event_type)
        if not scenarios:
            continue
        # 找 min/max delta 展示范围
        deltas = [s["delta_pp"] for s in scenarios.values()]
        min_d = min(deltas); max_d = max(deltas)
        events_out.append({
            "date": ev["date"],
            "days_until": days_until,
            "event": ev["event"],
            "impact": impact,
            "event_type": event_type,
            "scenarios": scenarios,
            "delta_range_pp": [min_d, max_d],
            "range_desc": f"({min_d:+d}pp ~ {max_d:+d}pp)",
        })

    events_out.sort(key=lambda x: x["days_until"])
    return {
        "as_of": today.isoformat(),
        "days_ahead": days_ahead,
        "count": len(events_out),
        "events": events_out,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "note": "Scenarios 是规则版预测. 实际数据发布时,系统会自动对照到最近的 scenario 追加 thesis_history 验证.",
    }


if __name__ == "__main__":
    import json
    r = get_forecast(days_ahead=45)
    print(f"共 {r.get('count')} 条 forward-looking 事件 (未来 45 天):")
    print()
    for ev in r.get("events", []):
        print(f"  📅 {ev['date']} ({ev['days_until']:>2}d) {ev['event']:<25s} {ev['range_desc']}")
        for name, s in ev["scenarios"].items():
            sign = "+" if s["delta_pp"] > 0 else ""
            print(f"      {name:<15s} → {sign}{s['delta_pp']:>3}pp · {s['desc']}")
        print()
