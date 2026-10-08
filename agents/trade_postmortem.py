"""卖出复盘 (2026-10-08 用户要求): 定期让 AI 读 log, 对每笔卖出做复盘.

每笔卖出输出:
  · 当时那个时段的技术面 + 消息面概况
  · 5 条"当时为什么卖"的理由 (每条标注 技术面/消息面, 附 log 或新闻原文证据)
  · 3 条局限性 (这些理由/这次卖出本身的局限)
  · 事后走势 (只作参考, 不当作当时的理由)

材料 (只读):
  · broker 成交 (fill_ledger) + trade_log 里的触发标签/决策原因
  · logs/run_YYYYMMDD.log (日本时间) 中卖出前 8 小时 ~ 后 1 小时: 该标的信号块 + 大盘/宏观/期权流行
  · signals/news_cache 里 AI 已解析的新闻 + (在 Windows 上运行时) Yahoo RSS 该标的头条
  · 卖出后 36 小时内 log 里的价格 + yfinance 后 3 个交易日收盘
补现金卖单 (REBALANCE CASH) 是机械操作, 只列出不复盘.
已复盘的订单记在 signals/postmortem/reviewed.json, 不重复; AI 失败的下次重试.
不下单, 不改仓位.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIGNALS = HERE / "signals"
LOGS = HERE / "logs"
OUT_DIR = SIGNALS / "postmortem"
REVIEWED = OUT_DIR / "reviewed.json"
LATEST = OUT_DIR / "latest.json"
JST = timezone(timedelta(hours=9))
LOOKBACK_DAYS = 7
BEFORE_H, AFTER_H, HINDSIGHT_H = 8, 1, 36
MAX_EXCERPT_LINES = 160
MARKET_PATTERNS = ("REGIME =", "SPY 今日", "[宏观]", "期货", "解读:", "[options-flow]", "[cash]",
                   "risk-monitor", "VIX", "盘前", "breaking", "Trump", "事件:")
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


# ── 材料收集 (纯函数尽量多, 便于测试) ─────────────────────────────────────────
def parse_log(path: Path) -> list[tuple[datetime | None, str]]:
    """返回 [(utc 时间 or None(续行), 文本)]. log 时间戳是日本时间."""
    out = []
    last = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = _LOG_TS.match(line)
        if m:
            last = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).astimezone(timezone.utc)
            out.append((last, line))
        else:
            out.append((None, line))
    return out


def load_log_window(t: datetime, before_h: float, after_h: float, logs_dir: Path = LOGS) -> list[tuple[datetime, str]]:
    start, end = t - timedelta(hours=before_h), t + timedelta(hours=after_h)
    days = {(start.astimezone(JST) + timedelta(days=i)).strftime("%Y%m%d")
            for i in range((end - start).days + 2)}
    rows = []
    for d in sorted(days):
        cur = None
        for ts, line in parse_log(logs_dir / f"run_{d}.log"):
            cur = ts or cur
            if cur is not None and start <= cur <= end:
                rows.append((cur, line))
    return rows


def select_excerpt(rows: list[tuple[datetime, str]], ticker: str, max_lines: int = MAX_EXCERPT_LINES) -> list[str]:
    """该标的信号块 (【TICKER】 起到空行) + 提到该标的的行 + 大盘/宏观行."""
    bare = ticker.replace("US.", "")
    keep, block = [], None          # block: 当前所在信号块的标的 (None = 不在块内)
    for _, line in rows:
        body = line[20:] if _LOG_TS.match(line) else line
        m = re.search(r"【([^】]+)】", line)
        if m:
            block = m.group(1)
        elif block is not None and (not body.strip() or not body.startswith(" ")):
            block = None            # 信号块的续行都是缩进行; 空行或顶格行 = 块结束
        if block is not None and block != bare:
            continue                # 其它标的的信号块 (含它们的"盘前跌"等行) 不进来
        if block == bare or re.search(rf"\b(US\.)?{re.escape(bare)}\b", line) or any(p in line for p in MARKET_PATTERNS):
            keep.append(line.rstrip())
    # 去重保序, 太长时保留首尾
    seen, uniq = set(), []
    for l in keep:
        body = l[20:] if _LOG_TS.match(l) else l      # 同一句话不同时间戳只留第一次
        if body not in seen:
            seen.add(body); uniq.append(l)
    if len(uniq) > max_lines:
        uniq = uniq[: max_lines // 2] + ["... (中间省略) ..."] + uniq[-max_lines // 2:]
    return uniq


def price_path_after(rows: list[tuple[datetime, str]], ticker: str) -> list[str]:
    bare = ticker.replace("US.", "")
    return [l.rstrip() for _, l in rows if f"【{bare}】价格" in l][:12]


def news_from_cache(ticker: str, t: datetime, cache_dir: Path = SIGNALS / "news_cache") -> list[dict]:
    bare = ticker.replace("US.", "")
    out = []
    for f in cache_dir.glob("news_parsed_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(d.get("ts")).replace(tzinfo=JST) if d.get("ts") else None
        except Exception:
            continue
        if ts is None or not (t - timedelta(hours=24) <= ts.astimezone(timezone.utc) <= t + timedelta(hours=2)):
            continue
        for it in d.get("items") or []:
            aff = [a.upper() for a in it.get("tickers_affected") or []]
            if it.get("event_type") == "NOISE":
                continue
            if bare in aff or any(a in aff for a in ("SPY", "QQQ")) or not aff:
                out.append({"time_jst": ts.strftime("%m-%d %H:%M"), "type": it.get("event_type"),
                            "direction": it.get("direction"), "magnitude": it.get("magnitude"),
                            "evidence": it.get("verbatim_evidence"), "source": d.get("source")})
    return out[:20]


def news_symbols(ticker: str) -> list[str]:
    """标的本身 + 它跟踪的底层/板块 (杠杆 ETF 自己几乎没有新闻) + 大盘."""
    bare = ticker.replace("US.", "")
    syms = [bare]
    try:
        from option_flow import POSITION_PROXY_MAP
        syms += [p["source"] for p in POSITION_PROXY_MAP.get(bare, [])]
    except Exception:
        pass
    syms += ["QQQ", "SPY"]
    return list(dict.fromkeys(syms))


def news_from_rss(ticker: str, t: datetime) -> list[dict]:
    try:
        from news_analyzer import fetch_yahoo_rss
    except Exception:
        return []
    out = []
    for sym in news_symbols(ticker):
        try:
            items = fetch_yahoo_rss(sym)
        except Exception:
            continue
        for it in items:
            try:
                pub = parsedate_to_datetime(it.get("pubDate"))
            except Exception:
                continue
            if t - timedelta(hours=24) <= pub <= t + timedelta(hours=2):
                out.append({"time_utc": pub.astimezone(timezone.utc).strftime("%m-%d %H:%M"),
                            "symbol": sym, "title": it.get("title")})
    seen, uniq = set(), []
    for n in out:
        if n["title"] not in seen:
            seen.add(n["title"]); uniq.append(n)
    return uniq[:25]


def label_closes(rows: list[dict], sell_date_et: str) -> list[dict]:
    """给日收盘标上相对卖出日的位置: D-1 / D0 (卖出当天收盘) / D+1 ..."""
    dates = [r["date"] for r in rows]
    base = next((i for i, d in enumerate(dates) if d >= sell_date_et), len(dates))
    return [dict(r, rel=f"D{i - base:+d}" if i != base else "D0") for i, r in enumerate(rows)]


def closes_after(ticker: str, t: datetime, n: int = 3) -> list[dict]:
    try:
        import yfinance as yf
        df = yf.Ticker(ticker.replace("US.", "")).history(start=(t - timedelta(days=4)).date().isoformat(),
                                                           interval="1d", auto_adjust=False)
        rows = [{"date": i.strftime("%Y-%m-%d"), "close": round(float(c), 4)} for i, c in df["Close"].items()]
    except Exception:
        return []
    et_date = (t - timedelta(hours=4)).date().isoformat()   # 美东日期 (夏令时近似)
    labeled = label_closes(rows, et_date)
    return [r for r in labeled if r["rel"] in ("D-1", "D0") or r["rel"].startswith("D+")][: n + 2]


def collect_sells(days: int = LOOKBACK_DAYS) -> list[dict]:
    import fill_ledger
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sells = [r for r in fill_ledger.statement_rows(n=500, since=since) if r["side"] == "SELL"]
    tl = {}
    try:
        for line in (SIGNALS / "trade_log.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if d.get("order_id"):
                    tl[str(d["order_id"])] = d
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    for s in sells:
        e = tl.get(str(s["order_id"])) or {}
        s["tag"] = e.get("tag") or ""
        s["decision_ts"] = e.get("ts") or s["ts"]
        s["decision"] = {k: v for k, v in (e.get("decision") or {}).items() if v not in (None, "", {})}
        s["mechanical_cash"] = "REBALANCE CASH" in s["tag"]
    return sells


# ── AI ───────────────────────────────────────────────────────────────────────
def build_prompt(sell: dict, excerpt: list[str], news: list[dict], after_log: list[str], after_closes: list[dict]) -> str:
    return f"""你是交易复盘分析师。下面是一个自动交易系统的一笔**卖出**和当时的原始材料。
只依据这些材料作答, 不要编造材料里没有的新闻或数字; 材料不足就明确写"材料不足".

## 卖出
标的 {sell['ticker']} | 卖出 {sell['qty']} 股 @ ${sell['price']} | 下单时间 (UTC) {sell['decision_ts']}
触发标签: {sell['tag'] or '无'}
系统决策字段: {json.dumps(sell.get('decision') or {}, ensure_ascii=False)}

## 当时 log 摘录 (日本时间, 卖出前 {BEFORE_H} 小时 ~ 后 {AFTER_H} 小时)
{chr(10).join(excerpt) or '(无)'}

## 当时新闻
{json.dumps(news, ensure_ascii=False, indent=0) or '(无)'}

## 事后 (只用于"事后走势"一栏, 不得当作当时的卖出理由)
日收盘 (D-1 = 卖出前一天, D0 = 卖出当天, D+1 = 之后一天): {json.dumps(after_closes, ensure_ascii=False)}
注意: log 里"【标的】价格"是日线参考价 (常是前一交易日收盘), 不是卖出时的实时价.

## 要求
1. 概括卖出那个时段的技术面和消息面 (各 1-3 句).
2. 正好 5 条"当时卖出"的理由, 每条标注 "技术面" 或 "消息面", 并给出材料里的证据原文或数值. 消息面材料不足时可以全是技术面, 但要在局限性里说明.
3. 正好 3 条局限性: 这些理由或这次卖出本身可能错在哪里 (例如流动性、信号滞后、数据缺失、只看单一指标、规则机械).
4. 事后走势一句话 + 这次卖出事后看是否合理 (合理/不合理/难判断).
只输出 JSON, 不要任何其他文字:
{{"context": {{"technical": "...", "news": "..."}},
 "reasons": [{{"type": "技术面|消息面", "reason": "...", "evidence": "..."}}],
 "limitations": [{{"limitation": "...", "why": "..."}}],
 "hindsight": {{"summary": "...", "verdict": "合理|不合理|难判断"}}}}
"""


def parse_ai(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    reasons, lims = d.get("reasons"), d.get("limitations")
    if not (isinstance(reasons, list) and len(reasons) == 5 and isinstance(lims, list) and len(lims) == 3):
        return None
    if any(r.get("type") not in ("技术面", "消息面") or not r.get("reason") for r in reasons):
        return None
    if any(not l.get("limitation") for l in lims):
        return None
    return d


def review_one(sell: dict, *, ai=None, live_fetch: bool = True) -> dict:
    t = datetime.fromisoformat(sell["decision_ts"].replace("Z", "+00:00"))
    rows = load_log_window(t, BEFORE_H, AFTER_H)
    excerpt = select_excerpt(rows, sell["ticker"])
    news = news_from_cache(sell["ticker"], t) + (news_from_rss(sell["ticker"], t) if live_fetch else [])
    after_log: list[str] = []   # 不再用 log 价格做事后走势 (是日线参考价, 易误读)
    after_closes = closes_after(sell["ticker"], t) if live_fetch else []
    prompt = build_prompt(sell, excerpt, news, after_log, after_closes)
    if ai is None:
        from ai_prompt import query_ai_cli
        ai = lambda p: query_ai_cli(p, timeout=300, complexity="complex")
    out, status, provider, fb = ai(prompt)
    parsed = parse_ai(out)
    return {"order_id": sell["order_id"], "ticker": sell["ticker"], "qty": sell["qty"], "price": sell["price"],
            "ts": sell["decision_ts"], "tag": sell["tag"], "provider": provider, "status": status,
            "ok": parsed is not None, "analysis": parsed,
            "material": {"excerpt_lines": len(excerpt), "news_items": len(news), "after_closes": after_closes},
            "reviewed_at": datetime.now(timezone.utc).isoformat()}


def run(days: int = LOOKBACK_DAYS, *, ai=None, live_fetch: bool = True) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reviewed = json.loads(REVIEWED.read_text(encoding="utf-8")) if REVIEWED.exists() else {}
    sells = collect_sells(days)
    done, mechanical = [], []
    for s in sells:
        if s["mechanical_cash"]:
            mechanical.append({k: s[k] for k in ("ts", "ticker", "qty", "price", "tag", "order_id")})
            continue
        if str(s["order_id"]) in reviewed:
            continue
        r = review_one(s, ai=ai, live_fetch=live_fetch)
        print(f"[postmortem] {s['ticker']} {s['order_id']} ok={r['ok']} provider={r['provider']} status={str(r['status'])[:80]}")
        if r["ok"]:
            day = r["ts"][:10]
            (OUT_DIR / f"{day}_{r['ticker'].replace('US.', '')}_{r['order_id']}.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            reviewed[str(s["order_id"])] = r["reviewed_at"]
        done.append(r)
    REVIEWED.write_text(json.dumps(reviewed, ensure_ascii=False, indent=1), encoding="utf-8")
    # latest: 最近 20 份成功复盘
    files = sorted(OUT_DIR.glob("20*_*.json"), reverse=True)[:20]
    latest = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "reviews": [json.loads(f.read_text(encoding="utf-8")) for f in files],
              "mechanical_cash_sells": mechanical[:20],
              "last_run": {"new": sum(1 for r in done if r["ok"]), "failed": sum(1 for r in done if not r["ok"])}}
    LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    return latest


def _main():
    import traceback
    log = LOGS / "trade_postmortem_last.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as fh:
        class Tee:
            def write(self, s):
                fh.write(s); fh.flush()
            def flush(self):
                fh.flush()
        sys.stdout = sys.stderr = Tee()
        try:
            days = int(sys.argv[1]) if len(sys.argv) > 1 else LOOKBACK_DAYS
            res = run(days)
            print("done", res["last_run"])
            return 0
        except Exception:
            traceback.print_exc()
            return 1


if __name__ == "__main__":
    sys.exit(_main())
