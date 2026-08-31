"""Short-horizon market-style classification.

This module deliberately answers a different question from ``hmm_regime``:
the HMM describes the slow SPY background, while this classifier measures
whether the last few sessions have been directional or directionless/choppy.
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


STYLE_LABELS = {
    "trend_up": "短线趋势偏强",
    "trend_down": "短线趋势偏弱",
    "pullback": "上升结构内回调",
    "rebound": "下降结构内反弹",
    "chop_bull": "震荡偏强",
    "chop": "横盘震荡",
    "chop_weak": "震荡偏弱",
    "mixed": "方向不明",
    "unknown": "数据不足",
}


def _series(values: Iterable[float] | pd.Series | None) -> pd.Series:
    if values is None:
        return pd.Series(dtype=float)
    out = pd.Series(values, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    return out.reset_index(drop=True)


def _finite(value, digits: int = 3):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def _efficiency(close: pd.Series, sessions: int) -> float | None:
    if len(close) < sessions + 1:
        return None
    window = close.tail(sessions + 1)
    path = float(window.diff().abs().sum())
    if path <= 0:
        return 0.0
    return float(abs(window.iloc[-1] - window.iloc[0]) / path)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    if min(len(high), len(low), len(close)) < period * 2:
        return None
    frame = pd.DataFrame({"high": high, "low": low, "close": close}).dropna()
    if len(frame) < period * 2:
        return None
    up_move = frame["high"].diff()
    down_move = -frame["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index,
    )
    prev_close = frame["close"].shift()
    tr = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    value = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    return float(value) if pd.notna(value) and math.isfinite(float(value)) else None


def analyze_price_style(close, high=None, low=None) -> dict:
    """Return a transparent short-horizon trend/chop classification.

    A chop score of 3+ means at least several independent symptoms agree:
    inefficient net movement, low ADX, frequent return reversals, repeated
    MA20 crossings, or a small net move despite a non-trivial travelled path.
    ATR expansion is supporting evidence, not a mandatory condition.
    """
    close_s = _series(close)
    high_s = _series(high)
    low_s = _series(low)
    if len(close_s) < 21:
        return {
            "style": "unknown",
            "style_zh": STYLE_LABELS["unknown"],
            "chop_score": 0,
            "is_choppy": False,
            "reason": "至少需要21个日线收盘价",
            "metrics": {},
            "policy_note": "数据不足，不据此调整仓位",
        }

    price = float(close_s.iloc[-1])
    pct_5d = (price / float(close_s.iloc[-6]) - 1) * 100
    pct_20d = (price / float(close_s.iloc[-21]) - 1) * 100
    ma20 = float(close_s.tail(20).mean())
    ma50 = float(close_s.tail(50).mean()) if len(close_s) >= 50 else None
    dist_ma20 = (price / ma20 - 1) * 100 if ma20 else None
    dist_ma50 = (price / ma50 - 1) * 100 if ma50 else None
    efficiency_10 = _efficiency(close_s, 10)
    efficiency_20 = _efficiency(close_s, 20)

    returns = close_s.pct_change().dropna().tail(10)
    signs = np.sign(returns.to_numpy())
    signs = signs[signs != 0]
    reversal_count = int(np.sum(signs[1:] != signs[:-1])) if len(signs) > 1 else 0

    ma20_series = close_s.rolling(20).mean()
    relation = np.sign((close_s - ma20_series).dropna().tail(10).to_numpy())
    relation = relation[relation != 0]
    ma20_crossings = int(np.sum(relation[1:] != relation[:-1])) if len(relation) > 1 else 0

    adx_14 = None
    atr_ratio = None
    if len(high_s) == len(close_s) and len(low_s) == len(close_s):
        adx_14 = _adx(high_s, low_s, close_s)
        prev_close = close_s.shift()
        tr = pd.concat(
            [high_s - low_s, (high_s - prev_close).abs(), (low_s - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr20 = float(tr.tail(20).mean())
        atr5 = float(tr.tail(5).mean())
        atr_ratio = atr5 / atr20 if atr20 > 0 else None

    score = 0
    symptoms = []
    if efficiency_10 is not None and efficiency_10 < 0.20:
        score += 2
        symptoms.append(f"10日趋势效率很低({efficiency_10:.2f})")
    elif efficiency_10 is not None and efficiency_10 < 0.35:
        score += 1
        symptoms.append(f"10日趋势效率偏低({efficiency_10:.2f})")
    if adx_14 is not None and adx_14 < 18:
        score += 1
        symptoms.append(f"ADX偏低({adx_14:.1f})")
    if reversal_count >= 5:
        score += 1
        symptoms.append(f"10日涨跌反转{reversal_count}次")
    if ma20_crossings >= 2:
        score += 1
        symptoms.append(f"10日穿越MA20 {ma20_crossings}次")
    if abs(pct_5d) < 2 and abs(pct_20d) < 5 and (efficiency_20 or 0) < 0.35:
        score += 1
        symptoms.append("净涨跌小但路径反复")
    if atr_ratio is not None and atr_ratio > 1.20:
        score += 1
        symptoms.append(f"短期ATR放大({atr_ratio:.2f}x)")

    is_choppy = score >= 3
    if is_choppy:
        if pct_5d <= -0.75 or (dist_ma20 is not None and dist_ma20 < 0 and pct_20d < 0):
            style = "chop_weak"
        elif pct_5d >= 0.75 and dist_ma20 is not None and dist_ma20 > 0:
            style = "chop_bull"
        else:
            style = "chop"
    elif pct_20d >= 2 and dist_ma20 is not None and dist_ma20 > 0:
        style = "trend_up" if pct_5d >= 0 else "pullback"
    elif pct_20d <= -2 and dist_ma20 is not None and dist_ma20 < 0:
        style = "trend_down" if pct_5d <= 0 else "rebound"
    else:
        style = "mixed"

    if is_choppy:
        policy_note = "短线震荡：提高加仓门槛、降低目标波动，不把宏观偏多当追涨依据"
    elif style in ("trend_down", "pullback"):
        policy_note = "短线承压：等待价格与动量重新确认"
    elif style == "trend_up":
        policy_note = "短线趋势尚可，但仍需个股/期权流确认"
    else:
        policy_note = "方向不明：按中性环境处理"

    metrics = {
        "pct_5d": _finite(pct_5d, 2),
        "pct_20d": _finite(pct_20d, 2),
        "dist_ma20_pct": _finite(dist_ma20, 2),
        "dist_ma50_pct": _finite(dist_ma50, 2),
        "efficiency_10": _finite(efficiency_10),
        "efficiency_20": _finite(efficiency_20),
        "adx_14": _finite(adx_14, 1),
        "reversal_count_10d": reversal_count,
        "ma20_crossings_10d": ma20_crossings,
        "atr_5_20_ratio": _finite(atr_ratio),
    }
    return {
        "style": style,
        "style_zh": STYLE_LABELS[style],
        "chop_score": score,
        "is_choppy": is_choppy,
        "reason": "；".join(symptoms) if symptoms else "未出现明确震荡共振",
        "metrics": metrics,
        "policy_note": policy_note,
    }


def effective_board_regime(base_regime: str, style: dict) -> str:
    """Overlay short-term chop on the daily trading premise, never on HMM.

    覆盖全部 8 种 base_regime 的 chop overlay:
    - bull/pulling/extended: 加 chop tag 但保留 bull 语义 (震荡不改牛市前提)
    - neutral: neutral_chop (震荡横盘)
    - risk_off / recession_risk: chop 不改防御 (震荡的防御 = 更防御)
    - crisis / overheated: 极端情况 chop 无意义, 保留原语义
    """
    if not style.get("is_choppy"):
        return base_regime
    _CHOP_OVERLAY = {
        "bull_trending":  "bull_chop",
        "bull_pulling":   "bull_chop",   # 回调期 + chop = 震荡回调, 按 bull_chop 对待
        "bull_extended":  "bull_chop",   # 强延续 + chop = 顶部震荡, 降级到 bull_chop
        "neutral":        "neutral_chop",
        "risk_off":       "risk_off",    # 已防御, chop 不加码
        "recession_risk": "recession_risk",  # 同上
        "crisis":         "crisis",      # 极端场景 chop 语义已被包含
        "overheated":     "overheated",  # 已提示顶部风险, chop 不重复
    }
    return _CHOP_OVERLAY.get(base_regime, base_regime)
