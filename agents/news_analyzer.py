"""
news_analyzer.py — RSS / 新闻 → 结构化信号

任何 events_watch / decision_agent / breaking_news 消费 RSS 之前必须先经此模块过滤。
调用本地 Claude CLI（fallback Codex CLI）把自由文本拆成固定 JSON schema：

  {
    "items": [
      {
        "event_type": "CPI_RELEASE" | "PPI_RELEASE" | "NFP_RELEASE"
                    | "PCE_RELEASE" | "FOMC" | "EARNINGS" | "BREAKING" | "NOISE",
        "is_landed": bool,                # 数据是否实际已发布（RELEASE/FOMC）
        "period":  "2026-05" | "2026-Q1" | "",  # 数据所属时段
        "value":   {"mom_pct": 0.4, ...}, # 解析出的具体数值（dict）
        "direction":  "bullish" | "bearish" | "neutral",   # 对美股影响
        "magnitude":  "small" | "medium" | "large" | "extreme",
        "tickers_affected": ["TQQQ", "SOXL", "GLD", "SPY", "QQQ", "NVDA"],
        "confidence": 0-10,
        "raw_source": "Yahoo Finance" | "BLS" | ...,
        "verbatim_evidence": "..."        # 原文关键句（≤120 字符）
      },
      ...
    ]
  }

24h 缓存（按 input 文本 SHA1 做 key 写到 signals/news_parsed_<hash>.json）。
CLI 不可用时 fallback {"items":[], "fallback":true}，调用方需自行决定退化路径。

独立使用：
  python news_analyzer.py SPY        # 拉 SPY Yahoo RSS 解析并打印
  python news_analyzer.py --bls      # 拉 BLS umbrella RSS 解析并打印
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path

from config import SIGNALS_DIR


CACHE_DIR = Path(SIGNALS_DIR) / "news_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_HOURS = 24

# 所有可能的 event_type / direction / magnitude（防止 CLI 输出未知值）
_EVENT_TYPES = {"CPI_RELEASE", "PPI_RELEASE", "NFP_RELEASE", "PCE_RELEASE",
                "FOMC", "EARNINGS", "BREAKING", "NOISE"}
_DIRECTIONS  = {"bullish", "bearish", "neutral"}
_MAGNITUDES  = {"small", "medium", "large", "extreme"}
_RELEASE_TYPES = {"CPI_RELEASE", "PPI_RELEASE", "NFP_RELEASE",
                  "PCE_RELEASE", "FOMC"}


_PROMPT = """你是 RSS 新闻 → 结构化信号 的解析器。请把每个 item 严格分类后返回 JSON。

event_type 枚举（必须从中选一个）：
- CPI_RELEASE / PPI_RELEASE / NFP_RELEASE / PCE_RELEASE：经济数据发布或预告
- FOMC：联储利率决议
- EARNINGS：公司财报
- BREAKING：地缘冲突 / 央行紧急行动 / 重大政策 / 系统性风险（要明显高于日常波动）
- NOISE：与上述都无关 / 纯例行报道 / 标题党 / Stock Y 新闻策略类

对每个 item 必须返回：
- event_type
- is_landed: 是否报道一个 *已经发布的实测值*。仅 RELEASE/FOMC 类需要判断；其它 false。
    判定：新闻引用具体数字 + 过去时表达（"CPI rose 0.4% in May"）→ true；
    用将来时 / 预测 / 待发布（"CPI due Friday"、"economists expect ..."）→ false
- period: YYYY-MM（月度）或 YYYY-Qn（季度）。RELEASE/FOMC 类必须有；从原文里识别月份名称
    "in May 2026" → "2026-05"，"Q1 2026" → "2026-Q1"。未明确则 ""
- value: dict。CPI 类典型 {{"mom_pct":0.4, "yoy_pct":3.2}}；NFP {{"jobs_k":172, "unemp_pct":4.3}}；
    FOMC {{"rate_pct":3.63}}；没数字就 {{}}
- direction: 对美股影响。CPI 高于预期=bearish（紧缩）/ 低于预期=bullish；FOMC 鹰=bearish，鸽=bullish
- magnitude: small / medium / large / extreme
- tickers_affected: 从给定 watched_tickers 选受影响的，最多 6 个
- confidence: 0-10
- raw_source: 复述 source 标签
- verbatim_evidence: 从原文截一段关键证据（≤120 字符，必须是原文片段，不要总结）

**输出规则（务必遵守）**：
1. **严格 JSON**，仅一个根对象 `{{"items":[...]}}`
2. 不要 markdown，不要 ```json``` 围栏，不要解释，不要前言后语
3. items 必须等长于输入 items，按相同顺序输出
4. 字段缺失或未知 → 用 "" 或 [] 或 {{}} 填，不要省略字段

今日日期：{today}
关注的经济日历事件（未来 14 天 + 过去 7 天）：{events}
关注标的 watched_tickers：{tickers}

待解析 RSS items（JSON 数组）：
{rss_items_json}
"""


# ── 缓存 ──────────────────────────────────────────────────────────────────
def _cache_key(rss_items: list, today: str, events: str, tickers: str) -> str:
    text = json.dumps([rss_items, today, events, tickers],
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"news_parsed_{key}.json"


def _read_cache(key: str) -> dict | None:
    p = _cache_path(key)
    if not p.exists():
        return None
    age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
    if age_h > CACHE_TTL_HOURS:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(key: str, data: dict) -> None:
    try:
        _cache_path(key).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass


# ── JSON 解析鲁棒化 ────────────────────────────────────────────────────────
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_from_cli_output(text: str) -> dict | None:
    """从 CLI 输出里抠出 JSON。容忍 markdown 围栏、前后空行、解释文字。"""
    if not text:
        return None
    # 1. ```json {...} ```
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 2. 整段就是 JSON
    s = text.strip().lstrip("﻿")
    if s.startswith("{") and s.endswith("}"):
        try:
            return json.loads(s)
        except Exception:
            pass
    # 3. 找第一个 { 到最后一个 }
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j+1])
        except Exception:
            return None
    return None


def _normalize_item(raw: dict, source: str) -> dict:
    """把 CLI 返回的单条 item 规范化 + 校验枚举值。非法字段用安全默认。"""
    et = (raw.get("event_type") or "NOISE").upper().strip()
    if et not in _EVENT_TYPES:
        et = "NOISE"
    direction = (raw.get("direction") or "neutral").lower().strip()
    if direction not in _DIRECTIONS:
        direction = "neutral"
    magnitude = (raw.get("magnitude") or "small").lower().strip()
    if magnitude not in _MAGNITUDES:
        magnitude = "small"
    # is_landed 仅 RELEASE/FOMC 才允许 true
    is_landed = bool(raw.get("is_landed", False)) and et in _RELEASE_TYPES
    period = (raw.get("period") or "").strip()
    # period 格式校验：YYYY-MM 或 YYYY-Qn
    if period and not re.match(r"^\d{4}-(\d{2}|Q[1-4])$", period):
        period = ""
    val = raw.get("value") or {}
    if not isinstance(val, dict):
        val = {}
    tickers = raw.get("tickers_affected") or []
    if not isinstance(tickers, list):
        tickers = []
    tickers = [str(t).upper().strip() for t in tickers][:6]
    try:
        conf = int(raw.get("confidence", 0))
    except Exception:
        conf = 0
    conf = max(0, min(10, conf))
    evidence = (raw.get("verbatim_evidence") or "").strip()[:200]
    return {
        "event_type": et,
        "is_landed": is_landed,
        "period": period,
        "value": val,
        "direction": direction,
        "magnitude": magnitude,
        "tickers_affected": tickers,
        "confidence": conf,
        "raw_source": (raw.get("raw_source") or source).strip()[:40],
        "verbatim_evidence": evidence,
    }


# ── 主入口 ────────────────────────────────────────────────────────────────
def analyze_rss_items(rss_items: list, *, source: str = "RSS",
                      watched_tickers: list | None = None,
                      events_calendar: list | None = None,
                      cli_timeout: int = 60) -> dict:
    """
    把 RSS items 列表喂给 Claude CLI 解析为结构化信号。

    rss_items: [{"title": str, "desc": str, ...}, ...]
    source:    标识来源（"Yahoo SPY"、"BLS umbrella" 等）
    watched_tickers: 关注的标的列表
    events_calendar: 关注的事件列表（[{date, event}, ...]）
    cli_timeout: Claude/Codex CLI 超时秒数

    返回 {"items": [...规范化后的 dict...], "fallback": bool, "source": str}
    fallback=True 表示 CLI 不可用、本次没解析（调用方应自行决定退化策略）。
    """
    if not rss_items:
        return {"items": [], "fallback": False, "source": source}

    today = date.today().isoformat()
    watched_tickers = watched_tickers or ["TQQQ", "SOXL", "GLD",
                                          "SPY", "QQQ", "NVDA"]
    if events_calendar is None:
        events_calendar = _default_events_window()

    events_str  = json.dumps(events_calendar, ensure_ascii=False)
    tickers_str = json.dumps(watched_tickers)
    rss_compact = [
        {"title": (it.get("title") or "")[:200],
         "desc":  (it.get("desc")  or "")[:600],
         "source": source}
        for it in rss_items
    ]
    rss_json = json.dumps(rss_compact, ensure_ascii=False, indent=2)

    key = _cache_key(rss_compact, today, events_str, tickers_str)
    cached = _read_cache(key)
    if cached:
        return {"items": cached.get("items", []),
                "fallback": False, "source": source, "cached": True}

    prompt = _PROMPT.format(today=today, events=events_str,
                            tickers=tickers_str, rss_items_json=rss_json)

    # 调用本地 Claude CLI；额度满时切 Codex CLI
    from ai_prompt import query_ai_cli
    out, status, provider, _ = query_ai_cli(prompt, timeout=cli_timeout)

    if not out:
        return {"items": [], "fallback": True, "source": source,
                "cli_status": status}

    parsed = _extract_json_from_cli_output(out)
    if not parsed or not isinstance(parsed.get("items"), list):
        return {"items": [], "fallback": True, "source": source,
                "cli_status": f"parse_failed: {out[:200]}"}

    items = [_normalize_item(it, source) for it in parsed["items"]
             if isinstance(it, dict)]

    result = {"items": items, "fallback": False, "source": source,
              "provider": provider, "ts": datetime.now().isoformat()}
    _write_cache(key, result)
    return result


# ── 经济日历窗口（不引入 events_watch 避免循环依赖） ────────────────────────
def _default_events_window() -> list:
    """返回过去 7 天 + 未来 14 天的硬编码经济日历事件。"""
    try:
        from events_watch import GOLD_CALENDAR as cal
    except Exception:
        return []
    today = date.today()
    out = []
    for ev in cal:
        try:
            d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if -7 <= (d - today).days <= 14:
            out.append({"date": ev["date"], "event": ev["event"]})
    return out


# ── RSS 抓取（薄包装，不做 keyword 匹配，仅产生原始 items） ──────────────────
def fetch_yahoo_rss(ticker: str, timeout: int = 8) -> list[dict]:
    url = (f"https://feeds.finance.yahoo.com/rss/2.0/headline"
           f"?s={ticker}&region=US&lang=en-US")
    return _fetch_rss(url, source=f"Yahoo {ticker}", timeout=timeout)


def fetch_agency_rss(rss_url: str, source: str = "Agency",
                     timeout: int = 8) -> list[dict]:
    return _fetch_rss(rss_url, source=source, timeout=timeout,
                      browser_ua=True)


def _fetch_rss(url: str, source: str, timeout: int = 8,
               browser_ua: bool = False) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0"} if not browser_ua else {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            root = ET.parse(resp).getroot()
        items = []
        for item in root.findall(".//item")[:10]:
            items.append({
                "title": item.findtext("title", "") or "",
                "url":   item.findtext("link", "") or "",
                "desc":  item.findtext("description", "") or "",
                "pubDate": item.findtext("pubDate", "") or "",
                "source": source,
            })
        return items
    except Exception:
        return []


# ── 高级查询：基于结构化信号回答业务问题 ─────────────────────────────────────
def is_event_landed_structured(event_type: str, period: str,
                                rss_items: list, source: str = "RSS") -> tuple[bool, str, dict]:
    """
    问 CLI：在 rss_items 里能否找到一条已落地的 <event_type, period>？
    返回 (landed, source_label, evidence_item)。
    """
    parsed = analyze_rss_items(rss_items, source=source)
    if parsed.get("fallback"):
        return False, "", {}
    for it in parsed["items"]:
        if (it["event_type"] == event_type and it["is_landed"]
                and (not period or it["period"] == period)):
            return True, f"{source}/CLI", it
    return False, "", {}


def find_breaking_news_structured(rss_items: list,
                                   min_confidence: int = 6) -> list[dict]:
    """挑出 BREAKING 类信号（CLI 判断，confidence ≥ 阈值）。"""
    parsed = analyze_rss_items(rss_items)
    if parsed.get("fallback"):
        return []
    return [it for it in parsed["items"]
            if it["event_type"] == "BREAKING" and it["confidence"] >= min_confidence]


# ── CLI 测试入口 ──────────────────────────────────────────────────────────
def _cli_main():
    if len(sys.argv) < 2:
        print("用法: python news_analyzer.py SPY|QQQ|--bls")
        return
    arg = sys.argv[1]
    if arg == "--bls":
        rss = fetch_agency_rss(
            "https://www.bls.gov/feed/bls_latest.rss",
            source="BLS")
    else:
        rss = fetch_yahoo_rss(arg)
    print(f"抓到 {len(rss)} 条原始 RSS items。调用 Claude CLI 解析中...")
    result = analyze_rss_items(rss, source=arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli_main()
