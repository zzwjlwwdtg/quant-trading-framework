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
输入是一堆卖方 rate desk 常用指标（10Y 收益率、TIPS、信用利差、VIX、BBI、DXY、EEM、油价、稳定币等）和已触发的警示。

**用户 thesis (交易假设，10 月前有效)**：
  "Trump 想逼 Fed 降息 → 会主动做坏金融市场 (VIX ↑ / credit ↑ / equity ↓) + 压油价 (通胀降 → Fed 有借口降息)"
  你必须**每次评估这个 thesis 在当前数据下是否 playing out**，并明确指出：
    - 哪些节点/数据支持 thesis (supporting_nodes)
    - 哪些节点/数据反驳 thesis (contradicting_nodes)
    - 综合判定 (thesis_status): playing_out (证据链在走) / mixed / against (证据反向)
  典型支持: 油价跌、VIX 涨、credit 扩大、CPI 冷却、equity 跌 + tariff 消息
  典型反驳: 油价涨、VIX 静、credit 平静、通胀继续高、equity resilient

**你的任务**：填一个 18 节点的宏观传导链树状图。每个节点用一个方向 + 一句短评。
系统会把这些标签渲染在 dashboard 的树状图上，用户一眼看清"链条哪断了、哪没传导过来"。

**传导链结构** (18 节点)：
  inflation + jobs → fed (通胀/就业驱动 Fed 决策)
  fed → rates / real_rates / dxy
  real_rates + rates → erp
  rates → curve
  real_rates → nfci → credit → vol
  dxy → em / stablecoin
  oil (独立: 通胀 driver, Trump 压油价 → 通胀降)
  vol + em + credit → bbi
  bbi → us_equity / jp_equity / kr_equity

**输出要求**（严格 JSON，无 markdown 围栏）：
{
  "chain_verdict": "看多" | "看空" | "中性" | "谨慎乐观" | "谨慎看空",
  "chain_summary": "40 字总结整条传导链（普通人能懂）",
  "chain_blocked_at": "节点 key，传导链在这里断了；如 'credit' 表示利率高但信用还没扩大。null 表示未阻断",
  "nodes": {
    "inflation":  {"direction": "deflationary|cooling|target|elevated|hot", "note": "≤15字（通胀）"},
    "jobs":       {"direction": "strong|healthy|softening|weak|recession", "note": "≤15字（就业）"},
    "fed":        {"direction": "loose|neutral|tightening", "note": "≤15字大白话短评（禁用 RRP/TGA 术语）"},
    "rates":      {"direction": "low|normal|elevated|extreme", "note": "≤15字"},
    "real_rates": {"direction": "low|normal|elevated|extreme", "note": "≤15字"},
    "dxy":        {"direction": "weak|normal|strong|extreme", "note": "≤15字"},
    "erp":        {"direction": "cheap|fair|compressed|extreme", "note": "≤15字（股票贵/便宜）"},
    "curve":      {"direction": "inverted|flat|normal|steep", "note": "≤15字"},
    "nfci":       {"direction": "loose|neutral|tight", "note": "≤15字（金融条件）"},
    "credit":     {"direction": "calm|widening|stress", "note": "≤15字（信用市场）"},
    "stablecoin": {"direction": "shrinking|stable|growing|surging", "note": "≤15字"},
    "vol":        {"direction": "calm|elevated|panic", "note": "≤15字（波动率）"},
    "em":         {"direction": "outperforming|neutral|underperforming|crisis", "note": "≤15字"},
    "bbi":        {"direction": "greed|neutral|fear|extreme_fear", "note": "≤15字（散户情绪）"},
    "us_equity":  {"direction": "resilient|weakening|breaking|crashing", "note": "≤15字（美股）·放量时必须点明"},
    "jp_equity":  {"direction": "resilient|weakening|breaking|crashing", "note": "≤15字（日股 N225）·放量时必须点明"},
    "kr_equity":  {"direction": "resilient|weakening|breaking|crashing", "note": "≤15字（韩股 KOSPI）·放量时必须点明"},
    "oil":        {"direction": "crashing|falling|stable|rising|surging", "note": "≤15字（原油）·跌=支持 Trump 降息 thesis"}
  },
  "user_thesis_check": {
    "status": "playing_out" | "mixed" | "against" | "no_evidence",
    "summary": "40 字总结当前 thesis 状态: '油价跌了 5% 支持 thesis, 但 VIX 静默 credit 平静 → thesis 尚未成型' 之类",
    "supporting_nodes": ["oil", "vol", ...],
    "contradicting_nodes": ["credit", "vix", ...],
    "action_around_thesis": "10 月前操作建议一句话: '若 thesis playing_out → 减仓; 若 against → 继续持有防守' 之类"
  },
  "action_hint": "对散户操作建议一句话",
  "confidence": "high" | "medium" | "low"
}

**判读原则**：
- **必须**填齐 13 个节点（数据缺失时 direction=null，note='数据不足'）
- 每个 note 短、大白话，禁用 ERP/OAS/duration/NFCI/DXY/RRP/TGA/EEM/BBI/CDX 等术语
  - 例：credit 说 "银行相互借钱正常" 不说 "IG 利差正常"
  - 例：dxy 说 "美元近期走弱" 不说 "DXY z=-1.23σ"
- direction 必须严格从枚举里选，system 依此上色
- chain_blocked_at 判读：找**第一个未传导下去**的节点。例：如果 rates 高但 credit 平静 → blocked_at="credit"
- **中英混杂 OK**（如 "EM 反而跑赢"），但不要出现纯行话短语
- **量能复证**：数据里给了各节点 proxy ETF 量能 z-score (相对 20d 均值)。
  - z >= +1σ 显著放量 = 信号有真金白银支持 (可信度更高)
  - z <= -1σ 显著缩量 = 信号没资金支持 (可能是噪音/假动作)
  - 如果价 direction=elevated/tightening 但量 z <= -1σ → note 里加 "价动量弱"
  - 如果价 direction=calm 但量 z >= +1σ → note 里加 "低调放量" (蓄势)
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
        "经济数据 (Fed 决策 input)": {
            "CPI 通胀 YoY": f"{mc.get('cpi_yoy_pct')}% (asof {mc.get('cpi_asof')})" if mc.get("cpi_yoy_pct") is not None else "N/A",
            "核心 CPI YoY": f"{mc.get('core_cpi_yoy_pct')}%" if mc.get("core_cpi_yoy_pct") is not None else "N/A",
            "失业率": f"{mc.get('unemployment_pct')}% (asof {mc.get('unemployment_asof')})" if mc.get("unemployment_pct") is not None else "N/A",
        },
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
        "油价 (Trump 降息 thesis 关键变量)": {
            "USO 价格": f"${mc.get('oil_uso')}" if mc.get("oil_uso") else "N/A",
            "USO 20d 变化": f"{mc.get('oil_pct_20d')}%" if mc.get("oil_pct_20d") is not None else "N/A",
            "WTI 期货": f"${mc.get('oil_wti')}" if mc.get("oil_wti") else "N/A",
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
        "各节点量能复证 (ETF proxy volume z-score, 相对最近 20 天均值)": {
            "说明": "z>=+1σ = 显著放量 (信号有真金白银支持) · z<=-1σ = 显著缩量 (信号可能是噪音) · |z|<1 = 正常",
            **{
                f"{key} (proxy {info['proxy']} {info['label']})": f"{info['vol_z_20d']:+.2f}σ ({'放量' if info['vol_z_20d'] >= 1 else ('缩量' if info['vol_z_20d'] <= -1 else '正常')})"
                for key, info in (mc.get("volume_confirm") or {}).items()
            }
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
    """AI CLI 挂时用规则拼简版树状图输出。"""
    warnings = bond_data.get("warnings", [])
    mc = bond_data.get("macro_context", {})
    yields = bond_data.get("yields", {})
    y10v = (yields.get("10y") or {}).get("value")
    y30v = (yields.get("30y") or {}).get("value")
    tipsv = (yields.get("tips_10y") or {}).get("value")
    dxy_v = mc.get("dxy")
    erp_v = mc.get("erp_vs_tips_pct")
    ig_v = mc.get("cdx_ig_bps")
    hy_v = mc.get("cdx_hy_bps")
    vix_v = mc.get("vix")
    nfci_v = (mc.get("nfci") or {}).get("value")
    rrp_v = mc.get("rrp_bn")
    tga_v = mc.get("tga_bn")
    stable_v = mc.get("stablecoin_total_bn")
    eem_pct = mc.get("eem_pct_20d")
    eem_rel = mc.get("eem_vs_spx_20d")
    bbi_v = mc.get("bbi_score")
    cpi_yoy = mc.get("cpi_yoy_pct")
    unrate = mc.get("unemployment_pct")
    vc = mc.get("volume_confirm") or {}
    us_eq_vol = (vc.get("us_equity") or {}).get("vol_z_20d")
    jp_eq_vol = (vc.get("jp_equity") or {}).get("vol_z_20d")
    kr_eq_vol = (vc.get("kr_equity") or {}).get("vol_z_20d")

    def _cat(val, thresholds, labels):
        """val + [t1<t2<t3] + [lo, mid_lo, mid_hi, hi] → label"""
        if val is None:
            return None
        if val < thresholds[0]: return labels[0]
        if val < thresholds[1]: return labels[1]
        if val < thresholds[2]: return labels[2]
        return labels[3]

    nodes = {
        "fed":        {"direction": ("tightening" if (rrp_v is not None and rrp_v < 100) or (tga_v is not None and tga_v > 900) else "neutral"),
                       "note": f"流动性抽干 (RRP ${rrp_v}B)" if rrp_v is not None and rrp_v < 100 else "Fed 政策稳定"},
        "rates":      {"direction": _cat(y10v, [4.0, 4.5, 5.0], ["low", "normal", "elevated", "extreme"]),
                       "note": f"10Y {y10v}% 借钱贵" if y10v and y10v >= 4.5 else f"10Y {y10v}% 正常" if y10v else "数据不足"},
        "real_rates": {"direction": _cat(tipsv, [1.0, 1.5, 2.0], ["low", "normal", "elevated", "extreme"]),
                       "note": f"实际利率 {tipsv}% 高压" if tipsv and tipsv >= 2.0 else f"实际利率 {tipsv}%" if tipsv else "数据不足"},
        "dxy":        {"direction": _cat(dxy_v, [95, 100, 105], ["weak", "normal", "strong", "extreme"]),
                       "note": f"美元 {dxy_v} " + ("走弱" if dxy_v and dxy_v < 100 else "走强" if dxy_v and dxy_v > 100 else "中性") if dxy_v else "数据不足"},
        "erp":        {"direction": ("compressed" if erp_v is not None and erp_v < 2 else "fair" if erp_v is not None and erp_v < 4 else "cheap" if erp_v else None),
                       "note": f"股票比债券贵 (溢价 {erp_v}%)" if erp_v is not None and erp_v < 2 else f"股票合理 (溢价 {erp_v}%)" if erp_v else "数据不足"},
        "curve":      {"direction": "normal", "note": "曲线正常"},  # 简化
        "nfci":       {"direction": ("tight" if nfci_v is not None and nfci_v > 0 else "loose" if nfci_v is not None and nfci_v < 0 else "neutral"),
                       "note": f"金融条件宽松 ({nfci_v})" if nfci_v is not None and nfci_v < 0 else f"金融条件收紧 ({nfci_v})" if nfci_v else "数据不足"},
        "credit":     {"direction": ("stress" if hy_v is not None and hy_v > 500 else "widening" if hy_v is not None and hy_v > 400 else "calm" if hy_v else None),
                       "note": f"银行不担心 (HY {hy_v}bps 低)" if hy_v is not None and hy_v < 400 else f"信用扩大 ({hy_v}bps)" if hy_v else "数据不足"},
        "stablecoin": {"direction": ("surging" if stable_v is not None and stable_v > 250 else "growing" if stable_v is not None and stable_v > 150 else "stable" if stable_v else None),
                       "note": f"影子美元 ${stable_v}B" if stable_v else "数据不足"},
        "vol":        {"direction": ("panic" if vix_v and vix_v > 30 else "elevated" if vix_v and vix_v > 20 else "calm" if vix_v else None),
                       "note": f"期权市场平静 (VIX {vix_v})" if vix_v and vix_v < 20 else f"波动上升 (VIX {vix_v})" if vix_v else "数据不足"},
        "em":         {"direction": ("outperforming" if eem_rel is not None and eem_rel > 3 else "underperforming" if eem_rel is not None and eem_rel < -3 else "neutral" if eem_pct is not None else None),
                       "note": f"EM 跑赢 SPX +{eem_rel}pp" if eem_rel is not None and eem_rel > 0 else f"EM 跑输 {eem_rel}pp" if eem_rel is not None else "数据不足"},
        "bbi":        {"direction": ("fear" if bbi_v is not None and bbi_v >= 3 else "greed" if bbi_v is not None and bbi_v <= -3 else "neutral" if bbi_v is not None else None),
                       "note": f"散户情绪 BBI {bbi_v}" if bbi_v is not None else "数据不足"},
        "inflation":  {"direction": ("hot" if cpi_yoy is not None and cpi_yoy >= 4 else "elevated" if cpi_yoy is not None and cpi_yoy >= 3 else "target" if cpi_yoy is not None and cpi_yoy >= 2 else "cooling" if cpi_yoy is not None else None),
                       "note": f"CPI +{cpi_yoy}% YoY" if cpi_yoy is not None else "数据不足"},
        "jobs":       {"direction": ("weak" if unrate is not None and unrate >= 5 else "softening" if unrate is not None and unrate >= 4.5 else "healthy" if unrate is not None else None),
                       "note": f"失业率 {unrate}%" if unrate is not None else "数据不足"},
        "us_equity":  {"direction": "resilient",
                       "note": (f"US 股放量 (SPY +{us_eq_vol}σ)" if us_eq_vol is not None and us_eq_vol >= 1 else "US 股扛着")},
        "jp_equity":  {"direction": "resilient",
                       "note": (f"日股放量 (EWJ +{jp_eq_vol}σ)" if jp_eq_vol is not None and jp_eq_vol >= 1 else "日股平稳")},
        "kr_equity":  {"direction": "resilient",
                       "note": (f"韩股放量 (EWY +{kr_eq_vol}σ)" if kr_eq_vol is not None and kr_eq_vol >= 1 else "韩股平稳")},
        "oil":        {"direction": ("crashing" if mc.get("oil_pct_20d") is not None and mc.get("oil_pct_20d") <= -10
                                     else "falling" if mc.get("oil_pct_20d") is not None and mc.get("oil_pct_20d") <= -5
                                     else "rising" if mc.get("oil_pct_20d") is not None and mc.get("oil_pct_20d") >= 5
                                     else "surging" if mc.get("oil_pct_20d") is not None and mc.get("oil_pct_20d") >= 10
                                     else "stable" if mc.get("oil_pct_20d") is not None else None),
                       "note": f"油价 20d {mc.get('oil_pct_20d'):+.1f}%" if mc.get("oil_pct_20d") is not None else "数据不足"},
    }
    # 简版 thesis check
    oil_pct = mc.get("oil_pct_20d")
    supporting = []
    contradicting = []
    if oil_pct is not None:
        (supporting if oil_pct <= -3 else contradicting).append("oil")
    if vix_v is not None:
        (supporting if vix_v >= 22 else contradicting).append("vol")
    if hy_v is not None:
        (supporting if hy_v >= 400 else contradicting).append("credit")
    if cpi_yoy is not None:
        (supporting if cpi_yoy <= 2.5 else contradicting).append("inflation")
    thesis_status = "playing_out" if len(supporting) >= 3 else "against" if len(contradicting) >= 3 else "mixed"
    thesis_check = {
        "status": thesis_status,
        "summary": (f"支持 {len(supporting)} 项 vs 反驳 {len(contradicting)} 项 → 规则版判定 {thesis_status}"),
        "supporting_nodes": supporting,
        "contradicting_nodes": contradicting,
        "action_around_thesis": "10 月前继续观察, thesis 未成型别提前减仓" if thesis_status != "playing_out" else "thesis playing out, 逐步减仓风险高持仓",
    }

    yields_high = y10v and y10v >= 4.5
    stress_absent = (ig_v or 999) < 100 and (hy_v or 999) < 350
    if yields_high and stress_absent:
        one_liner = "利率高但市场没在怕，谨慎观望"
        verdict = "谨慎乐观"
        action = "持仓不动，别盲目加仓"
        blocked = "credit"
    elif yields_high and not stress_absent:
        one_liner = "利率高 + 信用扩大，风险开始传导"
        verdict = "谨慎看空"
        action = "减仓部分风险高的持仓"
        blocked = None
    else:
        one_liner = "宏观环境相对平静"
        verdict = "中性"
        action = "按原计划操作"
        blocked = None

    return {
        "chain_verdict": verdict,
        "chain_summary": one_liner,
        "chain_blocked_at": blocked,
        "nodes": nodes,
        "user_thesis_check": thesis_check,
        "action_hint": action,
        "confidence": "low",
        "_source": "rules_fallback",
        # 兼容旧字段 (dashboard 老版本)
        "one_liner": one_liner,
        "verdict": verdict,
        "reasoning": "规则版兜底解读，AI CLI 不可用。" + one_liner,
        "who_agrees": "规则版兜底 (AI CLI 不可用)",
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

    # 兜底缺字段 (新旧字段都填)
    for k, dflt in (
        ("chain_verdict", "中性"), ("chain_summary", ""), ("chain_blocked_at", None),
        ("nodes", {}), ("action_hint", ""), ("confidence", "low"),
        # 兼容旧字段
        ("verdict", "中性"), ("one_liner", ""), ("reasoning", ""), ("who_agrees", ""),
    ):
        result.setdefault(k, dflt)
    # 老 verdict/one_liner 未填 → 从 chain 版本迁移
    if not result.get("verdict") or result["verdict"] == "中性":
        result["verdict"] = result.get("chain_verdict", "中性")
    if not result.get("one_liner"):
        result["one_liner"] = result.get("chain_summary", "")
    result["_source"] = f"ai_cli_{provider.lower()}"
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


if __name__ == "__main__":
    from bond_monitor import get_bond_monitor
    d = get_bond_monitor()
    result = interpret_bond_context(d)
    print(json.dumps(result, ensure_ascii=False, indent=2))
