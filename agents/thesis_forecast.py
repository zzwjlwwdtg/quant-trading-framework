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
# ── 先验概率 (基于最近历史 base rate + 数据分布) ─────────────────
# 通胀环境: 通胀 3.5-4% 时 (当前) 数据更容易偏 hawkish (通胀粘性)
# 就业环境: 失业率 4-4.5% 时 (当前偏低) 数据更容易 base/hawkish
# 数字加起来 = 1.0 (规范化)
_PRIOR_PROBS = {
    # 当前通胀 3.7% (偏高), Fed 仍 restrictive → CPI 偏 hawkish 概率高
    "CPI": {
        "dovish": 0.10, "mild_dovish": 0.15, "base": 0.35,
        "mild_hawkish": 0.25, "hawkish": 0.15
    },
    # NFP 就业强劲, 通常在 base/hawkish
    "NFP": {
        "dovish": 0.10, "mild_dovish": 0.15, "base": 0.45,
        "hawkish": 0.20, "very_hawkish": 0.10
    },
    # PCE 是 Fed 首选指标, 相对 CPI 更贴近 target 2% (核心通胀粘性)
    "PCE": {
        "dovish": 0.15, "base": 0.45,
        "hawkish": 0.25, "very_hawkish": 0.15
    },
    # PPI/Retail 分布类似 CPI/NFP 但影响较小
    "PPI": {"dovish": 0.20, "base": 0.50, "hawkish": 0.30},
    "Retail Sales": {"dovish": 0.20, "base": 0.55, "hawkish": 0.25},
    # FOMC: 会被 CME FedWatch 覆盖. 默认 fallback: 大概率 hold
    "FOMC": {
        "surprise_cut": 0.05, "dovish_hold": 0.25, "base": 0.55,
        "hawkish_hold": 0.12, "surprise_hike": 0.03
    },
}


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


def _load_cleveland_nowcast() -> Optional[dict]:
    """加载 Cleveland Fed InflationNowcast (缓存 12h)."""
    try:
        from cleveland_nowcast import get_nowcast
        return get_nowcast()
    except Exception:
        return None


def _get_nowcast_probs(nowcast: Optional[dict], event_type: str) -> Optional[dict]:
    """从 Cleveland Fed nowcast 派生 CPI/PCE 场景概率.
    优于历史 prior, 因为反映真实经济学家模型预测."""
    if not nowcast or not nowcast.get("current_month"):
        return None
    cm = nowcast["current_month"]
    try:
        from cleveland_nowcast import (
            scenario_probabilities_from_nowcast,
            CPI_RANGES, PCE_RANGES,
        )
    except Exception:
        return None
    if event_type == "CPI":
        val = cm.get("cpi_mom")
        if val is not None:
            return scenario_probabilities_from_nowcast(val, CPI_RANGES)
    elif event_type == "PCE":
        val = cm.get("core_pce_mom")   # 用 Core PCE (Fed 首选指标)
        if val is not None:
            return scenario_probabilities_from_nowcast(val, PCE_RANGES)
    return None


def _load_fedwatch_cache() -> Optional[dict]:
    """尝试从 webui 缓存读 fed_watch 数据."""
    try:
        from pathlib import Path
        import json
        p = Path(__file__).parent / ".webui_cache" / "fed_watch.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("value") if isinstance(d, dict) else None
    except Exception:
        return None


def _get_fomc_probabilities(fed_watch: Optional[dict], event_date: str) -> Optional[dict]:
    """从 fed_watch 数据映射 CME FedWatch action probabilities → 我们的场景.

    CME FedWatch actions: hike_25/hike_50 / hold / cut_25/cut_50
    映射:
      cut_50 (罕见)              → surprise_cut
      cut_25                     → 部分 (取决于 dot plot 里 dovish 强度)
      hold (majority)            → hold_dovish + base + hold_hawkish (需 disaggregate)
      hike_25/50                 → surprise_hike
    简化: 直接用 FedWatch 的 cut/hold/hike 概率, 手动 split hold 成 dovish/base/hawkish (30%/50%/20%).
    """
    if not fed_watch or not fed_watch.get("meetings"):
        return None
    meeting = None
    for m in fed_watch["meetings"]:
        if m.get("date", "").startswith(event_date[:7]):  # 同月
            meeting = m
            break
    if not meeting:
        return None
    probs = meeting.get("probabilities") or {}
    if not probs:
        return None
    p_hold = probs.get("hold", 0) / 100.0
    p_cut25 = probs.get("cut_25", 0) / 100.0
    p_cut50 = probs.get("cut_50", 0) / 100.0
    p_hike25 = probs.get("hike_25", 0) / 100.0
    p_hike50 = probs.get("hike_50", 0) / 100.0
    # 归一化
    total = p_hold + p_cut25 + p_cut50 + p_hike25 + p_hike50
    if total <= 0:
        return None
    # Split hold 成 dovish/base/hawkish (30/50/20)
    return {
        "surprise_cut": round((p_cut25 + p_cut50) / total, 3),
        "dovish_hold": round(p_hold * 0.30 / total, 3),
        "base": round(p_hold * 0.50 / total, 3),
        "hawkish_hold": round(p_hold * 0.20 / total, 3),
        "surprise_hike": round((p_hike25 + p_hike50) / total, 3),
    }


# ── 按事件类型 × 方向定义 具体受益/受损 资产列表 (金十数据风格) ──────────
_ASSET_MAP = {
    # 利率敏感事件: dovish (cut_prob 升) = 降息预期强 = 全 risk-on
    "CPI_dovish": {
        "利多": ["科技股/纳指 (QQQ/NVDA)", "长债 (TLT)", "黄金 (GLD)", "REITs", "growth 股"],
        "利空": ["USD (UUP)", "银行股 (XLF)", "能源股 (通胀对冲失效)"],
    },
    "CPI_hawkish": {
        "利空": ["科技股/纳指 (长久期最伤)", "长债 (TLT 收益率飙)", "REITs", "growth 股"],
        "利多": ["USD (UUP)", "能源/大宗 (通胀受益)", "银行股 (利差扩)", "黄金 (通胀对冲)"],
    },
    "PCE_dovish": {
        "利多": ["科技股/纳指", "长债", "黄金", "REITs"],
        "利空": ["USD", "银行股"],
    },
    "PCE_hawkish": {
        "利空": ["科技股", "长债", "REITs", "growth 股"],
        "利多": ["USD", "能源/大宗", "银行股", "黄金 (通胀对冲)"],
    },
    "FOMC_dovish": {
        "利多": ["全部风险资产 (股/加密/HY 债)", "长债", "黄金", "REITs"],
        "利空": ["USD (美元指数)"],
    },
    "FOMC_hawkish": {
        "利空": ["科技股 (估值杀)", "长债", "黄金", "REITs", "新兴市场"],
        "利多": ["USD 强化", "银行股"],
    },
    # 增长敏感事件
    "NFP_dovish": {  # 就业弱 = 衰退担忧
        "利多": ["长债 (避险)", "黄金 (避险)", "防御股 (公用/医疗 XLV/XLU)"],
        "利空": ["消费股 (XLY/XRT)", "工业股 (XLI)", "银行股", "小盘股 (IWM)"],
    },
    "NFP_hawkish": {  # 就业强 = 加息压力
        "利多": ["消费股 (XLY/XRT)", "工业股", "小盘股", "USD"],
        "利空": ["长债", "growth 股 (加息预期)", "REITs"],
    },
    "PPI_dovish": {
        "利多": ["长债", "growth 股 (成本降利润升)", "消费股"],
        "利空": ["能源/材料"],
    },
    "PPI_hawkish": {
        "利多": ["能源/材料", "大宗商品 (USO/GLD)"],
        "利空": ["制造业 (成本升)", "长债"],
    },
    "Retail_Sales_dovish": {  # 消费弱 = 衰退担忧
        "利多": ["长债", "防御股", "黄金"],
        "利空": ["消费股", "小盘股"],
    },
    "Retail_Sales_hawkish": {  # 消费强 = 加息压力
        "利多": ["消费股", "银行股", "小盘股"],
        "利空": ["长债"],
    },
}


def _market_impact_from_expected(event_type: str, expected_pp: float) -> dict:
    """把 cut_prob 期望 delta 翻译成对债市/股市 + 具体标的的影响 (金十数据风格).

    传导逻辑:
    · 债市: 与 cut_prob 强相关 (概率升 → 收益率降 → 债价升). 1:1 direct.
    · 股市: 分两类事件
        - 利率敏感 (CPI/PCE/FOMC): 与 cut_prob 同向 (discount rate 主导)
        - 增长敏感 (NFP/Retail/PPI): 反向 (经济强 = 利率高但盈利好, 净效应弱化)
    · 具体标的: 用 _ASSET_MAP 查表, 按 dovish/hawkish 方向输出受益+受损列表
    """
    def _label(pp: float) -> str:
        # 更细粒度阈值 (金十数据不用"中性"当兜底)
        if pp >= 3.0:  return "强利多"
        if pp >= 1.0:  return "利多"
        if pp >= 0.3:  return "偏多"
        if pp <= -3.0: return "强利空"
        if pp <= -1.0: return "利空"
        if pp <= -0.3: return "偏空"
        return "无明显方向"

    # dovish (cut_prob 升) or hawkish (cut_prob 降) 方向
    if expected_pp >= 0.3:
        direction = "dovish"
    elif expected_pp <= -0.3:
        direction = "hawkish"
    else:
        direction = None  # 太平

    bond_label = _label(expected_pp)

    # 股市: 分利率敏感 vs 增长敏感
    rate_sensitive = event_type in ("CPI", "PCE", "FOMC")
    if rate_sensitive:
        equity_label = _label(expected_pp)
        equity_channel = "利率驱动"
    else:
        # 增长敏感: 打半折 (对总大盘 SPX)
        soft_pp = expected_pp * 0.5
        equity_label = _label(soft_pp)
        equity_channel = "增长/利率对冲"

    # 查具体标的
    favored: list[str] = []
    hurt: list[str] = []
    if direction:
        # event_type + direction → key
        key = f"{event_type.replace(' ','_')}_{direction}"
        assets = _ASSET_MAP.get(key, {})
        favored = assets.get("利多", [])
        hurt = assets.get("利空", [])

    return {
        "bond": bond_label,
        "equity": equity_label,
        "equity_channel": equity_channel,
        "direction": direction or "neutral",
        "long_favored": favored,     # 利多标的列表 (金十风格)
        "long_hurt": hurt,            # 利空标的列表
        # 数字估算 (rough): 每 1pp cut_prob → 10Y yield ~-3bp, SPX ~+0.15%
        "bond_yield_bps": round(-expected_pp * 3, 1),
        "spx_pct_est": round(expected_pp * (0.15 if rate_sensitive else 0.05), 2),
    }


def _compute_expected_delta(scenarios: dict, probs: dict) -> tuple[float, str]:
    """Σ prob × delta_pp. 返回 (expected_pp, source)."""
    total = 0.0
    prob_sum = 0.0
    for name, s in scenarios.items():
        p = probs.get(name, 0)
        total += p * s["delta_pp"]
        prob_sum += p
    if prob_sum <= 0.01:  # 概率没覆盖
        return (0.0, "no_prob")
    # 若不满 1.0 (只覆盖部分), normalize
    if prob_sum < 0.99:
        total = total / prob_sum
    return (round(total, 2), "ok")


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
    fed_watch = _load_fedwatch_cache()
    nowcast = _load_cleveland_nowcast()
    events_out = []
    for ev in EQUITY_CALENDAR:
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        days_until = (ev_date - today).days
        if days_until < 0 or days_until > days_ahead:
            continue
        # 优先用日历自带的 impact 字段 (更精确, 已由维护者手工标注),
        # fallback 到 name 分类器. 修复过: PCE 带注释描述会被 exact-match 打成 moderate.
        impact = ev.get("impact") or _classify_event_impact(ev["event"])
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

        # 概率数据源优先级:
        #   FOMC: CME FedWatch → prior
        #   CPI/PCE: Cleveland Fed nowcast → prior
        #   其它 (NFP/PPI/Retail): prior (无实时 nowcast)
        prob_source = "prior"
        probs = _PRIOR_PROBS.get(event_type, {})
        if event_type == "FOMC":
            fw_probs = _get_fomc_probabilities(fed_watch, ev["date"])
            if fw_probs:
                probs = fw_probs
                prob_source = "cme_fedwatch"
        elif event_type in ("CPI", "PCE"):
            nc_probs = _get_nowcast_probs(nowcast, event_type)
            if nc_probs:
                probs = nc_probs
                prob_source = "cleveland_fed_nowcast"

        # 每场景嵌入概率 (合并 scenarios + probs)
        scenarios_with_prob = {}
        for name, s in scenarios.items():
            scenarios_with_prob[name] = {
                **s,
                "probability": round(probs.get(name, 0), 3),
            }

        expected_pp, ev_source = _compute_expected_delta(scenarios, probs)

        market_impact = _market_impact_from_expected(event_type, expected_pp)
        events_out.append({
            "date": ev["date"],
            "days_until": days_until,
            "event": ev["event"],
            "impact": impact,
            "event_type": event_type,
            "scenarios": scenarios_with_prob,
            "delta_range_pp": [min_d, max_d],
            "range_desc": f"({min_d:+d}pp ~ {max_d:+d}pp)",
            "expected_delta_pp": expected_pp,
            "probability_source": prob_source,
            "market_impact": market_impact,   # {bond, equity, equity_channel, bond_yield_bps, spx_pct_est}
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
        exp = ev.get("expected_delta_pp", 0)
        sign_exp = "+" if exp > 0 else ""
        src = ev.get("probability_source", "?")
        print(f"  📅 {ev['date']} ({ev['days_until']:>2}d) {ev['event']:<25s} {ev['range_desc']}  期望={sign_exp}{exp}pp [{src}]")
        for name, s in ev["scenarios"].items():
            sign = "+" if s["delta_pp"] > 0 else ""
            p = s.get("probability", 0)
            print(f"      {name:<15s} → {sign}{s['delta_pp']:>3}pp × {p*100:>4.1f}% = {p*s['delta_pp']:>5.2f}pp · {s['desc']}")
        print()
