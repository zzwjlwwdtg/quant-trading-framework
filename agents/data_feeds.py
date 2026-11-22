"""
DataFeeds — 公开 API 数据聚合，零 API Key 要求。
数据源: Yahoo Finance (yfinance) + CNN Fear & Greed

EVO-20261122-2: 增加 symbol 市场前缀路由表。
非美市场符号 (.T/.TW/.HK/.SS/.SZ 等) 应优先走 moomoo_data，
Yahoo Finance（尤其 quoteSummary）对这些市场符号静默返回 404。
"""

import urllib.request
import json
import yfinance as yf

# ── 非美市场符号格式白名单 ──────────────────────────────────────────────────
# 后缀 → 市场标识（供路由决策使用）
_NON_US_SUFFIX_MARKET: dict[str, str] = {
    ".T":   "JP",    # 东京证券交易所 (日本)
    ".TW":  "TW",    # 台湾证券交易所
    ".HK":  "HK",    # 香港交易所
    ".SS":  "SH",    # 上海证券交易所 (A股)
    ".SZ":  "SZ",    # 深圳证券交易所 (A股)
    ".KS":  "KR",    # 韩国证券交易所
    ".KQ":  "KR",    # 韩国 KOSDAQ
    ".AX":  "AU",    # 澳大利亚证券交易所
}
# 已经带 openD 市场前缀的格式不走 Yahoo
_OPEND_PREFIXES = ("JP.", "HK.", "SH.", "SZ.", "TW.", "KR.", "AU.")


def is_yahoo_safe(ticker: str) -> bool:
    """返回 False 表示该符号不应请求 Yahoo quoteSummary / download。

    非美市场符号（.T/.TW/.HK/.SS 等）对 Yahoo quoteSummary 会静默 404，
    这类符号应走 moomoo_data 或明确的异常路径。
    """
    if not ticker:
        return True
    t = ticker.strip().upper()
    for sfx in _NON_US_SUFFIX_MARKET:
        if t.endswith(sfx):
            return False
    for pfx in _OPEND_PREFIXES:
        if t.startswith(pfx):
            return False
    return True


def get_datasource_for(ticker: str) -> str:
    """返回符号应使用的数据源名称。

    "yahoo"   — 美股/指数/期货，可用 yfinance 拉取
    "moomoo"  — 非美市场已知符号格式，应走 moomoo_data
    "unknown" — 格式不在白名单，应走明确异常路径并记录 data_quality 事件
    """
    if not ticker:
        return "unknown"
    t = ticker.strip().upper()
    for sfx in _NON_US_SUFFIX_MARKET:
        if t.endswith(sfx):
            return "moomoo"
    for pfx in _OPEND_PREFIXES:
        if t.startswith(pfx):
            return "moomoo"
    # 美股/指数/期货/美元格式（字母+数字, 允许 ^ = - . 等特殊字符）
    # 长度超 20 通常意味着格式异常
    if len(t) <= 20:
        return "yahoo"
    return "unknown"


def infer_market(ticker: str) -> str | None:
    """推断 ticker 所属市场，未知返回 None。

    "US" / "JP" / "TW" / "HK" / "SH" / "SZ" / ...
    """
    if not ticker:
        return None
    t = ticker.strip().upper()
    for pfx in _OPEND_PREFIXES:
        if t.startswith(pfx):
            return pfx.rstrip(".")
    for sfx, mkt in _NON_US_SUFFIX_MARKET.items():
        if t.endswith(sfx):
            return mkt
    return "US"


WATCHLIST = {
    "TQQQ":      "3x纳指",
    "SOXL":      "3x半导体",
    "GLD":       "黄金ETF",
    "QQQ":       "纳斯达克100",
    "SPY":       "标普500",
    "^VIX":      "VIX恐慌",
    "^TNX":      "10Y美债",
    "BTC-USD":   "比特币",
    "CL=F":      "WTI原油",
    "DX-Y.NYB":  "美元指数",
}


def fetch_market_data() -> dict:
    """通过 yfinance 获取全部标的最新行情。"""
    result = {}
    for sym, name in WATCHLIST.items():
        try:
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist) < 2:
                raise ValueError("data too short")
            price   = float(hist["Close"].iloc[-1])
            prev    = float(hist["Close"].iloc[-2])
            vol     = float(hist["Volume"].iloc[-1])
            avg_vol = float(hist["Volume"].iloc[-5:-1].mean()) if len(hist) >= 5 else vol
            result[sym] = {
                "name":      name,
                "price":     round(price, 2),
                "pct_chg":   round((price - prev) / prev * 100, 2),
                "vol_ratio": round(vol / avg_vol, 2) if avg_vol > 0 else None,
            }
        except Exception as e:
            result[sym] = {"name": name, "price": None, "pct_chg": None, "error": str(e)}
    return result


def fetch_fear_greed() -> dict:
    """
    CNN Fear & Greed Index (0=极度恐惧, 100=极度贪婪)。
    CNN 用 418 屏蔽 default User-Agent，必须带 Referer/Origin 才放行。
    """
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.cnn.com/",
            "Origin":          "https://www.cnn.com",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = json.load(r)
        fg = raw["fear_and_greed"]
        score = round(float(fg["score"]))
        prev  = round(float(fg.get("previous_close", fg["score"])))
        return {
            "score":      score,
            "rating":     fg["rating"],
            "prev_close": prev,
            "delta":      score - prev,
        }
    except Exception as e:
        return {"score": None, "rating": "N/A", "error": str(e)}


def fetch_vix() -> float | None:
    """只拉VIX最新收盘价，用于15分钟信号周期的宏观快照。"""
    try:
        hist = yf.Ticker("^VIX").history(period="2d")
        return float(hist["Close"].iloc[-1]) if len(hist) >= 1 else None
    except Exception:
        return None


def fetch_dxy(period: str = "10d") -> dict:
    """拉 DXY（美元指数）含近期趋势。黄金宏观分析核心：USD↑ → GLD↓。
    返回 {value, value_5d_ago, pct_chg_5d, pct_chg_20d}。"""
    try:
        hist = yf.Ticker("DX-Y.NYB").history(period=period)
        if len(hist) < 1:
            return {}
        latest = float(hist["Close"].iloc[-1])
        out = {"value": latest}
        if len(hist) >= 5:
            v5 = float(hist["Close"].iloc[-5])
            out["value_5d_ago"] = v5
            out["pct_chg_5d"] = (latest - v5) / v5 * 100
        if len(hist) >= 20:
            v20 = float(hist["Close"].iloc[-20])
            out["pct_chg_20d"] = (latest - v20) / v20 * 100
        return out
    except Exception:
        return {}


def vix_label(v) -> str:
    if v is None: return "N/A"
    if v < 15:   return "极度平静（过度乐观⚠️）"
    if v < 20:   return "平静"
    if v < 25:   return "警戒"
    if v < 30:   return "高度警戒🔶"
    return           "恐慌🔴"


def fg_label(s) -> str:
    if s is None: return "N/A"
    if s < 25:  return "极度恐惧🟢（可能是买入机会）"
    if s < 45:  return "恐惧"
    if s < 55:  return "中性"
    if s < 75:  return "贪婪"
    return          "极度贪婪🔴（警惕回调）"


def yield_label(y) -> str:
    if y is None: return "N/A"
    if y < 3.5:  return "低利率，有利成长股"
    if y < 4.0:  return "中性"
    if y < 4.5:  return "偏高，压制高估值股⚠️"
    return           "高位，显著压制成长股🔴"
