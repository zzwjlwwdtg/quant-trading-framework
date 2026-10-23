"""commodity_overlay.py — GC=F / CL=F 24h futures 数据补 GLD/USO 之外的时段.

GLD/USO 只在 NYSE 时段（9:30-16:00 ET）交易，
夜盘 extended-hours 流动性稀薄，抓不到 Asia/London 时段的 gold/oil 移动。
而 gold futures (GC=F) 和 WTI futures (CL=F) 是 23h 市场，
才是真实的"通宵行情"来源——尤其：
  · Asia 时段央行购金 (上海/东京)
  · London AM/PM Fix (14:30/22:00 UTC)
  · OPEC 深夜 headline
  · 中东地缘事件（多在美股收盘后）

用途：给 GLD/USO 的 market_watch snapshot 覆盖 overnight_pct 字段，
让 confluence.py 的 [夜盘] 信号能反映真实商品期货的隔夜动量。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── ticker → futures symbol 映射 ─────────────────────────────────
UNDERLYING_MAP = {
    "GLD": "GC=F",   # COMEX gold futures
    "USO": "CL=F",   # NYMEX WTI crude futures
    # 后续可加：SLV → SI=F 白银 / DBA → 农产品 / SPY 已有 ES=F
}

# 缓存 5 min（futures 15min bar 数据变化慢，避免多 ticker 重复拉）
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300.0


def _fetch_futures_change(symbol: str) -> Optional[dict]:
    """拉 yfinance 24h 期货数据，算 last-24h 变化。返回 {price, pct_24h, pct_since_us_close, source}."""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(symbol)
        # 5min bar × 2d 覆盖 24h + margin
        hist = t.history(period="2d", interval="5m", prepost=False)
        if hist is None or hist.empty or len(hist) < 12:
            return None
        cur = float(hist["Close"].iloc[-1])
        # 24h ago 对应 288 根 5min bar (24 × 12)，取有效样本
        idx_24h = max(0, len(hist) - 288)
        ago = float(hist["Close"].iloc[idx_24h])
        pct_24h = round((cur - ago) / ago * 100, 2) if ago else 0.0

        # 找上一个 US NYSE close (16:00 ET = 21:00 UTC 冬令 / 20:00 UTC 夏令) 附近的价
        # 简化：找最接近今日 21:00 UTC 之前的 bar
        now_utc = datetime.now(timezone.utc)
        us_close_utc = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
        if now_utc < us_close_utc:
            us_close_utc -= timedelta(days=1)
        # yfinance 5m bar 索引是 tz-aware
        hist_close = hist.index
        try:
            mask = hist_close <= us_close_utc
            if mask.any():
                us_close_price = float(hist.loc[mask, "Close"].iloc[-1])
            else:
                us_close_price = ago
        except Exception:
            us_close_price = ago
        pct_since_close = round((cur - us_close_price) / us_close_price * 100, 2) if us_close_price else 0.0

        return {
            "price": round(cur, 2),
            "pct_24h": pct_24h,
            "pct_since_us_close": pct_since_close,
            "source": symbol,
            "ts": now_utc.isoformat(timespec="seconds"),
        }
    except Exception:
        return None


def get_commodity_overnight(ticker: str) -> Optional[dict]:
    """
    ticker: 'GLD' | 'USO' | 'US.GLD' 等；无映射返回 None
    返回: {price, pct_24h, pct_since_us_close, source, ts} 或 None
    """
    tk = ticker.replace("US.", "") if ticker else ""
    symbol = UNDERLYING_MAP.get(tk)
    if not symbol:
        return None
    now = time.time()
    cached = _CACHE.get(symbol)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    data = _fetch_futures_change(symbol)
    if data:
        _CACHE[symbol] = (now, data)
    return data


def enrich_market_snapshot(mkt: dict) -> dict:
    """给 market_watch 返回的 dict 加 commodity_futures 字段 + 覆盖 overnight_pct（若能获取）。
    幂等：非商品 ticker / 拉取失败 直接返回原 dict。"""
    if not mkt or not isinstance(mkt, dict):
        return mkt
    ticker = mkt.get("ticker", "")
    ov = get_commodity_overnight(ticker)
    if not ov:
        return mkt
    mkt["commodity_futures"] = ov
    # 覆盖 overnight_pct：yfinance/moomoo 的 GLD extended-hours 太稀薄，
    # 期货 24h 变化更能反映真实"通宵动量"
    old_on_pct = mkt.get("overnight_pct")
    mkt["overnight_pct"] = ov["pct_since_us_close"]
    mkt["overnight_source"] = ov["source"]
    mkt["overnight_label_raw"] = mkt.get("overnight_label", "夜盘")
    # 若期货变化明显但 label 未 forward-looking，强制标 "进行中" 让 confluence 计入
    if abs(ov["pct_since_us_close"]) >= 0.5:
        mkt["overnight_label"] = f"{ov['source']} 24h进行中"
    mkt["_overnight_pct_prev"] = old_on_pct
    return mkt


if __name__ == "__main__":
    import json
    print(json.dumps(get_commodity_overnight("GLD"), indent=2))
    print(json.dumps(get_commodity_overnight("USO"), indent=2))
