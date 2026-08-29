"""
EventsWatchAgent — Zero LLM tokens.
Checks hardcoded macro calendar + Yahoo Finance RSS for breaking headlines.
Supports two modes: equity (TQQQ/SOXL) and gold (GCmain).
Inspired by QuantConnect's Risk Model: event proximity → risk multiplier.
"""

import json
import os
import hashlib
import email.utils
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path

from config import SIGNALS_DIR
from i18n import t

# 新闻去重持久化：避免同一突发新闻在多个 cycle 反复触发 breaking_news
_NEWS_SEEN_PATH = Path(SIGNALS_DIR) / "news_seen.json"
_NEWS_TTL_HOURS = 12  # 12 小时内出现过的新闻不再算"突发"


def _hash_news(item: dict) -> str:
    """根据 URL（无 URL 则用标题）生成稳定指纹。"""
    key = (item.get("url") or item.get("title") or "").strip().lower()
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def _load_seen() -> dict:
    """{hash: iso_timestamp} ；过期条目顺手清理。"""
    if not _NEWS_SEEN_PATH.exists():
        return {}
    try:
        data = json.loads(_NEWS_SEEN_PATH.read_text(encoding="utf-8"))
        cutoff = datetime.now() - timedelta(hours=_NEWS_TTL_HOURS)
        return {h: ts for h, ts in data.items()
                if datetime.fromisoformat(ts) > cutoff}
    except Exception:
        return {}


def _save_seen(seen: dict) -> None:
    try:
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        _NEWS_SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    except Exception:
        pass


def _filter_fresh_news(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    将 RSS 拉到的新闻分为：fresh（首次见到）+ seen（已经见过）。
    fresh 用于判断 breaking_news；seen 仅做展示。
    见过的项会被标记时间戳，过期后自动清理。
    """
    seen = _load_seen()
    fresh, stale = [], []
    now_iso = datetime.now().isoformat()
    for it in items:
        h = _hash_news(it)
        if h in seen:
            stale.append(it)
        else:
            fresh.append(it)
            seen[h] = now_iso
    _save_seen(seen)
    return fresh, stale


# ── Equity events (affects TQQQ / SOXL) ──────────────────────────────────────
# FOMC: Jan28, Mar17, Apr28, Jun9, Jul28, Sep15, Oct27, Dec8
# CPI:  monthly ~10th-15th
# 事件影响等级（按市场实际反应幅度分级，2026-06-16）
# critical: 历史上常引发 ±1-2% 单日波动（FOMC/CPI/NFP）→ 前1天强制 HOLD 重仓ETF
# high:     常引发 ±0.5-1%（PCE/PPI/Retail Sales）→ 前1天降级 risk 但不强制 HOLD
# moderate: 小幅反应（单股财报、地区联储数据）→ 不影响决策路径
_EVENT_IMPACT_MAP = {
    "FOMC Decision": "critical",
    "Jackson Hole Symposium": "critical",
    "CPI Release":   "critical",
    "NFP Release":   "critical",
    "PCE Release":   "high",
    "PPI Release":   "high",
    "Retail Sales":  "high",
    "NVDA Earnings":         "moderate",
    "NVDA Earnings (est)":   "moderate",
}


def _classify_event_impact(event_name: str) -> str:
    """根据事件名返回 impact 等级（critical / high / moderate / normal）.

    支持 exact match 和 prefix match: 例如
    "PCE Release (核心 PCE >0.25% m/m = bond thesis invalidate)" 会命中 "PCE Release".
    避免日历里加了注释描述之后被误降级为 moderate.
    """
    if event_name in _EVENT_IMPACT_MAP:
        return _EVENT_IMPACT_MAP[event_name]
    for key, level in _EVENT_IMPACT_MAP.items():
        if event_name.startswith(key):
            return level
    return "moderate"


def _compute_risk_level(breaking: bool, days_away: int, event_impact: str) -> str:
    """综合 breaking_news + 事件距离 + 事件影响等级 → risk_level。

    risk_level → _etf_rules / _gold_rules 消费：
      · "high"     → days≤1 + critical 时强制 HOLD（保护重仓）
      · "elevated" → 降级低信心多头但不强制 HOLD
      · "moderate" → 仅展示，不影响决策
      · "normal"   → 不影响

    分级矩阵（distance × impact）：
      days≤1 + critical (FOMC/CPI/NFP) → high
      days≤1 + high     (PCE/PPI/Retail) → elevated
      days≤1 + moderate (财报)         → moderate
      days≤3 + critical                → elevated
      days≤3 + high                    → moderate
      days≤7 + critical                → moderate
      其它                              → normal
    """
    if breaking:
        return "high"
    if days_away <= 1:
        if event_impact == "critical": return "high"
        if event_impact == "high":     return "elevated"
        return "moderate"
    if days_away <= 3:
        if event_impact == "critical": return "elevated"
        if event_impact == "high":     return "moderate"
        return "normal"
    if days_away <= 7:
        if event_impact == "critical": return "moderate"
        return "normal"
    return "normal"


# ── 事件级备注 (thesis invalidation trigger 等) ──────────────────────────
# 从事件名分离到独立 dict, 事件名保持干净 (方便与 FRED cache 匹配)
# thesis_forecast / auto_rebalance 引用这里的注释
EVENT_THESIS_NOTES = {
    "PCE Release": "核心 PCE >0.25% m/m = bond thesis invalidate (project_thesis_2026Q3)",
    "FOMC Decision": "9-16 若不降息 = thesis playing_out 需 confirm",
}


def _load_calendar_from_fred_cache() -> list[dict] | None:
    """从 signals/economic_calendar.json (由 _refresh_calendar.py 周刷) 读日历.
    优先级: FRED cache > hardcoded fallback.

    Cache >14 天视为 stale, 忽略. 保证日历始终不落后 2 周以上.
    """
    from pathlib import Path
    from datetime import datetime as _dt
    p = Path(SIGNALS_DIR) / "economic_calendar.json"
    if not p.exists():
        return None
    try:
        age_days = (_dt.now().timestamp() - p.stat().st_mtime) / 86400
        if age_days > 14:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        events = d.get("events", [])
        if not events:
            return None
        # PCE 加 thesis 注释 (关键事件)
        for ev in events:
            note = EVENT_THESIS_NOTES.get(ev.get("event", ""))
            if note and ev.get("impact") == "critical":
                ev["thesis_note"] = note
        return events
    except Exception:
        return None


# hardcoded fallback: 如果 FRED cache 不可用 (首次运行/网络问题) 用这个
# 定期通过 _refresh_calendar.py --print 校对; 长期依赖 FRED cache
_EQUITY_CALENDAR_FALLBACK = [
    # CPI（通胀）
    {"date": "2026-05-13", "event": "CPI Release",           "impact": "high"},
    {"date": "2026-06-10", "event": "CPI Release",           "impact": "high"},
    {"date": "2026-07-15", "event": "CPI Release",           "impact": "high"},
    {"date": "2026-08-12", "event": "CPI Release",           "impact": "high"},
    {"date": "2026-09-11", "event": "CPI Release",           "impact": "high"},
    {"date": "2026-10-13", "event": "CPI Release",           "impact": "high"},
    {"date": "2026-11-12", "event": "CPI Release",           "impact": "high"},
    {"date": "2026-12-10", "event": "CPI Release",           "impact": "high"},
    # PPI（生产者价格，通常 CPI 后一天发布；上游通胀压力）
    {"date": "2026-05-14", "event": "PPI Release",           "impact": "high"},
    {"date": "2026-06-11", "event": "PPI Release",           "impact": "high"},
    {"date": "2026-07-16", "event": "PPI Release",           "impact": "high"},
    {"date": "2026-08-13", "event": "PPI Release",           "impact": "high"},
    {"date": "2026-09-10", "event": "PPI Release",           "impact": "high"},
    {"date": "2026-10-14", "event": "PPI Release",           "impact": "high"},
    {"date": "2026-11-13", "event": "PPI Release",           "impact": "high"},
    {"date": "2026-12-11", "event": "PPI Release",           "impact": "high"},
    # 零售销售（消费者支出代理，CPI/PPI 同周发布）
    {"date": "2026-05-15", "event": "Retail Sales",          "impact": "high"},
    {"date": "2026-06-17", "event": "Retail Sales",          "impact": "high"},
    {"date": "2026-07-16", "event": "Retail Sales",          "impact": "high"},
    {"date": "2026-08-14", "event": "Retail Sales",          "impact": "high"},
    {"date": "2026-09-16", "event": "Retail Sales",          "impact": "high"},
    {"date": "2026-10-15", "event": "Retail Sales",          "impact": "high"},
    {"date": "2026-11-13", "event": "Retail Sales",          "impact": "high"},
    # NFP（非农就业，月初首个周五）
    {"date": "2026-06-05", "event": "NFP Release",           "impact": "high"},
    {"date": "2026-07-03", "event": "NFP Release",           "impact": "high"},
    {"date": "2026-08-07", "event": "NFP Release",           "impact": "high"},
    {"date": "2026-09-04", "event": "NFP Release",           "impact": "high"},
    {"date": "2026-10-02", "event": "NFP Release",           "impact": "high"},
    {"date": "2026-11-06", "event": "NFP Release",           "impact": "high"},
    {"date": "2026-12-04", "event": "NFP Release",           "impact": "high"},
    # FOMC 决议 (Day 2, 2 PM ET 发布, Fed 官方 2026 calendar)
    {"date": "2026-06-17", "event": "FOMC Decision",         "impact": "high"},
    {"date": "2026-07-29", "event": "FOMC Decision",         "impact": "high"},
    {"date": "2026-09-16", "event": "FOMC Decision",         "impact": "high"},
    {"date": "2026-10-28", "event": "FOMC Decision",         "impact": "high"},
    {"date": "2026-12-09", "event": "FOMC Decision",         "impact": "high"},
    # NVDA 财报（半导体板块风向标；用户确认 2026-Q1 FY27 在 5-20）
    {"date": "2026-05-20", "event": "NVDA Earnings",         "impact": "high"},
    {"date": "2026-08-26", "event": "NVDA Earnings (est)",   "impact": "high"},
    {"date": "2026-11-18", "event": "NVDA Earnings (est)",   "impact": "high"},
    # Jackson Hole Symposium (Kansas City Fed annual, 每年 8 月最后一整周四五)
    # Fed Chair 演讲通常 8/25 前后周四/周五, 常重定义利率路径 (2022 Powell hawkish → SPX -3%)
    # 不在 FRED 也不在 Fed FOMC calendar API, 需要 hardcoded
    {"date": "2026-08-28", "event": "Jackson Hole Symposium (Warsh 演讲, hawkish 重定义)", "impact": "critical"},
    # 2026-Q3 thesis: 核心 PCE (BEA)、GOOG/AMZN Q3 earnings — invalidation triggers
    # 详见 memory/project_thesis_2026Q3.md
    # BEA Personal Income & Outlays 通常是每月倒数第 2 或最后一个工作日 (Wed-Fri) 08:30 ET
    # 与 GOLD_CALENDAR 里的 PCE 日期对齐 (2026-08-28 曾经错为 08-29)
    # BEA 官方 2026 release schedule (每月倒数 1-2 工作日, Wed/Thu 08:30 ET)
    {"date": "2026-08-28", "event": "PCE Release (核心 PCE >0.25% m/m = bond thesis invalidate)", "impact": "critical"},
    {"date": "2026-09-30", "event": "PCE Release", "impact": "critical"},
    {"date": "2026-10-29", "event": "PCE Release", "impact": "critical"},
    {"date": "2026-11-25", "event": "PCE Release", "impact": "critical"},
    {"date": "2026-12-23", "event": "PCE Release", "impact": "critical"},
    # GOOG / AMZN Q3 财报（capex 若继续上调 → AI 价值链 shift 判断错误）
    {"date": "2026-10-28", "event": "GOOG Q3 Earnings (capex 是否继续上调?)", "impact": "high"},
    {"date": "2026-10-30", "event": "AMZN Q3 Earnings (capex 是否继续上调?)", "impact": "high"},
    # MSFT Q1 FY27（AI capex 收窄若下季再现 = 结构性 shift 确认）
    {"date": "2026-10-28", "event": "MSFT Q1 FY27 Earnings (capex 是否继续收窄?)", "impact": "high"},
]


# 关键: EQUITY_CALENDAR 优先用 FRED cache, hardcoded 仅 fallback
# 用户 2026-08-27 明确要求: "日历一定要准", 不能靠手工维护
# _refresh_calendar.py 每周 weekly.bat 自动刷 FRED release schedule
_cached_events = _load_calendar_from_fred_cache()
if _cached_events:
    # 保留 hardcoded 里的公司财报事件 (NVDA/GOOG/AMZN/MSFT 等 — FRED 没有这些)
    _corp_events = [ev for ev in _EQUITY_CALENDAR_FALLBACK
                    if any(kw in ev.get("event", "")
                           for kw in ("Earnings", "NVDA", "GOOG", "AMZN", "MSFT"))]
    EQUITY_CALENDAR = sorted(_cached_events + _corp_events,
                              key=lambda x: (x["date"], x["event"]))
else:
    EQUITY_CALENDAR = _EQUITY_CALENDAR_FALLBACK

# ── 多信源验证配置 ─────────────────────────────────────────────────────────────
# 优先级：官方机构 RSS（零延迟）→ Yahoo Finance（~5分钟）→ FRED（可能滞后数小时）
# umbrella=True 表示该 RSS 是「最新数据汇总」格式（单条 item 含多数据点），
#   不能按 pubDate 匹配，需在 description 里找关键词 + 上月年月文字（"Apr 2026" 等）
_EVENT_VERIFY = {
    "CPI Release": {
        "agency_rss":  "https://www.bls.gov/feed/bls_latest.rss",
        "agency_kw":   ["consumer price index", "cpi"],
        "umbrella":    True,
        "yahoo_kw":    ["cpi", "consumer price", "inflation accelerat", "inflation pressur",
                        "inflation surge", "inflation cool", "inflation just hit",
                        "core inflation", "cpi spik", "cpi hot", "cpi rose", "cpi climbed"],
        "fred_series": "CPIAUCSL",
        "src_label":   "BLS",
        "value_re":    r"\+?(-?\d+\.\d+)%\s*in\s+\w+\s+\d{4}",
    },
    "NFP Release": {
        "agency_rss":  "https://www.bls.gov/feed/bls_latest.rss",
        "agency_kw":   ["employment situation", "nonfarm payroll", "nonfarm",
                        "total nonfarm", "unemployment rate"],
        "umbrella":    True,
        "yahoo_kw":    ["jobs report", "nonfarm", "jobs added", "employment situation",
                        "payrolls", "hiring slow", "jobs market", "unemployment"],
        "fred_series": "PAYEMS",
        "src_label":   "BLS",
    },
    "FOMC Decision": {
        "agency_rss":  "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "agency_kw":   ["federal open market", "target range", "federal funds rate"],
        "yahoo_kw":    ["fed holds", "fed cuts", "fed hikes", "fed raises", "fomc decision",
                        "rate decision", "fed keeps", "fed leaves", "fed maintains"],
        "fred_series": "FEDFUNDS",
        "src_label":   "Fed",
    },
    "PCE Release": {
        "agency_rss":  "https://www.bea.gov/rss/homefeed.rss",
        "agency_kw":   ["personal income", "personal consumption", "pce"],
        "yahoo_kw":    ["pce", "personal consumption expenditure", "personal spending",
                        "core pce", "fed preferred", "preferred inflation gauge"],
        "fred_series": "PCEPI",
        "src_label":   "BEA",
    },
    "PPI Release": {
        "agency_rss":  "https://www.bls.gov/feed/bls_latest.rss",
        "agency_kw":   ["producer price", "ppi", "producer prices"],
        "umbrella":    True,
        "yahoo_kw":    ["ppi", "producer price", "wholesale price", "producer inflation",
                        "ppi rose", "ppi climbed", "ppi spike", "ppi cool",
                        "input prices", "factory gate"],
        "fred_series": "PPIFIS",   # PPI Final Demand
        "src_label":   "BLS",
    },
    "Retail Sales": {
        "agency_rss":  "https://www.census.gov/economic-indicators/rss-indicators.xml",
        "agency_kw":   ["retail sales", "retail trade", "advance monthly retail"],
        "umbrella":    False,
        "yahoo_kw":    ["retail sales", "consumer spending", "retail trade",
                        "retail sales rose", "retail sales fell", "shoppers",
                        "retail data"],
        "fred_series": "RSAFS",    # Advance Retail Sales
        "src_label":   "Census",
    },
}

# ── Gold-specific calendar (additional events that move gold) ─────────────────
# Gold reacts to: FOMC, CPI, NFP, PCE, USD data, geopolitical
# 未来 PCE 事件已在 EQUITY_CALENDAR (critical). 此处只保留历史 PCE
# (已发布, 供回测/参考). 未来重复项会被下面的去重逻辑清掉.
GOLD_CALENDAR = EQUITY_CALENDAR + [
    {"date": "2026-05-29", "event": "PCE Release",           "impact": "high"},
    {"date": "2026-06-26", "event": "PCE Release",           "impact": "high"},
    {"date": "2026-07-31", "event": "PCE Release",           "impact": "high"},
]
# 单一源守护: 若 EQUITY_CALENDAR 已包含某未来 PCE 日期, GOLD 里同日期不重复
# (memory: feedback_single_source_calendar.md)
_gold_seen = set()
GOLD_CALENDAR = [
    ev for ev in GOLD_CALENDAR
    if not ((ev["date"], ev["event"].split("(")[0].strip()) in _gold_seen
            or _gold_seen.add((ev["date"], ev["event"].split("(")[0].strip())))
]
del _gold_seen

# ── Breaking news keywords ─────────────────────────────────────────────────────
EQUITY_BREAKING = [
    "crash", "circuit breaker", "emergency", "rate hike", "tariff",
    "trade war", "recession", "default", "bank failure", "war",
    "sanctions", "inflation surge", "fed cut", "fed hike",
]

# Gold-positive keywords (push gold higher)
GOLD_POSITIVE_KW = [
    "geopolit", "war", "sanction", "inflation", "gold rally", "gold surge",
    "gold spike", "safe haven", "dollar fall", "dollar weak", "rate cut",
    "fed dovish", "debt crisis", "bank crisis",
]

# Gold-negative keywords (push gold lower)
GOLD_NEGATIVE_KW = [
    "gold fall", "gold drop", "gold plunge", "gold slump",
    "dollar surge", "dollar rally", "risk on", "fed hawkish",
    "rate hike", "yields rise",
]


# BLS/政府站点对默认 User-Agent 返回 403，需要浏览器化的 header
_AGENCY_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept":          "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _check_agency_rss(rss_url: str, keywords: list, event_date: str,
                      umbrella: bool = False) -> bool:
    """
    官方机构 RSS 验证。
    - 常规 RSS（umbrella=False）：按 pubDate 匹配事件当天 + 含关键词
    - 汇总 RSS（umbrella=True，如 BLS Latest Numbers）：单条 item 内含多个数据点，
      不按 pubDate 匹配；在 description 里查找关键词 + 「上月-年月」字串
      （CPI for Apr 发布于 May 13，所以应在 description 里出现 "Apr 2026"）
    """
    try:
        ev_dt = datetime.strptime(event_date, "%Y-%m-%d").date()
        req   = urllib.request.Request(rss_url, headers=_AGENCY_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            root = ET.parse(resp).getroot()

        if umbrella:
            # 上一个完整月：CPI Apr 数据通常在 5 月发布
            from datetime import timedelta as _td
            target_month = (ev_dt.replace(day=1) - _td(days=1)).strftime("%b %Y").lower()
            for item in root.findall(".//item"):
                desc = ((item.findtext("title") or "") + " " +
                        (item.findtext("description") or "")).lower()
                # 关键词与目标月份必须在同一窗口（≤120 字符）内，
                # 防止 BLS 母 RSS 的同条 item 含多指标时跨指标误匹配
                # （例：NFP 段含 "May 2026"，CPI 段含 "Apr 2026"，全文 desc
                # 同时有 cpi + may 2026，旧版会误判 CPI 已落地）
                for kw in keywords:
                    idx = desc.find(kw)
                    while idx >= 0:
                        window = desc[max(0, idx - 60): idx + len(kw) + 120]
                        if target_month in window:
                            return True
                        idx = desc.find(kw, idx + 1)
            return False

        # 常规 RSS：按 pubDate 匹配
        for item in root.findall(".//item"):
            pub_str = item.findtext("pubDate") or ""
            try:
                tup = email.utils.parsedate(pub_str)
                if not tup or date(tup[0], tup[1], tup[2]) != ev_dt:
                    continue
            except Exception:
                continue
            text = ((item.findtext("title") or "") + " " +
                    (item.findtext("description") or "")).lower()
            if any(kw in text for kw in keywords):
                return True
    except Exception:
        pass
    return False


def _check_fred_series(series_id: str, event_date: str, api_key: str) -> bool:
    """FRED 兜底：可能滞后数小时，不作为主要信源。"""
    try:
        url = (f"https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={series_id}&sort_order=desc&limit=1"
               f"&api_key={api_key}&file_type=json")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return data["observations"][0]["date"] >= event_date
    except Exception:
        return False


def _is_event_landed(event_name: str, event_date: str) -> tuple[bool, str]:
    """
    三层信源验证事件是否已落地（**Yahoo RSS 改走 Claude CLI 结构化解析**）。
    返回 (landed: bool, source_label: str)
      1. 官方机构 RSS（BLS/Fed/BEA，零延迟，按月份匹配）
      2. Yahoo Finance RSS → news_analyzer (Claude CLI) 结构化解析；不再 keyword 匹配
         （keyword 容易把 "inflation due tomorrow" 误判已落地，CLI 能识别时态）
      3. FRED API（可能延迟数小时，兜底）
    """
    cfg = _EVENT_VERIFY.get(event_name)
    if not cfg:
        return False, ""

    # 1. 官方机构 RSS（umbrella 格式特殊处理）
    if _check_agency_rss(cfg["agency_rss"], cfg["agency_kw"], event_date,
                         umbrella=cfg.get("umbrella", False)):
        return True, cfg["src_label"]

    # 2. Yahoo Finance RSS → Claude CLI 结构化解析
    structured_type = _yahoo_event_type_map(event_name)
    if structured_type:
        try:
            from news_analyzer import fetch_yahoo_rss, is_event_landed_structured
            try:
                from datetime import datetime as _dt, timedelta as _td
                ev_dt = _dt.strptime(event_date, "%Y-%m-%d").date()
                period = (ev_dt.replace(day=1) - _td(days=1)).strftime("%Y-%m")
            except Exception:
                period = ""
            rss = fetch_yahoo_rss("SPY")
            landed, src, _ev = is_event_landed_structured(
                structured_type, period, rss, source="Yahoo SPY")
            if landed:
                return True, src or "Yahoo/CLI"
        except Exception:
            pass

    # 3. FRED API（兜底）
    try:
        from config import FRED_API_KEY
        if FRED_API_KEY and cfg.get("fred_series"):
            if _check_fred_series(cfg["fred_series"], event_date, FRED_API_KEY):
                return True, "FRED"
    except Exception:
        pass

    return False, ""


def _yahoo_event_type_map(event_name: str) -> str:
    """events_watch 内部事件名 → news_analyzer structured event_type 枚举值。"""
    return {
        "CPI Release":   "CPI_RELEASE",
        "PPI Release":   "PPI_RELEASE",
        "NFP Release":   "NFP_RELEASE",
        "PCE Release":   "PCE_RELEASE",
        "FOMC Decision": "FOMC",
    }.get(event_name, "")


def _next_event(calendar: list) -> dict | None:
    """
    返回最近未落地的重大事件。
    对当天 (days_away == 0) 的经济数据事件调用 FRED 验证：
    若数据已发布则跳过，显示下一个事件。
    """
    today = date.today()
    upcoming = []
    for ev in calendar:
        ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        if ev_date < today:
            continue
        days_away = (ev_date - today).days
        verify_src = ""

        # 当天事件：多信源确认数据是否已落地
        if days_away == 0 and ev["event"] in _EVENT_VERIFY:
            landed, src = _is_event_landed(ev["event"], ev["date"])
            if landed:
                continue          # 数据已发布，跳到下一个事件
            verify_src = src or "checked"   # 已验证但数据尚未出

        # 用 _classify_event_impact 覆盖日历里的 impact（按事件类型分级，不按日历标签）
        impact_classified = _classify_event_impact(ev["event"])
        upcoming.append({**ev, "impact": impact_classified,
                         "days_away": days_away, "verify_src": verify_src})

    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x["days_away"])
    return upcoming[0]


def _breaking_via_cli_or_keyword(rss_items: list, source: str,
                                  keyword_set: list) -> tuple[bool, str]:
    """
    Breaking news 判断：先用 Claude CLI 结构化解析；CLI 不可用回退 keyword。

    返回 (breaking: bool, evidence: str)
      evidence 是原文片段（CLI 路径）或匹配到的 keyword（fallback 路径）
    """
    if not rss_items:
        return False, ""
    try:
        from news_analyzer import analyze_rss_items
        parsed = analyze_rss_items(rss_items, source=source)
        if not parsed.get("fallback"):
            for it in parsed["items"]:
                if (it["event_type"] == "BREAKING"
                        and it["confidence"] >= 6
                        and it["magnitude"] in ("large", "extreme")):
                    return True, it.get("verbatim_evidence", "")[:120]
            return False, ""
    except Exception:
        pass
    # CLI 不可用 → keyword 兜底（保留旧行为防止系统断电）
    joined = " ".join(i.get("title", "") for i in rss_items).lower()
    for kw in keyword_set:
        if kw in joined:
            return True, f"keyword:{kw}"
    return False, ""


def _check_rss(ticker: str = "QQQ", timeout: int = 5) -> list[dict]:
    """Returns list of {title, url, desc} dicts for the latest headlines."""
    try:
        url = (
            f"https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={ticker}&region=US&lang=en-US"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tree = ET.parse(resp)
            root = tree.getroot()
            items = []
            for item in root.findall(".//item")[:8]:
                items.append({
                    "title": item.findtext("title", ""),
                    "url":   item.findtext("link", ""),
                    "desc":  item.findtext("description", ""),  # 用于挖共识
                })
        return items
    except Exception:
        return []


# ── 共识预期 / 超预期推断 ─────────────────────────────────────────────────────
import re as _re

# 每个事件对应的标题关键词（用于过滤相关新闻）
_EVENT_KEYWORDS = {
    "CPI Release":   ["cpi", "consumer price", "inflation report"],
    "PCE Release":   ["pce", "personal consumption", "core pce"],
    "PPI Release":   ["ppi", "producer price", "wholesale price", "producer inflation"],
    "Retail Sales":  ["retail sales", "consumer spending", "retail trade"],
    "NFP Release":   ["nonfarm", "payroll", "jobs report", "labor market"],
    "FOMC Decision": ["fed", "fomc", "rate decision", "powell", "federal reserve"],
}

# 超预期/符合/低于预期的英文表达（标题+描述 grep）
# 显式：above/below/in line forecast
# 隐式：媒体常用的描述词（accelerates / spike / cool / 3-year high 等）
_ABOVE_KW = [
    # 显式
    "above forecast", "above expectations", "above estimate",
    "beat ", "beats ", "tops forecast", "tops estimate",
    "hotter than", "stronger than expected", "exceeded forecast",
    # "Hot X" 模式（媒体口语化通胀偏热标题）
    "hot ppi", "hot cpi", "hot inflation", "hot jobs", "hot labor",
    "hot wage", "hot retail", "hot report", "hot data",
    "despite hot",
    # 隐式（通胀/经济数据偏热）
    "accelerat", "spike", "spiked", "spiking", "surge",
    "hottest", "sticky inflation", "resurgent inflation",
    "3-year high", "year high", "highest in", "hits new high",
    "rose more than", "climbed more than", "ticks higher",
]
_BELOW_KW = [
    "below forecast", "below expectations", "below estimate",
    "miss ", "misses ", "missed", "falls short",
    "cooler than", "softer than expected", "weaker than expected",
    # "Cool X" 模式
    "cool ppi", "cool cpi", "cool inflation", "cool jobs",
    "cool report", "cool data",
    # 隐式（偏冷/缓和）
    "cooled", "cooling", "ease", "eased", "easing",
    "slow", "slowed", "slowing", "weakest", "softest",
    "below expectation",
]
_INLINE_KW = ["in line with", "matching expectations", "as expected",
              "matched forecast", "matches estimate"]

# 数字共识抽取（"expected 0.3%"、"forecast of 0.4%"、"consensus 2.5%"）
_CONSENSUS_PATTERNS = [
    _re.compile(
        r"(?:consensus|expected|forecast|estimat\w+|economists?\s+expect\w*)"
        r"\s+(?:to\s+|of\s+|at\s+|was\s+|for\s+)?\s*([+\-]?\d+\.?\d*)\s*%",
        _re.IGNORECASE),
    _re.compile(
        r"versus\s+(?:the\s+)?(?:expected|forecast|consensus)\s+([+\-]?\d+\.?\d*)\s*%",
        _re.IGNORECASE),
    _re.compile(
        r"([+\-]?\d+\.?\d*)\s*%\s+(?:was\s+)?(?:expected|forecast|estimate)",
        _re.IGNORECASE),
]


def _extract_surprise(event_name: str) -> dict:
    """
    从 Yahoo 财经标题+描述里挖事件超预期判断。
    返回 {verdict: "超预期/符合预期/低于预期/", consensus: float|None, source_title: str}

    收紧策略：**标题里必须含事件关键词**，避免把通用交易策略文章误判
    （例如标题"Trade Strategy For SPY"含 cool/cooler 词被错判为 CPI 低于预期）。
    """
    kws = _EVENT_KEYWORDS.get(event_name, [])
    if not kws:
        return {"verdict": "", "consensus": None, "source_title": ""}

    # 多个 ticker feed 增加命中率（CPI 类宏观新闻可能在 SPY 或 QQQ 流里）
    candidates = []
    for tk in ("SPY", "QQQ", "DIA"):
        for it in _check_rss(tk):
            title = (it.get("title") or "").lower()
            desc  = (it.get("desc") or "").lower()
            # 关键改动：标题必须含事件关键词，不仅依赖描述
            if not any(kw in title for kw in kws):
                continue
            # 候选文本仍合并标题+描述（判定 verdict 用）
            candidates.append((title + " " + desc, it.get("title", "")))

    verdict, consensus, src_title = "", None, ""
    for text, title in candidates:
        if not verdict:
            if   any(kw in text for kw in _ABOVE_KW):  verdict = "超预期"; src_title = title
            elif any(kw in text for kw in _BELOW_KW):  verdict = "低于预期"; src_title = title
            elif any(kw in text for kw in _INLINE_KW): verdict = "符合预期"; src_title = title
        if consensus is None:
            for pat in _CONSENSUS_PATTERNS:
                m = pat.search(text)
                if m:
                    try: consensus = float(m.group(1)); break
                    except ValueError: pass
        if verdict and consensus is not None:
            break

    return {"verdict": verdict, "consensus": consensus, "source_title": src_title}


def _get_options_risk_safe() -> dict:
    """events_signal 层安全 wrapper：option_walls 失败时返回最小占位 dict。"""
    try:
        from option_walls import get_options_risk_signal
        return get_options_risk_signal(["TQQQ", "SOXL", "DRAM", "SPY", "QQQ"])
    except Exception:
        return {"witching": {"is_witching_day": False, "phase": "far",
                              "days_to_witching": 99},
                "summary": {"max_risk": "low", "reason": "options risk unavailable"},
                "fallback": True}


def _get_trump_signal_safe() -> dict:
    """events_signal 层用的安全 wrapper：CLI 失败时返回 fallback dict 不抛错。"""
    try:
        from trump_signal import get_trump_signal_cached
        ts = get_trump_signal_cached(hours=24)
    except Exception:
        ts = {"fallback": True}
    return {
        "direction":      ts.get("aggregate_direction", "neutral"),
        "magnitude":      ts.get("aggregate_magnitude", "small"),
        "score":          ts.get("aggregate_score", 0),
        "tariff_alert":   ts.get("tariff_alert", False),
        "silence_signal": ts.get("silence_signal", False),
        "posts_count":    ts.get("posts_count", 0),
        "n_signals_medium_plus": sum(
            1 for s in ts.get("active_signals", [])
            if s.get("magnitude") in ("medium", "large", "extreme")
        ),
        "fallback":       ts.get("fallback", False),
    }


def get_events_signal() -> dict:
    """Equity-mode events signal (for TQQQ / SOXL)."""
    ev      = _next_event(EQUITY_CALENDAR)
    items   = _check_rss("QQQ")
    # 只用新出现的标题判断 breaking_news：先尝试 CLI 结构化解析，失败再 keyword 兜底
    fresh, _ = _filter_fresh_news(items)
    breaking, breaking_evidence = _breaking_via_cli_or_keyword(
        fresh, source="Yahoo QQQ", keyword_set=EQUITY_BREAKING)

    days_away = ev["days_away"] if ev else 99
    # risk_level 按事件 impact 等级 + 距离天数综合判定
    # critical (FOMC/CPI/NFP): days≤1 → high；days≤3 → elevated；days≤7 → moderate
    # high (PCE/PPI/Retail):    days≤1 → elevated（不再强制 HOLD）；days≤3 → moderate
    # moderate (单股财报):      不升风险
    event_impact = ev.get("impact", "moderate") if ev else "normal"
    risk_level = _compute_risk_level(breaking, days_away, event_impact)

    return {
        "ts":                datetime.now().strftime("%Y-%m-%d %H:%M"),
        "next_event":        f"{ev['event']} ({ev['date']})" if ev else "none",
        "next_event_name":   ev["event"]  if ev else "",
        "next_event_date":   ev["date"]   if ev else "",
        "next_event_impact":  ev.get("impact", "normal") if ev else "normal",
        "event_verify_src":   ev.get("verify_src", "") if ev else "",
        "days_to_event":      days_away,
        "breaking_news":      breaking,
        "breaking_evidence":  breaking_evidence,
        "top_headlines":      items[:2],
        "risk_level":         risk_level,
        "trump_signal":       _get_trump_signal_safe(),
        "options_risk":       _get_options_risk_safe(),
    }


def get_gold_events_signal() -> dict:
    """Gold-mode events signal (for GLD/GCmain). Includes PCE + gold sentiment."""
    ev     = _next_event(GOLD_CALENDAR)
    items  = _check_rss("GLD")
    # breaking 判断只看新出现的标题；情绪偏向用全部（包括反复出现的）
    fresh, _ = _filter_fresh_news(items)
    joined       = " ".join(i["title"] for i in items).lower()

    breaking, breaking_evidence = _breaking_via_cli_or_keyword(
        fresh, source="Yahoo GLD", keyword_set=EQUITY_BREAKING)

    # 黄金宏观信号（real_rate / DXY / Fed BS / FOMC）— 仅展示 + 供 Claude 解读
    # 注：尝试用 gold_macro 直接驱动 gold_bias 替代 keyword 时，_gold_rules 兜底
    # 路径（bias + trend）准确率下降 -4%（_backtest_gold_macro.py 验证）。
    # 维持 keyword 法驱动 gold_bias 决策路径，gold_macro 仅作 banner + Claude 输入。
    try:
        from gold_macro import get_gold_macro_signal
        gold_macro = get_gold_macro_signal()
    except Exception:
        gold_macro = {"fallback": True}
    gold_positive = any(kw in joined for kw in GOLD_POSITIVE_KW)
    gold_negative = any(kw in joined for kw in GOLD_NEGATIVE_KW)
    if gold_positive and gold_negative:
        gold_bias = "mixed"
    elif gold_positive:
        gold_bias = "bullish"
    elif gold_negative:
        gold_bias = "bearish"
    else:
        gold_bias = "neutral"
    gold_bias_source = "keyword"

    days_away = ev["days_away"] if ev else 99
    event_impact = ev.get("impact", "moderate") if ev else "normal"
    risk_level = _compute_risk_level(breaking, days_away, event_impact)

    return {
        "ts":                datetime.now().strftime("%Y-%m-%d %H:%M"),
        "next_event":        f"{ev['event']} ({ev['date']})" if ev else "none",
        "next_event_name":   ev["event"]  if ev else "",
        "next_event_date":   ev["date"]   if ev else "",
        "next_event_impact":  ev.get("impact", "normal") if ev else "normal",
        "event_verify_src":   ev.get("verify_src", "") if ev else "",
        "days_to_event":      days_away,
        "breaking_news":      breaking,
        "breaking_evidence":  breaking_evidence,
        "gold_bias":          gold_bias,
        "gold_bias_source":   gold_bias_source,
        "gold_macro":         gold_macro,
        "top_headlines":      items[:2],
        "risk_level":         risk_level,
        "trump_signal":       _get_trump_signal_safe(),
        "options_risk":       _get_options_risk_safe(),
    }


# ── 事件日历报告 ──────────────────────────────────────────────────────────────

def _get_fred_obs(series_id: str, api_key: str, n: int = 13) -> list[dict]:
    """从 FRED 取最近 n 条观测值，过滤掉缺失值（'.'）。"""
    try:
        url = (f"https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={series_id}&sort_order=desc&limit={n}"
               f"&api_key={api_key}&file_type=json")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return [o for o in data["observations"] if o.get("value", ".") != "."]
    except Exception:
        return []


def _surprise_tag(verdict: str, consensus: float | None) -> str:
    """格式化共识/超预期标注。"""
    if not verdict and consensus is None:
        return ""
    parts = []
    if consensus is not None:
        parts.append(f"预期{consensus:+.2f}%")
    if verdict:
        parts.append(verdict)
    return f"  ({' '.join(parts)})" if parts else ""


def _extract_bls_value(event_name: str, event_date: str = "") -> str:
    """
    BLS Latest Numbers RSS 的 description 已经写好了最新数据（"+0.6% in Apr 2026"）。
    直接解析这个字串，比等 FRED 更新更快。

    event_date 传入时（YYYY-MM-DD），强制要求提取出的 "MMM YYYY" 等于 *上一个完整月*
    （即此次事件应发布的数据所属月份）；不等则跳过——避免显示陈旧数据。
    """
    cfg = _EVENT_VERIFY.get(event_name)
    if not cfg or not cfg.get("umbrella"):
        return ""
    target_month = ""
    if event_date:
        try:
            from datetime import timedelta as _td
            ev_dt = datetime.strptime(event_date, "%Y-%m-%d").date()
            target_month = (ev_dt.replace(day=1) - _td(days=1)).strftime("%b %Y").lower()
        except Exception:
            target_month = ""
    try:
        req = urllib.request.Request(cfg["agency_rss"], headers=_AGENCY_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            root = ET.parse(resp).getroot()
        for item in root.findall(".//item"):
            desc = (item.findtext("description") or "")
            # 找到包含目标关键词的段落，提取「+X.X% in MMM YYYY」
            # 容忍 BLS 用 "(p)"=preliminary, "(r)"=revised 这种后缀标记
            for kw in cfg["agency_kw"]:
                idx = desc.lower().find(kw)
                if idx < 0:
                    continue
                window = desc[idx:idx + 600]
                # 主模式：+1.4%(p)  in Apr 2026
                m = _re.search(
                    r"([+\-]?\d+\.\d+)\s*%\s*(?:\([a-zA-Z]\))?\s*in\s+(\w+\s+\d{4})",
                    window)
                if m:
                    if target_month and m.group(2).lower() != target_month:
                        continue
                    return f"{m.group(1)}% in {m.group(2)}"
                # NFP 风格：+185,000 或 4.3% 后跟年月
                m2 = _re.search(
                    r"([+\-]?\d+(?:[\.,]\d+)?\s*%?)\s*(?:\([a-zA-Z]\))?\s+in\s+(\w+\s+\d{4})",
                    window)
                if m2:
                    if target_month and m2.group(2).lower() != target_month:
                        continue
                    return f"{m2.group(1)} in {m2.group(2)}"
        return ""
    except Exception:
        return ""


def _get_event_baseline(event_name: str) -> str:
    """
    事件未发布时的参照基线：从 FRED 取最近实际值 + 近3月均值。
    无需等媒体出共识——直接给用户「上月多少，最近趋势如何」。
    """
    cfg = _EVENT_VERIFY.get(event_name)
    if not cfg or not cfg.get("fred_series"):
        return ""
    try:
        from config import FRED_API_KEY
        if not FRED_API_KEY:
            return ""
        obs = _get_fred_obs(cfg["fred_series"], FRED_API_KEY, n=6)
        if len(obs) < 2:
            return ""
        curr = float(obs[0]["value"])
        prev = float(obs[1]["value"])

        if event_name == "FOMC Decision":
            return f"当前利率 {curr:.2f}%"

        if event_name == "NFP Release":
            return f"上月新增 {curr - prev:+.0f}K"

        # CPI / PCE：上月 MoM + 近3月均 MoM
        prev_mom = (curr - prev) / prev * 100 if prev else 0
        avg3_str = ""
        if len(obs) >= 4:
            moms = []
            for i in range(3):
                a = float(obs[i]["value"])
                b = float(obs[i + 1]["value"])
                if b:
                    moms.append((a - b) / b * 100)
            if moms:
                avg3_str = f", 近3月均{sum(moms)/len(moms):+.2f}%"
        return f"上月{prev_mom:+.2f}%{avg3_str}"
    except Exception:
        return ""


def _get_event_result(event_name: str, event_date: str) -> str:
    """
    返回已落地事件的实际数据 + 超预期判断。
    优先级：BLS 实测值（umbrella RSS 内） > FRED 历史值
    """
    cfg = _EVENT_VERIFY.get(event_name)
    if not cfg:
        return ""
    surprise = _extract_surprise(event_name)

    # 1. BLS umbrella RSS 已经有实测值（CPI 用这个最快）
    bls_val = _extract_bls_value(event_name, event_date) if cfg.get("umbrella") else ""
    if bls_val:
        return f"BLS: {bls_val}" + _surprise_tag(surprise["verdict"], surprise["consensus"])

    # 2. 回退到 FRED
    if not cfg.get("fred_series"):
        return _surprise_tag(surprise["verdict"], surprise["consensus"]).lstrip()
    try:
        from config import FRED_API_KEY
        if not FRED_API_KEY:
            return ""
        obs = _get_fred_obs(cfg["fred_series"], FRED_API_KEY)
        if not obs:
            return ""
        curr = float(obs[0]["value"])

        if event_name == "FOMC Decision":
            if len(obs) >= 2:
                prev = float(obs[1]["value"])
                chg  = curr - prev
                core = (f"维持利率 {curr:.2f}%" if abs(chg) < 0.01
                        else f"{'降息' if chg < 0 else '加息'} {abs(chg)*100:.0f}bp → {curr:.2f}%")
            else:
                core = f"利率 {curr:.2f}%"
            return core + _surprise_tag(surprise["verdict"], surprise["consensus"])

        if len(obs) < 2:
            return f"{curr:.2f}" + _surprise_tag(surprise["verdict"], surprise["consensus"])
        prev_m = float(obs[1]["value"])

        if event_name == "NFP Release":
            chg_k = curr - prev_m
            return f"新增 {chg_k:+.0f}K 非农就业" + _surprise_tag(
                surprise["verdict"], surprise["consensus"])

        # CPI / PCE：月增 + 同比 + 上月对比 + 超预期判断
        mom = (curr - prev_m) / prev_m * 100 if prev_m else 0
        # 上月的 MoM（用于判断是否加速）
        prev_mom_str = ""
        if len(obs) >= 3:
            prev_prev = float(obs[2]["value"])
            prev_mom = (prev_m - prev_prev) / prev_prev * 100 if prev_prev else 0
            prev_mom_str = f" vs 上月{prev_mom:+.2f}%"
        # 同比
        yoy_str = ""
        if len(obs) >= 13:
            prev_y = float(obs[12]["value"])
            yoy = (curr - prev_y) / prev_y * 100 if prev_y else 0
            yoy_str = f"  同比 {yoy:+.1f}%"
        return (f"月增 {mom:+.2f}%{prev_mom_str}{yoy_str}"
                + _surprise_tag(surprise["verdict"], surprise["consensus"]))
    except Exception:
        return ""


def _get_earnings_result(ticker: str) -> str:
    """从 Yahoo Finance RSS 找财报结果标题（最多60字符）。"""
    items = _check_rss(ticker)
    kw = ["beat", "miss", "earnings", "eps", "revenue", "results", "quarterly"]
    for item in items:
        if any(k in item["title"].lower() for k in kw):
            return item["title"][:60]
    return ""


def generate_event_calendar_report(lookback_days: int = 30,
                                   lookahead_days: int = 45) -> str:
    """
    输出重大事件日历：
      - 过去 lookback_days 天已落地事件 + 实际数据（FRED）
      - 今日事件 + 实时落地验证
      - 未来 lookahead_days 天待发布事件 + 倒计时
    """
    # 去重合并日历（GOLD_CALENDAR 已含 EQUITY_CALENDAR + PCE）
    seen: set = set()
    all_events: list = []
    for ev in sorted(GOLD_CALENDAR, key=lambda x: x["date"]):
        key = (ev["date"], ev["event"])
        if key not in seen:
            seen.add(key)
            all_events.append(ev)

    today   = date.today()
    W       = 68
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = t("重大事件日历", "重要イベント・カレンダー")
    lines   = [
        "+" + "=" * (W - 2) + "+",
        f"|  {title}  |  {now_str}".ljust(W - 1) + "|",
        "+" + "=" * (W - 2) + "+",
        "",
    ]

    hdr_date  = t("日期", "日付")
    hdr_event = t("事件", "イベント")
    hdr_data  = t("实际数据", "実績データ")
    hdr_days  = t("距今", "残り日数")

    # ── 已落地 ────────────────────────────────────────────────────────────
    past = [
        ev for ev in all_events
        if datetime.strptime(ev["date"], "%Y-%m-%d").date() < today
        and (today - datetime.strptime(ev["date"], "%Y-%m-%d").date()).days <= lookback_days
    ]
    if past:
        lines.append(t(f"  [已落地]  过去 {lookback_days} 天",
                       f"  [発表済]  過去 {lookback_days} 日"))
        lines.append(f"  {hdr_date:<12} {hdr_event:<24} {hdr_data}")
        lines.append(f"  {'-'*12} {'-'*24} {'-'*22}")
        for ev in past:
            if ev["event"] in _EVENT_VERIFY:
                result = _get_event_result(ev["event"], ev["date"])
            elif "Earnings" in ev["event"]:
                result = _get_earnings_result(ev["event"].split()[0])
            else:
                result = ""
            lines.append(f"  [+] {ev['date']}  {ev['event']:<24}  {result or '---'}")
        lines.append("")

    # ── 今日 ──────────────────────────────────────────────────────────────
    today_evs = [ev for ev in all_events if ev["date"] == today.isoformat()]
    if today_evs:
        lines.append(t("  [今日事件]", "  [本日イベント]"))
        for ev in today_evs:
            if ev["event"] in _EVENT_VERIFY:
                landed, src = _is_event_landed(ev["event"], ev["date"])
                if landed:
                    result = _get_event_result(ev["event"], ev["date"])
                    status = t(f"已落地 [{src}]  {result}",
                               f"発表済 [{src}]  {result}").strip()
                else:
                    baseline = _get_event_baseline(ev["event"])
                    surprise = _extract_surprise(ev["event"])
                    consensus_str = (
                        t(f", 预期{surprise['consensus']:+.2f}%",
                          f", 予想{surprise['consensus']:+.2f}%")
                        if surprise["consensus"] is not None else "")
                    ctx_parts = [p for p in (baseline + consensus_str,) if p.strip()]
                    info = f"  ({ctx_parts[0]})" if ctx_parts else ""
                    tag  = (t(f"[{src}已验证]", f"[{src}検証済]")
                            if src else "")
                    status = t(f"待发布  {tag}{info}", f"未発表  {tag}{info}").strip()
            else:
                status = t("今日", "本日")
            lines.append(f"  [!] {ev['date']}  {ev['event']:<24}  {status}")
        lines.append("")

    # ── 待发布 ────────────────────────────────────────────────────────────
    future = [
        ev for ev in all_events
        if datetime.strptime(ev["date"], "%Y-%m-%d").date() > today
        and (datetime.strptime(ev["date"], "%Y-%m-%d").date() - today).days <= lookahead_days
    ]
    if future:
        lines.append(t(f"  [待发布]  未来 {lookahead_days} 天",
                       f"  [未発表]  今後 {lookahead_days} 日"))
        lines.append(f"  {hdr_date:<12} {hdr_event:<24} {hdr_days}")
        lines.append(f"  {'-'*12} {'-'*24} {'-'*8}")
        for ev in future:
            days    = (datetime.strptime(ev["date"], "%Y-%m-%d").date() - today).days
            day_str = (t("明天", "明日") if days == 1
                       else t(f"{days}天后", f"{days}日後"))
            mark    = " ★" if ev.get("impact") == "high" else ""
            baseline = ""
            if days <= 30 and ev["event"] in _EVENT_VERIFY:
                b = _get_event_baseline(ev["event"])
                if b: baseline = f"  ({b})"
            lines.append(f"  [ ] {ev['date']}  {ev['event']:<24}  {day_str}{mark}{baseline}")
        lines.append("")

    lines.append("=" * W)
    lines.append(t(
        "  ★ = 高影响事件  [+] = 已落地  [!] = 今日  [ ] = 待发布",
        "  ★ = 高影響イベント  [+] = 発表済  [!] = 本日  [ ] = 未発表",
    ))
    lines.append(t(
        "  数据来源: FRED (CPI/NFP/FOMC/PCE) · Yahoo Finance (财报)",
        "  データソース: FRED (CPI/NFP/FOMC/PCE) · Yahoo Finance (決算)",
    ))
    lines.append("=" * W)
    return "\n".join(lines)
