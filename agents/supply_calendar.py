"""供给冲击日历 (2026-10-05, 用户确认): 减持 / 增发 / 解禁 + 指数调仓日.

为什么: "砸盘" 多数来自可预见的集中供给 —— 投行盘后折价包销大宗、IPO 锁定期
到期、指数调仓与期权大到期. 这里把它们做成日历, 决策时:
  · 个股供给事件窗口内 → 新买入降级 HOLD (supply_guard), 不卖出, 不改仓位;
  · 市场级资金流日 (指数调仓 / 季度期权到期 / 季末) → 只在决策里标注 flow_day.

数据 (只读, 免费):
  · SEC EDGAR submissions (每家公司的申报列表, 带申报日):
      424B7         → 股东转售 (secondary)         窗口: 申报日起 3 个交易日
      424B4 / 424B5 → 增发 / 包销                   窗口: 申报日起 3 个交易日
      IPO 424B4     → 锁定期到期 ≈ IPO + 180 天    窗口: 到期前 3 个交易日 ~ 后 1 个交易日
  · 424B5 / 非 IPO 424B4 的债务/股票判断读封面页文本 (2026-10-22): 描述字段
    几乎全是 "424B5", 只靠关键词会把结构化票据 / 债券当成增发 (样本假阳性 ~74%).
    封面页正文按强债务 / 强股票 / ATM 词表打分; 文档拉取失败或无法判定 → 不计
    事件, 但在 classification 统计里可见. 文档缓存在 .webui_cache (可重生).
  · 指数日历: 标普季度调仓 = 3/6/9/12 月第三个周五 (同季度期权到期);
    罗素重组 (2026 起半年一次) 用官方公布日期, 未公布的不猜.

运行: WebUI 看门狗白名单任务每天收盘后 (_supply_calendar.bat), 输出
signals/supply_calendar.json. 读取方:
  · 文件缺失 / 超过 STALE_DAYS 天 / 当日抓取全失败 / 无股票状态 →
    只把 asset_class == "equity" 的新买入降级 HOLD; ETF/债券/商品不受影响.
  · 单只股票 error / no_cik / 未覆盖 → 只降级该股票的新买入.
  · 卖出 / 减仓永远不受影响. 数据降级每天告警一次 (股票风险敞口可见).
已知局限: 锁定期按 180 天估算 (部分公司分批解禁或提前解禁); Form 144
(内部人减持预告) 未纳入; 封面页分类是启发式, 无法判定的申报不计事件.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

AGENTS = Path(__file__).resolve().parent
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

OUT_PATH = AGENTS / "signals" / "supply_calendar.json"
DOC_CACHE = AGENTS / ".webui_cache" / "supply_calendar_docs"
STALE_DAYS = 3
LOOKBACK_DAYS = 30
LOCKUP_DAYS = 180
OFFERING_FORMS = {"424B4", "424B5", "424B7"}
DEBT_WORDS = ("NOTE", "DEBT", "BOND", "DEBENTURE", "SENIOR", "PREFERRED")
# 罗素美国指数重组生效日 (收盘后生效). 来源: LSEG/FTSE Russell 公告. 只写已公布的.
RUSSELL_RECON = {
    "2026-12-11": "https://www.lseg.com/en/media-centre/press-releases/ftse-russell/2026/"
                  "ftse-russell-announces-december-2026-russell-us-indexes-reconstitution-schedule",
}

# ---------------- 424B 封面页分类 (2026-10-22) ----------------
# 只看封面页前 12k 字符. 强债务/强股票词表来自 2026-10 样本 (n=493) 的人工核对:
# 424B5 里 86% 是债券/结构化票据, 描述字段几乎没有区分力.
_STRONG_DEBT = {
    "notes_due": r"\bnotes?\s+due\b",
    "senior_notes": r"\bsenior\s+notes?\b",
    "aggregate_principal": r"\baggregate\s+principal\s+amount\b",
    "principal_amount": r"\bprincipal\s+amount\s+of\b",
    "subordinated_notes": r"\bsubordinated\s+notes?\b",
}
_MED_DEBT = {
    "indenture": r"\bindenture\b",
    "debentures": r"\bdebentures?\b",
    "interest_rate": r"\binterest\s+rate\b",
}
_STRONG_EQUITY = {
    "common_stock": r"\bshares?\s+of\s+(?:our\s+)?(?:class\s+[a-z]\s+)?common\s+stock\b",
    "ordinary_shares": r"\bshares?\s+of\s+(?:our\s+)?(?:class\s+[a-z]\s+)?ordinary\s+shares\b",
    "class_a_shares": r"\bclass\s+[a-z]\s+(?:ordinary|common)\s+shares?\b",
    "selling_stockholders": r"\bselling\s+(?:stock|note)?holders?\b",
    "ipo": r"\binitial\s+public\s+offering\b",
}
_ATM = {
    "equity_distribution": r"\bequity\s+distribution\s+agreement\b",
    "at_the_market": r"\bat[- ]the[- ]market\b",
}


def _is_session(d: date) -> bool:
    try:
        from regime_today import is_nyse_session
        return is_nyse_session(d)
    except Exception:
        return d.weekday() < 5


def add_sessions(d: date, n: int) -> date:
    """d 起第 n 个交易日 (n 可为负); d 本身不是交易日时先挪到下一个交易日."""
    step = 1 if n >= 0 else -1
    cur = d
    while not _is_session(cur):
        cur += timedelta(days=1)
    k = abs(n)
    while k:
        cur += timedelta(days=step)
        if _is_session(cur):
            k -= 1
    return cur


# ---------------- 解析 EDGAR submissions ----------------
def _rows(sub: dict) -> list[dict]:
    r = (sub.get("filings") or {}).get("recent") or {}
    keys = ("form", "filingDate", "accessionNumber", "primaryDocument", "primaryDocDescription")
    cols = [r.get(k) or [] for k in keys]
    n = min((len(c) for c in cols[:3]), default=0)
    out = []
    for i in range(n):
        out.append({k: (c[i] if i < len(c) else "") for k, c in zip(keys, cols)})
    return out


def _url(cik: int, row: dict) -> str:
    acc = str(row.get("accessionNumber", "")).replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{row.get('primaryDocument', '')}"


def _clean_html(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def _first_pos(patterns: dict, text: str) -> int:
    return min((m.start() for p in patterns.values() if (m := re.search(p, text))),
               default=10 ** 9)


def classify_cover_text(raw: str) -> str:
    """封面页文本 → debt / equity / atm / unknown. 只看前 12k 字符."""
    text = _clean_html(raw)[:12000].lower()
    cover = text[:3000]
    sd = sum(len(re.findall(p, text)) for p in _STRONG_DEBT.values())
    md = sum(len(re.findall(p, text)) for p in _MED_DEBT.values())
    se = sum(len(re.findall(p, text)) for p in _STRONG_EQUITY.values())
    atm = sum(len(re.findall(p, cover)) for p in _ATM.values())
    if atm and se:
        return "atm"
    if sd and not se:
        return "debt"
    if se and not sd:
        return "equity"
    if sd and se:
        return "debt" if _first_pos(_STRONG_DEBT, cover) < _first_pos(_STRONG_EQUITY, cover) else "equity"
    if md >= 2 and not se:
        return "debt"
    return "unknown"


def _get_document(url: str, ua: str, retries: int = 2) -> str | None:
    """SEC 文档正文 (前 600KB), 缓存在 .webui_cache. 失败返回 None."""
    import hashlib
    # 按完整 URL 取 key: primaryDocument 常见同名 (424b5.htm), 只用文件名会跨公司串味.
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache = DOC_CACHE / f"{key}.txt"
    if cache.exists():
        try:
            return cache.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    import urllib.request
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read(600_000).decode("utf-8", "ignore")
            txt = _clean_html(raw)
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(txt, encoding="utf-8")
            except Exception:
                pass
            return txt
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    print(f"[supply_calendar] document fetch failed {url}: {last}", file=sys.stderr)
    return None


def classify_filing(row: dict, cik: int, ua: str,
                    get_document: Callable[[str, str], str | None] | None = None) -> str:
    """424B5 / 非 IPO 424B4 → debt / equity / atm / unknown."""
    form = row.get("form", "")
    if form == "424B7":
        return "secondary"
    desc = f"{row.get('primaryDocDescription', '')} {row.get('primaryDocument', '')}".upper()
    if form == "424B5" and any(w in desc for w in DEBT_WORDS):
        return "debt"
    fetch = get_document or _get_document
    text = fetch(_url(cik, row), ua)
    if not text:
        return "unknown"
    return classify_cover_text(text)


def events_from_submissions(sub: dict, today: date, lookback_days: int = LOOKBACK_DAYS,
                            classify: Callable[[dict, int], str] | None = None,
                            stats: dict | None = None) -> list[dict]:
    """一家公司的供给事件 (只用 today 当天及之前的申报).

    classify=None 时保留旧行为 (仅按描述字段排除债券); 生产路径传入封面页
    分类器. stats 若提供 → 按 classification 累加计数.
    """
    cik = int(sub.get("cik") or 0)
    rows = [r for r in _rows(sub) if r["filingDate"] and r["filingDate"] <= today.isoformat()]
    periodic = sorted(r["filingDate"] for r in rows if r["form"] in ("10-K", "10-Q", "20-F"))
    out = []

    def bump(kind: str) -> None:
        if stats is not None:
            stats[kind] = stats.get(kind, 0) + 1

    for r in rows:
        form, fd = r["form"], date.fromisoformat(r["filingDate"])
        if form not in OFFERING_FORMS:
            continue
        desc = f"{r.get('primaryDocDescription', '')} {r.get('primaryDocument', '')}".upper()
        if form == "424B5" and any(w in desc for w in DEBT_WORDS):
            bump("debt")
            continue
        is_ipo = form == "424B4" and not any(p < r["filingDate"] for p in periodic)
        if is_ipo:
            if lock_due(fd, today):
                bump("lockup")
                out.append(lockup_event(fd, cik, r))
            continue
        if (today - fd).days > lookback_days:
            continue
        kind = "equity"
        if form == "424B7":
            kind = "secondary"
        elif classify is not None:
            try:
                kind = classify(r, cik)
            except Exception:
                kind = "error"
            if kind == "debt":
                bump("debt")
                continue
            if kind not in ("equity", "atm"):
                bump(kind if kind in ("unknown", "error") else "unknown")
                continue
        bump(kind if kind in ("secondary", "equity", "atm") else "equity")
        out.append({"type": "secondary_sale" if form == "424B7" else "offering",
                    "date": fd.isoformat(),
                    "window": [fd.isoformat(), add_sessions(fd, 2).isoformat()],
                    "form": form, "url": _url(cik, r),
                    "classification": kind if classify is not None else "description"})
    return sorted(out, key=lambda e: e["date"])


def lock_due(fd: date, today: date) -> bool:
    return fd + timedelta(days=LOCKUP_DAYS) + timedelta(days=10) >= today


def lockup_event(fd: date, cik: int, row: dict) -> dict:
    lock = fd + timedelta(days=LOCKUP_DAYS)
    return {"type": "lockup_expiry", "date": lock.isoformat(),
            "window": [add_sessions(lock, -3).isoformat(), add_sessions(lock, 1).isoformat()],
            "form": row["form"], "ipo_filing_date": fd.isoformat(), "url": _url(cik, row),
            "note": f"IPO {fd} + {LOCKUP_DAYS} 天 (估算)"}


def market_flow_days(today: date, horizon_days: int = 120) -> list[dict]:
    """标普季度调仓 / 季度期权到期 (3/6/9/12 月第三个周五), 罗素重组, 季末."""
    out = []
    end = today + timedelta(days=horizon_days)
    y, m = today.year, today.month
    while date(y, m, 1) <= end:
        if m in (3, 6, 9, 12):
            first = date(y, m, 1)
            fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
            if not _is_session(fri):
                fri = add_sessions(fri, -1)
            out.append({"type": "sp500_rebalance_quad_witching", "date": fri.isoformat(),
                        "note": "标普季度调仓收盘生效 + 季度期权到期"})
            last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
            while not _is_session(last):
                last -= timedelta(days=1)
            out.append({"type": "quarter_end", "date": last.isoformat(), "note": "季末机构再平衡"})
        m += 1
        if m == 13:
            y, m = y + 1, 1
    for d, src in RUSSELL_RECON.items():
        out.append({"type": "russell_reconstitution", "date": d, "note": "罗素重组收盘生效", "source": src})
    return sorted([e for e in out if today.isoformat() <= e["date"] <= end.isoformat()],
                  key=lambda e: e["date"])


# ---------------- 决策用读取 ----------------
_CACHE: dict = {"mtime": None, "data": None}


def _age_days(mtime: float, data: dict) -> float:
    """文件年龄; generated_at 更旧时以它为准 (防止复制/触碰文件伪造新鲜度)."""
    age = (time.time() - mtime) / 86400
    ts = data.get("generated_at")
    if ts:
        try:
            gen = datetime.fromisoformat(str(ts)).timestamp()
            age = max(age, (time.time() - gen) / 86400)
        except Exception:
            pass
    return age


def load(path: Path | None = None) -> dict | None:
    p = Path(path) if path is not None else OUT_PATH
    if not p.exists():
        return None
    mt = p.stat().st_mtime
    if path is None and _CACHE["mtime"] == mt and _CACHE["data"] is not None:
        data = _CACHE["data"]
        data["_age_days"] = _age_days(mt, data)
        return data
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    data["_age_days"] = _age_days(mt, data)
    if path is None:
        _CACHE.update(mtime=mt, data=data)
    return data


def active_events(ticker: str, today: date, data: dict | None) -> list[dict]:
    if not data or data.get("_age_days", 0) > STALE_DAYS:
        return []
    tk = ticker.replace("US.", "").upper()
    t = today.isoformat()
    return [e for e in (data.get("tickers") or {}).get(tk, [])
            if e.get("window") and e["window"][0] <= t <= e["window"][1]]


def flow_days_near(today: date, data: dict | None, days: int = 1) -> list[dict]:
    if not data or data.get("_age_days", 0) > STALE_DAYS:
        return []
    lo, hi = (today - timedelta(days=days)).isoformat(), (today + timedelta(days=days)).isoformat()
    return [e for e in data.get("market_days", []) if lo <= e["date"] <= hi]


# ---------------- 资产类别 / 健康度 ----------------
def is_equity(ticker: str) -> bool:
    """股票 (含未注册的新 ticker, 保守视为股票). sector/leveraged ETF 不算."""
    if not ticker:
        return False
    try:
        from instrument_registry import get as _get
        inst = _get(ticker)
    except Exception:
        inst = None
    if inst is not None:
        return inst.asset_class == "equity"
    try:
        from config import get_asset_class
        return get_asset_class(ticker) == "equity_single"
    except Exception:
        return True


def health(data: dict | None = None) -> dict:
    """日历健康度. block_equity=True → 所有股票新买入应暂停 (ETF/债券/商品不受影响)."""
    data = load() if data is None else data
    if not data:
        return {"state": "missing", "block_equity": True, "age_days": None,
                "ok": 0, "error": 0, "no_cik": 0, "expected": 0}
    age = float(data.get("_age_days") or 0)
    status = data.get("status")
    base = {"age_days": round(age, 2), "generated_at": data.get("generated_at"),
            "run_status": data.get("run_status"),
            "counts": data.get("counts") or {},
            "classification": data.get("classification") or {}}
    if age > STALE_DAYS:
        return {**base, "state": "stale", "block_equity": True,
                "ok": _count(status, "ok"), "error": _count(status, "error"),
                "no_cik": _count(status, "no_cik"), "expected": _expected(data, status)}
    if not isinstance(status, dict):
        return {**base, "state": "no_status", "block_equity": True,
                "ok": 0, "error": 0, "no_cik": 0, "expected": 0}
    errors = {tk: s for tk, s in status.items() if str(s).startswith("error")}
    no_cik = {tk: s for tk, s in status.items() if s == "no_cik"}
    eq_ok = [tk for tk, s in status.items() if s == "ok" and is_equity(tk)]
    eq_bad = {tk: s for tk, s in {**errors, **no_cik}.items() if is_equity(tk)}
    state = "ok"
    if data.get("run_status") == "failed" or not eq_ok:
        state, block = "run_failed" if data.get("run_status") == "failed" else "no_equity_data", True
    elif errors or eq_bad or data.get("run_status") == "partial":
        state, block = "partial", False
    else:
        block = False
    return {**base, "state": state, "block_equity": block,
            "ok": _count(status, "ok"), "error": len(errors), "no_cik": len(no_cik),
            "expected": _expected(data, status),
            "errors": errors, "equity_errors": eq_bad}


def _count(status, prefix: str) -> int:
    if not isinstance(status, dict):
        return 0
    if prefix == "error":
        return sum(1 for s in status.values() if str(s).startswith("error"))
    return sum(1 for s in status.values() if s == prefix)


def _expected(data: dict, status) -> int:
    try:
        return int((data.get("counts") or {}).get("expected") or len(status or {}))
    except Exception:
        return len(status or {})


def ticker_health(ticker: str, data: dict | None = None, h: dict | None = None) -> str:
    """ok / not_equity / missing / error / no_cik / 日历整体状态."""
    if not is_equity(ticker):
        return "not_equity"
    data = load() if data is None else data
    h = health(data) if h is None else h
    if h.get("block_equity"):
        return h.get("state", "unknown")
    status = (data or {}).get("status") or {}
    tk = ticker.replace("US.", "").upper()
    s = status.get(tk)
    if s is None:
        return "ticker_missing"
    if str(s).startswith("error"):
        return "ticker_error"
    if s == "no_cik":
        return "ticker_no_cik"
    return "ok"


_BLOCK_LABEL = {
    "missing": "日历文件缺失",
    "stale": "日历过期",
    "no_status": "日历缺少状态字段",
    "run_failed": "日历当日抓取失败",
    "no_equity_data": "日历无有效股票数据",
    "ticker_missing": "该标的本轮未覆盖",
    "ticker_error": "该标的抓取失败",
    "ticker_no_cik": "该标的无 SEC CIK",
    "reader_error": "日历读取异常",
}


def notify_equity_block(kind: str, ticker: str = "", detail: str = "") -> None:
    """数据降级导致股票新买入暂停 → 每天一次可见告警 (不刷屏)."""
    try:
        from notifications import send_alert, _alert_sent_today, _record_alert_sent
        event = f"supply_calendar_block_{kind}"
        if _alert_sent_today(event, "warning"):
            return
        scope = f"（最近触发: {ticker.replace('US.', '')}）" if ticker else ""
        msg = (f"供给日历{_BLOCK_LABEL.get(kind, kind)} → 股票新买入暂停"
               f"{scope}; equity-only, 卖出/减仓与 ETF/债券/商品不受影响")
        if detail:
            msg += f"; {detail[:120]}"
        send_alert(msg, level="warning", dedup=True)
        _record_alert_sent(event, "warning")
    except Exception:
        pass


# ---------------- 联网 (Windows) ----------------
def _ua() -> str:
    p = AGENTS / ".sec_user_agent"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _get_json(url: str, ua: str):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def tracked_tickers() -> list[str]:
    from config import TRADE_ELIGIBLE_TICKERS
    tks = {t.replace("US.", "") for t in TRADE_ELIGIBLE_TICKERS}
    try:
        st = json.loads((AGENTS / "universe_state.json").read_text(encoding="utf-8"))
        tks |= {str(p.get("ticker") if isinstance(p, dict) else p).replace("US.", "")
                for p in st.get("picks") or []}
    except Exception:
        pass
    return sorted(t for t in tks if t)


def _run_status(tracked: list[str], status: dict) -> str:
    eq_ok = [tk for tk in tracked if is_equity(tk) and status.get(tk) == "ok"]
    if not eq_ok:
        return "failed"
    if any(str(status.get(tk, "")).startswith("error") or status.get(tk) in (None, "no_cik")
           for tk in tracked if is_equity(tk)):
        return "partial"
    return "ok"


def _write(out_path: Path, data: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)


def run(today: date | None = None, out_path: Path = OUT_PATH) -> dict:
    today = today or datetime.now(timezone.utc).date()
    ua = _ua()
    if "@" not in ua:
        raise SystemExit("agents/.sec_user_agent 缺失 (SEC 要求请求头带联系方式)")
    tracked = tracked_tickers()
    tickers, status, cls_stats = {}, {}, {}
    try:
        tick = _get_json("https://www.sec.gov/files/company_tickers_exchange.json", ua)
        cik_of = {str(r[2]).upper(): int(r[0]) for r in tick.get("data", [])}
    except Exception as e:
        status = {tk: f"error: ticker_map {e}" for tk in tracked}
        cik_of = {}
    for tk in tracked if cik_of else []:
        cik = cik_of.get(tk)
        if not cik:
            status[tk] = "no_cik"
            continue
        stats = {"debt": 0, "equity": 0, "atm": 0, "secondary": 0,
                 "lockup": 0, "unknown": 0, "error": 0}
        try:
            sub = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua)
            ev = events_from_submissions(
                sub, today,
                classify=lambda row, c: classify_filing(row, c, ua),
                stats=stats)
            if ev:
                tickers[tk] = ev
            status[tk] = "ok"
        except Exception as e:
            status[tk] = f"error: {e}"
        cls_stats[tk] = stats
        time.sleep(0.2)
    counts = {"expected": len(tracked), "ok": _count(status, "ok"),
              "error": _count(status, "error"), "no_cik": _count(status, "no_cik")}
    data = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": today.isoformat(), "tickers": tickers,
            "market_days": market_flow_days(today), "status": status,
            "run_status": _run_status(tracked, status) if status else "failed",
            "counts": counts, "classification": cls_stats}
    _write(out_path, data)
    h = health(load(path=out_path))
    if h["state"] in ("run_failed", "no_equity_data", "partial"):
        notify_equity_block(h["state"], detail=f"status={counts}")
    print(json.dumps({k: data[k] for k in ("generated_at", "run_status", "counts",
                                           "tickers", "status")}, ensure_ascii=False)[:3000])
    return data


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log = AGENTS / "logs" / "supply_calendar_py.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"===== {datetime.now(timezone.utc).isoformat()} =====\n{traceback.format_exc()}\n")
        raise SystemExit(1)
