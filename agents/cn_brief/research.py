"""Free public report metadata projection. Cache-only reader: no AI/network/accounts.

The collector has titles and ratings, NOT PDF bodies. A title digest must never
be presented as a full report summary, nor as our own investment conclusion.
"""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1] / "signals" / "cn_brief"
TTL = timedelta(hours=24)


def _text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _identity(kind: str, ref: str) -> tuple[str, str]:
    if kind == "MACRO" and re.fullmatch(r"[A-Za-z0-9+/]{20,158}={0,2}", ref):
        return "EM" + hashlib.sha256(ref.encode()).hexdigest()[:24], "https://data.eastmoney.com/report/zw_macresearch.jshtml?encodeUrl=" + quote(ref, safe="")
    if kind in ("STOCK", "INDUSTRY") and re.fullmatch(r"AP\d{10,30}", ref):
        url = f"https://data.eastmoney.com/report/info/{ref}.html" if kind == "STOCK" else f"https://data.eastmoney.com/report/zw_industry.jshtml?infocode={ref}"
        return ref, url
    return "", ""


def project(stock: list[dict], industry: list[dict], macro: list[dict]) -> list[dict]:
    """Preserve attributable metadata verbatim, not a model-generated synopsis."""
    result, seen = [], set()
    for kind, rows in (("STOCK", stock), ("INDUSTRY", industry), ("MACRO", macro)):
        selected = []
        for row in sorted(rows, key=lambda r: str(r.get("publishDate") or ""), reverse=True):
            ref = str(row.get("encodeUrl" if kind == "MACRO" else "infoCode") or "")
            info, url = _identity(kind, ref)
            title, org = _text(row.get("title"), 240), _text(row.get("orgSName"), 80)
            day = str(row.get("publishDate") or "")[:10]
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                continue
            if not info or not title or not org or info in seen:
                continue
            seen.add(info)
            selected.append({"id": info, "source_ref": ref, "report_type": kind, "institution": org,
                             "subject": _text(row.get("stockName") if kind == "STOCK" else row.get("industryName") if kind == "INDUSTRY" else "宏观研究", 80),
                             "published_date": day, "title": title,
                             "source": "东方财富研报中心（券商元数据）",
                             "url": url,
                             "content_scope": "TITLE_ONLY_NOT_FULL_REPORT",
                             "one_sentence": f"题摘：{title}",
                             "rating": _text(row.get("emRatingName"), 40),
                             "code": str(row.get("stockCode") or "") if re.fullmatch(r"\d{6}", str(row.get("stockCode") or "")) else ""})
            if len(selected) == 2:
                break
        result.extend(selected)
    return result


def read(*, root: Path = ROOT, now: datetime | None = None) -> dict:
    """Only read existing reports; never trigger fetching, inference or a push."""
    now = (now or datetime.now(JST)).astimezone(JST)
    out = {"version": 1, "scope": "PUBLIC_CN_RESEARCH_NOT_QUOTES", "status": "UNAVAILABLE",
           "generated_at": None, "expires_at": None, "reports": [], "macro_events": [],
           "source_policy": "FREE_PUBLIC_ONLY", "ai_analysis": None,
           "warning": "仅券商标题/评级题摘，未读取正文；机构观点与久留美AI看法须分开。经济日历为第三方转录，不是官方数据原文。"}
    candidates = []
    for slot in ("now", "close"):
        try:
            path = root / f"latest_{slot}.json"
            if path.stat().st_size > 2_000_000:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(raw["generated_at"])
            if generated.tzinfo is None or generated > now + timedelta(seconds=5):
                continue
            # Older caches without a dedicated provenance projection are not raw-text fallbacks.
            if isinstance(raw.get("research_reports"), list):
                candidates.append((generated, raw))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not candidates:
        return out
    generated, raw = max(candidates, key=lambda x: x[0])
    out.update(generated_at=generated.isoformat(), expires_at=(generated + TTL).isoformat())
    if now >= generated + TTL:
        out["status"] = "STALE"
        return out
    reports = []
    for row in raw["research_reports"][:6]:
        try:
            day = datetime.strptime(row["published_date"], "%Y-%m-%d").date()
            info, url = _identity(row["report_type"], row.get("source_ref") or row["id"])
            if not 0 <= (now.date() - day).days <= 7 or not info or info != row["id"]:
                continue
            if row.get("content_scope") != "TITLE_ONLY_NOT_FULL_REPORT" or row["report_type"] not in ("STOCK", "INDUSTRY", "MACRO"):
                continue
            title, org = _text(row["title"], 240), _text(row["institution"], 80)
            if not title or not org:
                continue
            # Rebuild all links/attribution; never forward cached markdown/AI/account fields.
            reports.append({"id": info, "source_ref": row.get("source_ref") or info, "report_type": row["report_type"], "institution": org,
                            "subject": _text(row.get("subject"), 80),
                            "code": str(row.get("code") or "") if re.fullmatch(r"\d{6}", str(row.get("code") or "")) else "",
                            "published_date": day.isoformat(), "title": title,
                            "source": "东方财富研报中心（券商元数据）",
                            "url": url,
                            "content_scope": "TITLE_ONLY_NOT_FULL_REPORT", "one_sentence": f"题摘：{title}",
                            "rating": _text(row.get("rating"), 40)})
        except (KeyError, TypeError, ValueError):
            continue
    events = []
    event_groups = raw.get("events") or {}
    for rows in (event_groups.values() if isinstance(event_groups, dict) else []):
        if not isinstance(rows, list):
            continue
        for row in rows[:100]:
            try:
                ts = datetime.fromisoformat(row["time_jst"])
                if ts.tzinfo is None or not now - timedelta(hours=24) <= ts <= now + timedelta(hours=24):
                    continue
                events.append({"title": _text(row["title"], 180), "region": _text(row["region"], 20),
                               "scheduled_at": ts.astimezone(JST).isoformat(),
                               "actual": _text(row.get("pubVal"), 60), "previous": _text(row.get("formerVal"), 60),
                               "expected": _text(row.get("expectVal") or row.get("indicateVal"), 60),
                               "source": "百度股市通财经日历", "url": "https://gushitong.baidu.com/"})
            except (KeyError, ValueError, TypeError):
                continue
    events.sort(key=lambda r: r["scheduled_at"])
    out.update(status="PARTIAL" if raw.get("errors") or len(reports) != len(raw["research_reports"]) else "OK",
               reports=reports, macro_events=events[:5])
    return out
