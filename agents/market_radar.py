"""Public news radar for chat. No positions, orders, raw AI reports or webhook writes.

GET reads cache only. Explicit refresh reuses news_analyzer (one bounded CLI batch).
The existing watchdog can run tick every 30 minutes; a failed scan is never 'no news'.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from config import SIGNALS_DIR

JST = timezone(timedelta(hours=9))
CACHE = Path(SIGNALS_DIR) / "market_radar.json"
FEEDS = (
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("BLS", "https://www.bls.gov/feed/bls_latest.rss"),
    ("BEA", "https://apps.bea.gov/rss/rss.xml"),
    ("Yahoo SPY", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY&region=US&lang=en-US"),
    ("Yahoo QQQ", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=QQQ&region=US&lang=en-US"),
    ("Yahoo GLD", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=GLD&region=US&lang=en-US"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
)
THEMES = {"QQQ": "科技需求", "SMH": "半导体供需", "XLE": "能源供需", "GLD": "黄金与实际利率",
          "TLT": "利率变化", "XLI": "工业投资", "XLF": "金融条件", "IWM": "小企业融资"}
RELEASES = {"CPI_RELEASE", "PPI_RELEASE", "NFP_RELEASE", "PCE_RELEASE", "FOMC"}


def _time(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def _text(value, limit):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", str(value or ""))).strip()[:limit]


def _url(value):
    try:
        p = urlsplit(value)
        if (p.scheme != "https" or not p.hostname or p.username or p.password
                or p.port not in (None, 443) or p.hostname in {"localhost", "127.0.0.1", "::1"}
                or not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", p.hostname)):
            return ""
        return value[:1000] if len(value) <= 1000 else ""
    except (ValueError, TypeError):
        return ""


def build(batches, *, now, analyze):
    """Pure orchestration around the existing classifier; injectable for offline tests."""
    candidates, seen, coverage = [], set(), []
    for name, rows in batches:
        coverage.append({"source": name, "status": "OK" if rows else "EMPTY_OR_UNAVAILABLE"})
        for raw in rows[:10]:
            try:
                published = parsedate_to_datetime(raw.get("pubDate", ""))
            except (ValueError, TypeError, IndexError):
                continue
            if not published or not published.tzinfo or not timedelta(0) <= now - published <= timedelta(hours=6):
                continue
            title, url = _text(raw.get("title"), 200), _url(raw.get("url", ""))
            key = re.sub(r"\W+", "", title.casefold())
            if not title or not url or key in seen:
                continue
            seen.add(key)
            candidates.append({"title": title, "desc": _text(raw.get("desc"), 600),
                               "source": name, "url": url, "published_at": published.astimezone(JST).isoformat()})
    candidates.sort(key=lambda row: row["published_at"], reverse=True)
    candidates = candidates[:16]
    # One batch only; no unlimited retry and no account-derived watch list.
    parsed = analyze(candidates, source="public-market-radar", watched_tickers=list(THEMES),
                     events_calendar=[], cli_timeout=90, radar_mode=True) if candidates else {"items": [], "fallback": False}
    classified = parsed.get("items", [])
    valid_batch = not parsed.get("fallback") and isinstance(classified, list) and len(classified) == len(candidates)
    accepted, rejected = [], 0
    if valid_batch:
        for raw, item in zip(candidates, classified):
            if not isinstance(item, dict):
                rejected += 1
                continue
            quote = _text(item.get("verbatim_evidence"), 200)
            available = raw["title"] + " " + raw["desc"]
            kind = item.get("event_type")
            confidence = item.get("confidence", 0)
            if (not quote or quote not in available or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float)) or not 7 <= confidence <= 10):
                rejected += 1
                continue
            if kind in RELEASES:
                qualifies = (item.get("is_landed") is True and bool(item.get("period"))
                             and bool(item.get("value")))
            else:
                qualifies = kind in {"BREAKING", "EARNINGS"} and item.get("magnitude") in {"large", "extreme"}
            if not qualifies:
                continue
            accepted.append({"id": hashlib.sha256((raw["url"] + raw["title"]).encode()).hexdigest()[:24],
                             "title": raw["title"], "source": raw["source"], "url": raw["url"],
                             "published_at": raw["published_at"], "evidence": quote, "event_type": kind,
                             "assessment": {"direction": item.get("direction", "neutral"),
                                            "confidence": confidence, "not_market_fact": True},
                             "themes": [t for t in item.get("tickers_affected", []) if t in THEMES][:3]})
    accepted = accepted[:6]
    themes = []
    for ticker, label in THEMES.items():
        related = [item for item in accepted if ticker in item["themes"]]
        domains = {urlsplit(item["url"]).hostname for item in related}
        if len(domains) >= 2:
            themes.append({"theme": label, "evidence_ids": [x["id"] for x in related[:3]],
                           "status": "HYPOTHESIS_ONLY", "independent_confirmation": False,
                           "requires": "核对事件因果、受益环节、市场是否已反映以及失效条件；不同域名不等于独立信源。"})
    complete = valid_batch and not rejected and all(row["status"] == "OK" for row in coverage)
    return {"version": 1, "scope": "PUBLIC_NEWS_NOT_QUOTES", "generated_at": now.astimezone(JST).isoformat(),
            "expires_at": (now + timedelta(minutes=35)).astimezone(JST).isoformat(),
            "status": "OK" if complete else "PARTIAL", "coverage": coverage,
            "classification": "OK" if valid_batch else "UNAVAILABLE_OR_MISALIGNED",
            "rejected_evidence": rejected, "news": accepted[:6], "opportunity_candidates": themes[:2],
            "warning": "仅已覆盖来源的公开消息；不是内幕或未定价证明，不是实时报价，不读取关键持仓。"}


def read(*, path=None, now=None):
    """No fetching, AI, state/cursor changes or fall-back to internal markdown."""
    now = now or datetime.now(JST)
    try:
        target = Path(path or CACHE)
        if target.stat().st_size > 100_000:
            raise ValueError("oversized")
        data = json.loads(target.read_text(encoding="utf-8"))
        generated, expires = _time(data["generated_at"]), _time(data["expires_at"])
        if (data.get("version") != 1 or data.get("scope") != "PUBLIC_NEWS_NOT_QUOTES"
                or not generated or not expires or generated > now or expires <= generated
                or expires - generated > timedelta(minutes=35)):
            raise ValueError("invalid")
        if now >= expires:
            return {"version": 1, "scope": "PUBLIC_NEWS_NOT_QUOTES", "status": "STALE",
                    "generated_at": data["generated_at"], "expires_at": data["expires_at"],
                    "news": [], "opportunity_candidates": [], "warning": "新闻缓存已过期，不能当作最新消息。"}
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return {"version": 1, "scope": "PUBLIC_NEWS_NOT_QUOTES", "status": "UNAVAILABLE",
                "generated_at": None, "expires_at": None, "news": [], "opportunity_candidates": [],
                "warning": "新闻扫描尚未成功或缓存无效，不能据此判断没有重要消息。"}


def refresh(*, path=None, now=None, fetch=None, analyze=None):
    from news_analyzer import analyze_rss_items, fetch_agency_rss
    now = now or datetime.now(JST)
    fetch = fetch or fetch_agency_rss
    target = Path(path or CACHE)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(".lock")
    # Never steal a stale lock automatically; operator can diagnose a crashed refresh.
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return {"status": "BUSY"}
    try:
        def collect(feed):
            name, url = feed
            try:
                return name, fetch(url, source=name, timeout=8)
            except Exception:
                return name, []
        with ThreadPoolExecutor(max_workers=6) as pool:
            batches = list(pool.map(collect, FEEDS))
        data = build(batches, now=now, analyze=analyze or analyze_rss_items)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        return {"status": data["status"], "news": len(data["news"]),
                "candidates": len(data["opportunity_candidates"]), "classification": data["classification"]}
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) == 2 else "read"
    if command == "tick":
        # Reuse a completed recent scan, including partial failures; bounded retry cadence.
        previous = read()
        t = _time(previous.get("generated_at"))
        result = {"status": "RECENT"} if t and datetime.now(JST) - t < timedelta(minutes=30) else refresh()
    elif command == "refresh":
        result = refresh()
    elif command == "read":
        result = read()
    else:
        raise SystemExit("read | refresh | tick")
    print(json.dumps(result, ensure_ascii=False))
