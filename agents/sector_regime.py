"""sector_regime.py — 板块级 regime 分类的**单一实现**.

之前 decision_agent._get_sector_regime + webui._compute_sectors 各算一套,
label 词汇不同, 阈值近似但独立维护 → 改一处漂移. 现统一到这里.

用法:
  from sector_regime import classify_sector
  info = classify_sector("SMH")    # 拉 yfinance 3mo daily, 缓存 15min
  # info = {"regime": "sector_pullback", "regime_full": "pullback", "regime_zh": "回调",
  #         "pct_5d": -3.2, "pct_20d": +1.1, "dist_ma20_pct": -1.5, "trend": "down",
  #         "style": {...}}

两套 label 体系:
  · regime_full: 用于 dashboard 展示 (含 upside: bear/crisis/weak/pullback/neutral/strong/overheated)
  · regime:     用于 decision_agent 只做 downward tightening
                (映射: crisis→sector_crisis, weak→sector_weak, pullback→sector_pullback,
                       bear→sector_bear, 其它 None; chop 单独判)

阈值来自旧 webui 版本 (与 decision_agent 相同, 但更完整):
  pct_20d ≤ -10 → bear
  pct_5d  ≤ -5  → crisis
  pct_20d ≤ -5  → weak
  pct_5d  ≤ -2  → pullback
  pct_5d  ≥ +5 AND pct_20d ≥ +10 → overheated
  pct_5d  ≥ +2 AND pct_20d ≥ +2  → strong
  其它 → neutral (再叠 style.is_choppy → chop)

未来若阈值要改, **只改这里**, decision_agent + webui 自动跟进.
"""
from __future__ import annotations

import time
from typing import Optional

_CACHE: dict = {}
_CACHE_TTL_SEC = 900   # 15 min

# label 映射: full → decision_agent 用的 "sector_xxx" (仅 downward)
_TO_DECISION_LABEL = {
    "bear":     "sector_bear",
    "crisis":   "sector_crisis",
    "weak":     "sector_weak",
    "pullback": "sector_pullback",
    # neutral / strong / overheated → None (decision 只收紧, 不放宽)
}

_ZH_LABEL = {
    "bear":       "技术熊",
    "crisis":     "危机（单日大跌）",
    "weak":       "弱势",
    "pullback":   "回调",
    "neutral":    "中性",
    "strong":     "强势",
    "overheated": "过热",
    "chop":       "震荡",
}


def _classify_full(pct_5d: Optional[float], pct_20d: Optional[float],
                    is_choppy: bool) -> str:
    """核心阈值判定. 返回 full label."""
    if pct_20d is not None and pct_20d <= -10:
        return "bear"
    if pct_5d is not None and pct_5d <= -5:
        return "crisis"
    if pct_20d is not None and pct_20d <= -5:
        return "weak"
    if pct_5d is not None and pct_5d <= -2:
        return "pullback"
    if (pct_5d is not None and pct_20d is not None
            and pct_5d >= 5 and pct_20d >= 10):
        return "overheated"
    if (pct_5d is not None and pct_20d is not None
            and pct_5d >= 2 and pct_20d >= 2):
        return "strong"
    return "chop" if is_choppy else "neutral"


def classify_sector(sector_etf: str) -> Optional[dict]:
    """拉 yfinance 3mo daily → 计算 sector regime. 15min 缓存."""
    if not sector_etf:
        return None
    now = time.time()
    hit = _CACHE.get(sector_etf)
    if hit and (now - hit["ts"] < _CACHE_TTL_SEC):
        return hit["data"]

    try:
        import yfinance as yf
        df = yf.Ticker(sector_etf).history(period="3mo", interval="1d", auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty or len(df) < 21:
        return None

    close = df["Close"].astype(float)
    price = float(close.iloc[-1])
    pct_5d  = ((price / float(close.iloc[-6])  - 1) * 100)
    pct_20d = ((price / float(close.iloc[-21]) - 1) * 100)
    ma20    = float(close.tail(20).mean())
    trend   = "up" if price > ma20 else "down"
    dist_ma20_pct = ((price - ma20) / ma20 * 100) if ma20 else None

    try:
        from market_style import analyze_price_style
        style = analyze_price_style(close, df["High"], df["Low"])
    except Exception:
        style = {"is_choppy": False}
    is_choppy = bool(style.get("is_choppy"))

    full = _classify_full(pct_5d, pct_20d, is_choppy)
    decision = _TO_DECISION_LABEL.get(full)
    # neutral + choppy → sector_chop (decision agent 需要知道震荡)
    if decision is None and full == "chop":
        decision = "sector_chop"

    result = {
        "regime":        decision,        # for decision_agent (None or "sector_xxx")
        "regime_full":   full,            # for webui display
        "regime_zh":     _ZH_LABEL.get(full, full),
        "price":         round(price, 2),
        "pct_5d":        round(pct_5d, 2),
        "pct_20d":       round(pct_20d, 2),
        "dist_ma20_pct": round(dist_ma20_pct, 2) if dist_ma20_pct is not None else None,
        "trend":         trend,
        "style":         style,
    }
    _CACHE[sector_etf] = {"ts": now, "data": result}
    return result


def classify_ticker_sector(ticker: str, ticker_to_sector: dict) -> Optional[str]:
    """decision_agent 用: ticker → sector_etf → regime (仅 downward label)."""
    sector = ticker_to_sector.get(ticker)
    if not sector:
        return None
    info = classify_sector(sector)
    return info["regime"] if info else None
