"""bond_ai_interpret.py — 把 bond_monitor 数字翻译成大白话.

Motivation: bond_monitor 输出 warnings + macro_context 里全是数字（10Y 4.74%,
ERP 1.53%, BBI +1.2, IG 82bps 之类），普通用户看不懂。这个模块把这一堆
数字交给 AI CLI（Claude 或 Codex）生成一段自然语言解读。

用途：dashboard 顶部宏观速览 banner 直接显示这段解读，替代干巴巴的数字。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional


_SYSTEM_PROMPT = """你是资深宏观策略师，用**大白话**给不懂金融的用户解释当前美国债/股市宏观状态。
输入是一堆卖方 rate desk 常用指标（10Y 收益率、TIPS、信用利差、VIX、BBI 等）和已触发的警示。

输出要求（严格 JSON，无 markdown 围栏）：
{
  "one_liner": "40 字以内一句话总结当前宏观状态（普通人能懂，不用行话）",
  "verdict": "看多股市" | "看空股市" | "中性" | "谨慎乐观" | "谨慎看空",
  "reasoning": "3-5 句话解释为什么。举例：'长期利率高说明借钱成本贵，公司利润受压。但信用市场平静说明银行还没紧张，风险还没扩散。' 只用大白话，禁用 ERP/OAS/duration 这类术语（除非能同时用大白话解释）",
  "who_agrees": "哪些卖方观点跟当前数据一致（如 Morgan Stanley Wilson 看空 = ERP 压缩证据；Goldman 谨慎乐观 = 信用平静）",
  "action_hint": "对散户股民的操作建议一句话（'继续持有'/'减仓' 之类，不给具体标的）",
  "confidence": "high" | "medium" | "low"
}

判读原则：
- **不预测**具体涨跌，只解释**当前状态和为什么**
- **多角度**：如果有相反证据（如利率高但信用平静），必须提到
- **禁用术语**（除非配大白话解释）：ERP、OAS、term premium、duration、hedge ratio、NFCI、DXY、RRP、TGA、EEM
- **数字要有 anchor**：说"10Y 4.74%"没意义，要说"比过去 10 年平均高了差不多 1 个点"
- 强调**这个观察是给你参考不是预测**
- **全球视角**：如果数据显示美元太强/EM 崩跌/稳定币暴增/Fed 流动性抽干，必须提到这些**跨市场**信号对 US 股的传导（EM 危机 → risk-off → SPX 跌；稳定币抽美债 → 短端利率被压 → 曲线扭曲 等）
"""


def _make_prompt(bond_data: dict) -> str:
    """Compose the AI prompt from bond_monitor snapshot."""
    warnings = bond_data.get("warnings", [])
    mc = bond_data.get("macro_context", {})
    yields = bond_data.get("yields", {})

    y10 = yields.get("10y", {}).get("value")
    y30 = yields.get("30y", {}).get("value")
    tips = yields.get("tips_10y", {}).get("value")
    spreads = bond_data.get("spreads", {})
    s2s10 = (spreads.get("2s10s", {}) or {}).get("value")

    facts = {
        "asof": bond_data.get("asof", "?"),
        "利率水平": {
            "10Y 名义": f"{y10}%" if y10 else "N/A",
            "30Y 名义": f"{y30}%" if y30 else "N/A",
            "10Y 实际(TIPS)": f"{tips}%" if tips else "N/A",
            "2s10s 曲线": f"{s2s10*100:.0f}bps" if s2s10 is not None else "N/A",
            "30s10s term spread": f"{mc.get('term_spread_30_10_bps')}bps" if mc.get('term_spread_30_10_bps') is not None else "N/A",
        },
        "股票估值": {
            "SPX 尾随 P/E": mc.get("spx_trailing_pe") or "N/A",
            "SPX EPS 收益率": f"{mc.get('spx_eps_yield_pct')}%" if mc.get('spx_eps_yield_pct') else "N/A",
            "ERP (股权风险溢价)": f"{mc.get('erp_vs_tips_pct')}%" if mc.get('erp_vs_tips_pct') else "N/A",
        },
        "信用市场": {
            "投资级 IG 利差": f"{mc.get('cdx_ig_bps')}bps" if mc.get('cdx_ig_bps') else "N/A",
            "高收益 HY 利差": f"{mc.get('cdx_hy_bps')}bps" if mc.get('cdx_hy_bps') else "N/A",
        },
        "金融条件": {
            "NFCI 芝加哥 Fed 金融条件": mc.get("nfci", {}).get("value") if mc.get("nfci") else "N/A",
            "VIX 波动率": mc.get("vix") or "N/A",
            "BBI (Bull&Bear 复合)": mc.get("bbi_score") or "N/A",
        },
        "黄金对冲": {
            "GLD vs TIPS 相关性": (bond_data.get("gld_correlation") or {}).get("vs_tips_10y"),
            "GLD hedge 状态": (bond_data.get("gld_correlation") or {}).get("regime"),
        },
        "全球美元流动性 + 稳定币虹吸": {
            "DXY 美元指数": mc.get("dxy") or "N/A",
            "DXY 20d 变化": f"{mc.get('dxy_pct_20d')}%" if mc.get("dxy_pct_20d") is not None else "N/A",
            "DXY 60d z-score": f"{mc.get('dxy_z_60d')}σ" if mc.get("dxy_z_60d") is not None else "N/A",
            "EEM 新兴市场股 20d": f"{mc.get('eem_pct_20d')}%" if mc.get("eem_pct_20d") is not None else "N/A",
            "EEM vs SPX 20d": f"{mc.get('eem_vs_spx_20d')}pp" if mc.get("eem_vs_spx_20d") is not None else "N/A",
            "Fed 逆回购 RRP": f"${mc.get('rrp_bn')}B (2023 峰值 $2500B)" if mc.get("rrp_bn") is not None else "N/A",
            "Treasury 一般账户 TGA": f"${mc.get('tga_bn')}B" if mc.get("tga_bn") is not None else "N/A",
            "稳定币总市值 (USDT+USDC)": f"${mc.get('stablecoin_total_bn')}B (USDT ${mc.get('usdt_bn')}B + USDC ${mc.get('usdc_bn')}B)" if mc.get("stablecoin_total_bn") else "N/A",
        },
        "已触发警示": [
            {"level": w["level"], "msg": w["msg"]} for w in warnings
        ],
    }

    return f"""{_SYSTEM_PROMPT}

当前数据快照:
```json
{json.dumps(facts, ensure_ascii=False, indent=2)}
```

请返回严格 JSON，不要 markdown 围栏，不要额外文字。"""


def _fallback_from_rules(bond_data: dict) -> dict:
    """AI CLI 挂时用规则拼一段简版解读。"""
    warnings = bond_data.get("warnings", [])
    mc = bond_data.get("macro_context", {})
    bbi = mc.get("bbi_score")
    yields_high = any(w["key"].startswith(("10y", "30y", "tips")) for w in warnings)
    stress_absent = ((mc.get("cdx_ig_bps") or 999) < 100
                     and (mc.get("cdx_hy_bps") or 999) < 350)
    if yields_high and stress_absent:
        one_liner = "利率高但市场没在怕，谨慎观望"
        verdict = "谨慎乐观"
        reasoning = "长期国债利率涨到 4.5% 以上说明借钱成本变贵，理论上对股市不利。但同时信用市场（银行相互借钱的利率）非常平静，说明大机构还没恐慌。"
        action = "持仓不动，别盲目加仓"
    elif yields_high and not stress_absent:
        one_liner = "利率高 + 信用扩大，风险开始传导"
        verdict = "谨慎看空"
        reasoning = "利率高的同时信用利差也开始扩大，说明风险正在从债市传导到股市。"
        action = "减仓部分风险高的持仓"
    else:
        one_liner = "宏观环境相对平静"
        verdict = "中性"
        reasoning = "各类指标处于历史正常区间。"
        action = "按原计划操作"
    return {
        "one_liner": one_liner,
        "verdict": verdict,
        "reasoning": reasoning,
        "who_agrees": "规则版兜底（AI CLI 不可用），跟 Goldman/JPM 谨慎乐观 view 一致",
        "action_hint": action,
        "confidence": "low",
        "_source": "rules_fallback",
    }


def interpret_bond_context(bond_data: dict, timeout: int = 120) -> dict:
    """输入 bond_monitor.get_bond_monitor() 输出，返自然语言解读 dict.

    Returns:
      {"one_liner", "verdict", "reasoning", "who_agrees", "action_hint",
       "confidence", "generated_at", "_source"}
    """
    if not bond_data or not isinstance(bond_data, dict):
        return {"error": "no_bond_data"}
    try:
        from ai_prompt import query_ai_cli
    except Exception as e:
        result = _fallback_from_rules(bond_data)
        result["_source"] = f"rules_fallback (ai_prompt import fail: {str(e)[:60]})"
        return result

    prompt = _make_prompt(bond_data)
    out, status, provider, fb_reason = query_ai_cli(prompt, timeout=timeout)
    if not out:
        result = _fallback_from_rules(bond_data)
        result["_source"] = f"rules_fallback ({provider}: {status[:100]})"
        result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return result

    # 解析 AI 返 JSON
    text = out.strip()
    # 剥离可能的 markdown 围栏
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    # 抓大括号内容
    try:
        first_brace = text.index("{")
        last_brace = text.rindex("}")
        text = text[first_brace:last_brace + 1]
        result = json.loads(text)
    except (ValueError, json.JSONDecodeError) as e:
        result = _fallback_from_rules(bond_data)
        result["_source"] = f"rules_fallback (json parse fail: {str(e)[:60]}, raw_head: {out[:120]!r})"
        result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return result

    # 兜底缺字段
    for k, dflt in (("one_liner", ""), ("verdict", "中性"), ("reasoning", ""),
                    ("who_agrees", ""), ("action_hint", ""), ("confidence", "low")):
        result.setdefault(k, dflt)
    result["_source"] = f"ai_cli_{provider.lower()}"
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


if __name__ == "__main__":
    from bond_monitor import get_bond_monitor
    d = get_bond_monitor()
    result = interpret_bond_context(d)
    print(json.dumps(result, ensure_ascii=False, indent=2))
