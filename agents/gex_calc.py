"""gex_calc.py — 期权结构综合分析: GEX + Vol Regime + Skew + Verdict.

对期权小白的核心目标: 通过期权结构预判**风险和机会**，dashboard 直接给结论。

理论基础（简化）:

**GEX (Gamma Exposure) = dealer 持仓 gamma × spot^2 × 100 × OI**
- 假设: dealer 与 retail 反向持仓 (retail 买 call/put → dealer 卖 → dealer short gamma)
- **正 GEX** (spot > gamma_flip): dealer 卖涨买跌 → **抑波/pin 效应** → 卖 straddle/iron condor 有 edge
- **负 GEX** (spot < gamma_flip): dealer 买涨卖跌 → **放波/加速** → 突破发动大波动
- Flip 位是 regime 转换点，跨越即 vol regime shift

**IV Regime**
- IV Premium = ATM IV / 60d Realized Vol - 1
- Premium > 30% = crush risk (event 后 IV 回归导致 option 亏钱, 即使方向对)
- Premium < 10% = 便宜期, 买保护便宜, hedge cost 低

**Skew (Put IV vs Call IV)**
- 25-delta put IV - 25-delta call IV
- Skew > 10% = 恐慌 tail 已定价, 突破 put wall 加速
- Skew < 3% = complacent, put wall 是真需求

无需 scipy，只用 stdlib (math.exp / math.erf / math.log).
"""
from __future__ import annotations

import math
from typing import Optional

RISK_FREE_RATE = 0.05   # ~ current Fed funds


def _cdf_normal(x: float) -> float:
    """N(x) — standard normal CDF via math.erf, no scipy."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pdf_normal(x: float) -> float:
    """n(x) — standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes gamma. Call gamma == put gamma."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _pdf_normal(d1) / (S * sigma * math.sqrt(T))


def _bs_delta_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0 if S < K else 1.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _cdf_normal(d1)


def _bs_delta_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return _bs_delta_call(S, K, T, r, sigma) - 1.0


def compute_gex(calls_df, puts_df, spot: float, days_to_expiry: int,
                r: float = RISK_FREE_RATE) -> dict:
    """全 strike GEX 分析 + gamma flip 定位.

    dealer 假设 (SpotGamma convention):
      - retail 净买 call → dealer net short call → dealer short gamma on calls
      - retail 净买 put → dealer net short put → dealer short gamma on puts
      - 因为 short gamma 两边都是负号，我们用**净 GEX = call GEX - put GEX**
        (call OI 更多 = dealer 更 short call → 空头 vol suppress rally, buy dip)
        (put OI 更多 = 净负 GEX → 突破 put wall 加速)

    返回:
    {
      total_gex_millions,       # $M per 1% underlying move
      by_strike: [{strike, call_gex, put_gex, net_gex_m}, ...],
      gamma_flip_strike,        # cumulative GEX 从负→正的价位
      spot_vs_flip_pct,         # (spot - flip) / spot * 100
      regime,                   # "positive_pin" | "negative_squeeze" | "at_flip"
      regime_zh,
      regime_hint,              # 一句话解释
    }
    """
    if calls_df is None or calls_df.empty or spot <= 0 or days_to_expiry <= 0:
        return {"error": "insufficient_data"}
    T = days_to_expiry / 365.0

    strikes = set()
    call_map = {}  # strike -> (OI, IV)
    put_map  = {}
    for _, row in calls_df.iterrows():
        k = float(row.get("strike", 0))
        oi = int(row.get("openInterest", 0) or 0)
        iv = float(row.get("impliedVolatility", 0) or 0)
        if k > 0 and oi > 0:
            call_map[k] = (oi, iv)
            strikes.add(k)
    for _, row in puts_df.iterrows():
        k = float(row.get("strike", 0))
        oi = int(row.get("openInterest", 0) or 0)
        iv = float(row.get("impliedVolatility", 0) or 0)
        if k > 0 and oi > 0:
            put_map[k] = (oi, iv)
            strikes.add(k)

    strikes = sorted(strikes)
    by_strike = []
    for k in strikes:
        c_oi, c_iv = call_map.get(k, (0, 0))
        p_oi, p_iv = put_map.get(k, (0, 0))
        # dealer 假设 short 两侧 → GEX 都是负号 (但 call/put gamma 都是正数)
        # 净 GEX = call gamma × call OI - put gamma × put OI
        # (call OI 大 → dealer more short call → 更 negative gamma → 更"顶盖" effect)
        # 但为 dashboard 直观：正数 = 抑波区，负数 = 放波区，用 spot^2 × 100 × 0.01 归一化
        c_gamma = _bs_gamma(spot, k, T, r, c_iv) if c_iv > 0 else 0
        p_gamma = _bs_gamma(spot, k, T, r, p_iv) if p_iv > 0 else 0
        # GEX 单位: $ per 1% spot move
        # = OI × 100 (contract multiplier) × spot × spot × gamma × 0.01
        # 简化: 用 spot × 100 × OI × gamma (即 1 point 的 delta 变化对应 $)
        norm = spot * spot * 100 * 0.01
        c_gex = c_oi * c_gamma * norm
        p_gex = p_oi * p_gamma * norm
        # dealer short call = negative sign for call GEX
        # dealer short put  = positive sign for put GEX
        # (put 增大 → dealer buy stock rally, sell dip → vol amplify)
        # ↑ 这个符号约定按 SpotGamma 惯例：net = call_gex - put_gex
        net = c_gex - p_gex
        by_strike.append({
            "strike":     round(k, 2),
            "call_oi":    c_oi,
            "put_oi":     p_oi,
            "call_iv":    round(c_iv * 100, 1) if c_iv else None,
            "put_iv":     round(p_iv * 100, 1) if p_iv else None,
            "call_gex_m": round(c_gex / 1e6, 3),
            "put_gex_m":  round(p_gex / 1e6, 3),
            "net_gex_m":  round(net / 1e6, 3),
        })

    total = sum(s["net_gex_m"] for s in by_strike)

    # Gamma flip = cumulative net GEX 从负→正 (或反向) 的 strike
    # 从最低 strike 累积
    cum = 0.0
    flip_strike = None
    prev_cum = 0.0
    for s in by_strike:
        prev_cum = cum
        cum += s["net_gex_m"]
        if flip_strike is None and prev_cum <= 0 < cum:
            flip_strike = s["strike"]
        elif flip_strike is None and prev_cum >= 0 > cum:
            flip_strike = s["strike"]
    # fallback: 用 spot 附近最大 |cum_shift| 的 strike
    if flip_strike is None and by_strike:
        # 找 net_gex_m 最大 abs 的 strike 作为"效果中心"
        flip_strike = max(by_strike, key=lambda s: abs(s["net_gex_m"]))["strike"]

    spot_vs_flip_pct = ((spot - flip_strike) / spot * 100) if (flip_strike and spot) else 0

    if abs(spot_vs_flip_pct) < 0.5:
        regime, regime_zh = "at_flip", "临界点 (regime 转换中)"
        regime_hint = "现价靠近 gamma flip · 突破一边就 vol 反转"
    elif spot > flip_strike:
        regime, regime_zh = "positive_pin", "正 GEX 抑波区"
        regime_hint = "dealer 卖涨买跌 → 短期区间震荡 · 卖 straddle/iron condor 有 edge"
    else:
        regime, regime_zh = "negative_squeeze", "负 GEX 放波区"
        regime_hint = "dealer 买涨卖跌 → 突破发动大波动 · 追高 or 追空都放大"

    return {
        "total_gex_millions":  round(total, 2),
        "by_strike":           by_strike,
        "gamma_flip_strike":   flip_strike,
        "spot_vs_flip_pct":    round(spot_vs_flip_pct, 2),
        "regime":              regime,
        "regime_zh":           regime_zh,
        "regime_hint":         regime_hint,
        "days_to_expiry":      days_to_expiry,
    }


def compute_iv_regime(calls_df, puts_df, spot: float, realized_vol_60d: Optional[float]) -> dict:
    """ATM IV vs 60d realized vol → premium/discount 判读."""
    if calls_df is None or calls_df.empty or spot <= 0:
        return {"error": "no_chain"}
    # 找 ATM (最近 spot 的 strike)
    all_strikes = sorted(set(list(calls_df["strike"].tolist()) + list(puts_df["strike"].tolist())))
    if not all_strikes:
        return {"error": "no_strikes"}
    atm = min(all_strikes, key=lambda k: abs(k - spot))
    c_atm = calls_df[calls_df["strike"] == atm]
    p_atm = puts_df[puts_df["strike"] == atm]
    ivs = []
    if not c_atm.empty:
        v = float(c_atm.iloc[0].get("impliedVolatility", 0) or 0)
        if v > 0: ivs.append(v)
    if not p_atm.empty:
        v = float(p_atm.iloc[0].get("impliedVolatility", 0) or 0)
        if v > 0: ivs.append(v)
    if not ivs:
        return {"error": "no_iv"}
    atm_iv = sum(ivs) / len(ivs)

    result = {
        "atm_strike":      atm,
        "atm_iv":          round(atm_iv, 4),
        "atm_iv_pct":      round(atm_iv * 100, 1),
        "realized_vol_60d_pct": round(realized_vol_60d * 100, 1) if realized_vol_60d else None,
    }

    if realized_vol_60d and realized_vol_60d > 0:
        premium_pct = (atm_iv / realized_vol_60d - 1) * 100
        result["iv_premium_pct"] = round(premium_pct, 1)
        if premium_pct >= 30:
            result["regime"], result["regime_zh"] = "crush_risk", "高 IV 挤压风险"
            result["regime_hint"] = "IV 已 rich · event 后即使方向对 option 也可能因 IV crush 亏钱"
        elif premium_pct >= 10:
            result["regime"], result["regime_zh"] = "elevated", "IV 偏高"
            result["regime_hint"] = "IV 略贵 · 买 option 需方向确定性大"
        elif premium_pct >= -10:
            result["regime"], result["regime_zh"] = "normal", "IV 合理"
            result["regime_hint"] = "IV 与实际波动匹配"
        else:
            result["regime"], result["regime_zh"] = "cheap", "IV 便宜期"
            result["regime_hint"] = "买保护 put / 上行 call 成本低 · hedge 便宜期"
    return result


def compute_skew(calls_df, puts_df, spot: float, atm_pct_range: float = 0.03) -> dict:
    """25-delta put / call IV skew (put IV OTM - call IV OTM).

    简化实现: 用 spot ±10% 附近的 put/call 平均 IV 代替真正 25d.
    """
    if calls_df is None or calls_df.empty or spot <= 0:
        return {"error": "no_chain"}
    # OTM call = strike > spot × (1 + ~5%) 附近
    # OTM put  = strike < spot × (1 - ~5%) 附近
    otm_call_range = (spot * 1.03, spot * 1.10)
    otm_put_range  = (spot * 0.90, spot * 0.97)

    def _avg_iv(df, lo, hi):
        subset = df[(df["strike"] >= lo) & (df["strike"] <= hi)]
        subset = subset[subset["impliedVolatility"] > 0]
        if subset.empty:
            return None
        # OI 加权平均，避免低成交 outlier 主导
        w = subset["openInterest"].fillna(1).clip(lower=1)
        iv = subset["impliedVolatility"]
        try:
            return float((iv * w).sum() / w.sum())
        except Exception:
            return float(iv.mean())

    put_iv  = _avg_iv(puts_df,  *otm_put_range)
    call_iv = _avg_iv(calls_df, *otm_call_range)
    if put_iv is None or call_iv is None:
        return {"error": "no_otm_iv"}

    skew_pct = (put_iv - call_iv) * 100
    ratio    = put_iv / call_iv if call_iv > 0 else None

    if skew_pct >= 10:
        regime, regime_zh = "steep_fear", "陡峭恐慌"
        regime_hint = "put tail 已重价 · 突破 put wall 加速 · 但 crash hedge cost 高"
    elif skew_pct >= 5:
        regime, regime_zh = "moderate", "中等 skew"
        regime_hint = "正常防御性定价"
    elif skew_pct >= 1:
        regime, regime_zh = "flat", "扁平"
        regime_hint = "市场 complacent · put wall 反映真实防御需求"
    else:
        regime, regime_zh = "call_skew", "call skew (少见)"
        regime_hint = "call 比 put 贵 · 可能有 gamma squeeze 期望"

    return {
        "otm_put_iv_pct":  round(put_iv * 100, 1),
        "otm_call_iv_pct": round(call_iv * 100, 1),
        "skew_pct":        round(skew_pct, 2),
        "ratio":           round(ratio, 3) if ratio else None,
        "regime":          regime,
        "regime_zh":       regime_zh,
        "regime_hint":     regime_hint,
    }


def generate_verdict(gex: dict, iv: dict, skew: dict, spot: float,
                     next_earnings_days: Optional[int] = None) -> dict:
    """把 3 组分析合成 1 line summary + risks[] + opportunities[]."""
    risks = []
    opportunities = []

    # === Risks ===
    if gex.get("regime") == "negative_squeeze":
        pct = gex.get("spot_vs_flip_pct", 0)
        risks.append({
            "level": "high",
            "text":  f"负 GEX 区，跌破 flip ${gex.get('gamma_flip_strike')} ({pct:+.1f}%) 加速下跌",
        })

    if iv.get("regime") == "crush_risk":
        prem = iv.get("iv_premium_pct", 0)
        earn_hint = f" · 财报仅 {next_earnings_days} 天" if next_earnings_days and next_earnings_days < 15 else ""
        risks.append({
            "level": "high",
            "text":  f"IV rich {prem:+.0f}% vs 60d RV{earn_hint} · event 后 crush 风险 (方向对也可能亏)",
        })

    if skew.get("regime") == "steep_fear":
        skew_pct = skew.get("skew_pct", 0)
        risks.append({
            "level": "medium",
            "text":  f"put skew +{skew_pct}% 陡峭 · tail 已定价 · 突破 put wall 杀伤大",
        })

    if next_earnings_days and next_earnings_days < 7:
        risks.append({
            "level": "high",
            "text":  f"财报仅剩 {next_earnings_days} 天 · IV crush + gap 双风险 · 追高 asymmetric",
        })

    # === Opportunities ===
    if gex.get("regime") == "positive_pin":
        risks_none_in_this = not any(r["level"] == "high" for r in risks)
        if risks_none_in_this:
            opportunities.append({
                "level": "medium",
                "text":  f"正 GEX pin · flip ${gex.get('gamma_flip_strike')} 稳固 · 卖 straddle/iron condor 有 edge",
            })

    if gex.get("regime") == "negative_squeeze":
        # Below flip: 反弹突破 flip 是关键信号
        opportunities.append({
            "level": "low",
            "text":  f"若突破 flip ${gex.get('gamma_flip_strike')} 上方 · dealer 转 long gamma 抑波 · 可轻仓追多",
        })

    if iv.get("regime") == "cheap":
        opportunities.append({
            "level": "medium",
            "text":  "IV 便宜期 · 买保护 put/上行 call 成本低 · hedge 或 lottery ticket 有 edge",
        })

    if iv.get("regime") == "crush_risk" and next_earnings_days and next_earnings_days < 30:
        opportunities.append({
            "level": "low",
            "text":  "财报后 IV 回归时卖 short strangle (需严格止损) · 高胜率低收益",
        })

    # === Summary (1 line 综合读) ===
    parts = []
    if gex.get("regime_zh"):
        parts.append(gex["regime_zh"])
    if iv.get("regime_zh"):
        parts.append(iv["regime_zh"])
    if skew.get("regime_zh") and skew.get("skew_pct", 0) > 5:
        parts.append(f"skew {skew['regime_zh']}")

    # 综合判断色调
    if any(r["level"] == "high" for r in risks) and len(risks) >= 2:
        overall = "⚠ 高风险窗口 · 避免追高"
        overall_cls = "bad"
    elif len(opportunities) > len(risks):
        overall = "✓ 结构友好 · 有机会窗口"
        overall_cls = "ok"
    elif len(risks) > len(opportunities):
        overall = "⚠ 中性偏空 · 观望等 vol regime 转换"
        overall_cls = "warn"
    else:
        overall = "➡ 中性 · 无明显 asymmetric edge"
        overall_cls = "muted"

    return {
        "summary":     f"{overall}  ·  {' + '.join(parts)}" if parts else overall,
        "overall":     overall,
        "overall_cls": overall_cls,
        "structure":   parts,
        "risks":       risks,
        "opportunities": opportunities,
    }


def full_analysis(calls_df, puts_df, spot: float, days_to_expiry: int,
                  realized_vol_60d: Optional[float] = None,
                  next_earnings_days: Optional[int] = None) -> dict:
    """入口: 全 3 层 + verdict 一次算完."""
    gex = compute_gex(calls_df, puts_df, spot, days_to_expiry)
    iv  = compute_iv_regime(calls_df, puts_df, spot, realized_vol_60d)
    sk  = compute_skew(calls_df, puts_df, spot)
    if "error" in gex or "error" in iv:
        return {
            "gex": gex, "iv": iv, "skew": sk,
            "error": gex.get("error") or iv.get("error"),
        }
    v = generate_verdict(gex, iv, sk, spot, next_earnings_days)
    return {
        "gex":  gex,
        "iv":   iv,
        "skew": sk,
        "verdict": v,
    }
