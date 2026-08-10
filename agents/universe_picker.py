"""
UniversePicker — 每日卫星仓候选池筛选。

基于 pca_sox 已经算好的：
  · 残差动量 z 分数 (剔除真信号因子后)
  · 5 因子回归的 α + t 统计量
  · 当前 regime

按 4a 严格 regime 矩阵筛选 + 3b "|z|>2 且 |α-t|>2" 双门槛 + 2b top 5。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import pandas as pd

from pca_sox import (
    SOX_TICKERS, SOX_THEME,
    fetch_returns, compute_spectrum,
    compute_residual_momentum, compute_factors, run_factor_regression,
)
from config import is_sim_active_trading

# ── 入选门槛（3b 调整版：α-t 放宽到 1.5，避免单只 picks）──────────────────
MIN_ABS_Z       = 2.0    # |residual momentum z| 下限
MIN_ABS_ALPHA_T = 1.5    # |alpha t-stat| 下限（边缘显著也算，原 2.0 太严）
MAX_PICKS       = 5      # 2b: top 5 卫星

# ── 仓位预览（实际下单由 paper_trader._position_size_usd 用账户购买力实时算）──
# 这里只是给 picks 显示用一个 baseline 估算；用 $1.5M 假设的账户购买力
PREVIEW_POWER_USD = 1_500_000.0
PREVIEW_FRAC_BASE = 0.10        # 与 paper_trader.POSITION_FRACTION_BASE 一致
PREVIEW_FRAC_MAX  = 0.15        # 与 paper_trader.POSITION_FRACTION_MAX 一致

# 全市场上下文用：核心仓白名单（不参与动态选股）
CORE_TICKERS = {"US.TQQQ", "US.SOXL", "US.GLD"}


def size_for(z: float) -> float:
    """预览：base = 10% 账户购买力，z 升高加到 15%。trader 真下单时用实际购买力重算。"""
    if z is None:
        z = MIN_ABS_Z
    frac = PREVIEW_FRAC_BASE + max(0.0, abs(z) - MIN_ABS_Z) * 0.025
    frac = min(frac, PREVIEW_FRAC_MAX)
    return round(PREVIEW_POWER_USD * frac, 0)


def _direction_for_regime(z: float, regime: str) -> Optional[str]:
    """
    4a 严格按 regime 矩阵决定方向：
      bull_trending  → 高 z 顺动量 (BUY)，低 z 不要
      overheated     → 高 z 反转减仓 (SKIP，反转风险高)，低 z 反弹候选 (BUY)
      recession_risk → 仅 |z|>2.5 才动；偏避险
      crisis         → 现金为王，全空
      neutral        → 仅 |z|>3 才动
    返回 'BUY' 或 None（不选）。
    """
    if regime == "crisis":
        return None
    if regime == "bull_trending":
        return "BUY" if z >= MIN_ABS_Z else None
    if regime == "overheated":
        return "BUY" if z <= -MIN_ABS_Z else None  # 反弹候选
    if regime == "recession_risk":
        if z <= (-2.0 if is_sim_active_trading() else -2.5):
            return "BUY"   # 极端超卖反转
        return None
    # neutral
    if is_sim_active_trading():
        return "BUY" if z >= 1.5 else None
    if abs(z) >= 3.0:
        return "BUY" if z > 0 else None  # 中性 regime 也只追多
    return None


def _detect_regime() -> str:
    """从 regime_today (系统级单一源) 读今日 regime。fallback 'neutral'。"""
    try:
        from regime_today import get_today_regime
        return get_today_regime()
    except Exception:
        return "neutral"


def pick_today_universe(regime: Optional[str] = None) -> dict:
    """
    主入口：拉数据、跑因子、按门槛+regime 筛选 → 返回 picks。
    返回 dict:
      {
        "regime": str,
        "ts":     ISO 时间戳,
        "picks":  [{ticker, z, alpha_t, theme, size_usd, hint}, ...],  # 至多 MAX_PICKS
        "skipped":[{ticker, z, alpha_t, reason}, ...],                # 被门槛过滤掉的
        "n_universe": int,
        "error":  str | None,
      }
    """
    out = {
        "regime": regime, "ts": datetime.now().isoformat(),
        "picks": [], "skipped": [], "n_universe": 0, "error": None,
    }
    if regime is None:
        regime = _detect_regime()
        out["regime"] = regime

    # crisis：直接返回空
    if regime == "crisis":
        out["error"] = "crisis regime: no dynamic picks"
        return out

    returns = fetch_returns()
    if returns is None or returns.empty:
        out["error"] = "fetch_returns failed"
        return out
    spec = compute_spectrum(returns)
    res_mom = compute_residual_momentum(returns, spec, lookback=20)
    factors = compute_factors(returns)

    cum_z = res_mom["cumulative_z"]
    tickers = res_mom["tickers"]
    out["n_universe"] = len(tickers)

    # 对每只票算 alpha + t-stat
    candidates = []
    for i, tk in enumerate(tickers):
        z = float(cum_z[i])
        try:
            reg = run_factor_regression(returns[tk], factors)
            alpha_t = float(reg["alpha_t"])
            alpha_ann = float(reg["alpha"]) * 252 * 100  # 年化 %
        except Exception:
            alpha_t = 0.0
            alpha_ann = 0.0
        candidates.append({
            "ticker": tk, "z": z, "alpha_t": alpha_t,
            "alpha_ann_pct": round(alpha_ann, 2),
            "theme": SOX_THEME.get(tk, "其他"),
        })

    # 应用门槛：方向匹配 + |z|>=MIN_Z + |alpha_t|>=MIN_AT
    def _filter(min_z: float, min_at: float):
        qual, skip = [], []
        for c in candidates:
            direction = _direction_for_regime(c["z"], regime)
            z_ok = abs(c["z"]) >= min_z
            a_ok = abs(c["alpha_t"]) >= min_at
            if direction and z_ok and a_ok:
                qual.append(c)
            else:
                reason = []
                if direction is None: reason.append(f"regime={regime} 方向不符")
                if not z_ok: reason.append(f"|z|={abs(c['z']):.2f}<{min_z}")
                if not a_ok: reason.append(f"|αt|={abs(c['alpha_t']):.2f}<{min_at}")
                if abs(c["z"]) >= min_z or abs(c["alpha_t"]) >= min_at:
                    skip.append({**c, "reason": "; ".join(reason)})
        return qual, skip

    # 严格模式：双重显著。模拟积极模式降低候选门槛，但仍要求正向残差动量。
    min_z = 1.5 if is_sim_active_trading() else MIN_ABS_Z
    min_alpha_t = 1.0 if is_sim_active_trading() else MIN_ABS_ALPHA_T
    out["selection_thresholds"] = {
        "min_abs_z": min_z,
        "min_abs_alpha_t": min_alpha_t,
        "sim_active": is_sim_active_trading(),
    }
    qualified, skipped = _filter(min_z, min_alpha_t)
    pick_mode = "strict"
    # B (自适应)：严格 0 picks → 降级到纯动量（只看 |z|，不要求 α-t 显著）
    if not qualified:
        qualified, skipped = _filter(min_z, 0.0)
        pick_mode = "fallback-z-only"
    out["pick_mode"] = pick_mode

    # 排序：bull_trending / neutral 按 z 降序；overheated / recession 按 |z| 降序（反转候选）
    if regime in ("bull_trending", "neutral"):
        qualified.sort(key=lambda c: -c["z"])
    else:
        qualified.sort(key=lambda c: -abs(c["z"]))

    picks = qualified[:MAX_PICKS]
    for p in picks:
        # 卫星票统一加 US. 前缀（与 moomoo ticker 规范一致）
        full_tk = p["ticker"] if p["ticker"].startswith("US.") else f"US.{p['ticker']}"
        p["ticker_full"]  = full_tk
        p["size_usd"]     = size_for(p["z"])
        p["hint"]         = _hint(p["z"], regime)

    out["picks"]   = picks
    out["skipped"] = skipped[:10]   # 最多展示 10 条
    return out


def _hint(z: float, regime: str) -> str:
    if regime == "bull_trending":
        return f"顺动量跟仓 (z={z:+.2f}σ)"
    if regime == "overheated":
        return f"反弹候选 (z={z:+.2f}σ)"
    if regime == "recession_risk":
        return f"超卖反转 (z={z:+.2f}σ)"
    return f"中性极端 (z={z:+.2f}σ)"


def format_picks_report(out: dict) -> list[str]:
    """生成可塞进 logger 的多行报告。"""
    W = 76
    lines = ["+" + "=" * (W - 2) + "+",
             f"|  今日卫星仓候选池  |  regime={out['regime']}"
             f"  ts={out['ts'][:19]}".ljust(W - 1) + "|",
             "+" + "=" * (W - 2) + "+"]
    if out.get("error"):
        lines.append(f"  [跳过] {out['error']}")
        lines.append("=" * W)
        return lines
    picks = out.get("picks", [])
    mode  = out.get("pick_mode", "strict")
    thresholds = out.get("selection_thresholds") or {}
    min_z = thresholds.get("min_abs_z", MIN_ABS_Z)
    min_alpha_t = thresholds.get("min_abs_alpha_t", MIN_ABS_ALPHA_T)
    mode_label = {
        "strict":          f"严格 (|z|>={min_z} 且 |α-t|>={min_alpha_t})",
        "fallback-z-only": f"降级 (|z|>={min_z}, α-t 不要求) — 严格模式 0 picks 触发",
    }.get(mode, mode)
    lines.append(f"  全市场: {out['n_universe']} 只SOX成分股 "
                 f"→ 入选 {len(picks)} / 上限 {MAX_PICKS}  [模式: {mode_label}]")
    if not picks:
        lines.append("  (即使降级到纯动量也无标的: 可能因 regime=crisis 或所有票方向不符)")
    else:
        lines.append(f"  {'#':<3} {'ticker':<10} {'z(σ)':>7} {'α-t':>6} "
                     f"{'α(年化%)':>10} {'$':>6}  {'主题':<14} 提示")
        lines.append(f"  {'-'*3} {'-'*10} {'-'*7} {'-'*6} {'-'*10} {'-'*6}  {'-'*14} ----")
        for i, p in enumerate(picks, 1):
            lines.append(
                f"  {i:<3} {p['ticker_full']:<10} {p['z']:>+6.2f} {p['alpha_t']:>+6.2f} "
                f"{p['alpha_ann_pct']:>+9.2f}% ${p['size_usd']:>5.0f}  "
                f"{p['theme']:<14} {p['hint']}"
            )
    if out.get("skipped"):
        lines.append("")
        lines.append("  差一点入选 (供参考):")
        for s in out["skipped"][:5]:
            lines.append(f"    {s['ticker']:<6} z={s['z']:+.2f} α-t={s['alpha_t']:+.2f}  "
                         f"reason: {s['reason']}")
    lines.append("=" * W)
    return lines


if __name__ == "__main__":
    res = pick_today_universe()
    for line in format_picks_report(res):
        print(line)
