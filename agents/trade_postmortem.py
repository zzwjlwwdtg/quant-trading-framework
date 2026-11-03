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
每笔卖出先按触发标签定性: 强制调仓 (机械止损 / 现金纪律 / 再平衡) 或 信号卖出, 如实写明.
补现金卖单 (REBALANCE CASH) 不调 AI, 用规则理由归档.
所有复盘按 order_id 追加到 signals/postmortem/archive.jsonl 长期归档.
复盘只供人看, 不进交易决策; 也明确要求 AI 不得因亏损建议拉黑标的.
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


# ── 卖出性质 (2026-10-08 用户: 机械执行的就如实归为"强制调仓", 但同样要写理由并归档) ─────
def classify_sell(tag: str, decision: dict | None = None) -> str:
    t = (tag or "").upper()
    if "REBALANCE CASH" in t:
        return "强制调仓·现金纪律"
    if "STOP" in t:
        return "强制调仓·机械止损"
    if "REBALANCE" in t:
        return "强制调仓·再平衡"
    if "TAKE-PROFIT" in t:
        return "规则止盈"
    if "KICKOUT" in t:
        return "强制调仓·卫星池剔除"
    return "信号卖出"


# ── 超卖研究证据：仅供复盘，不读取或写入交易状态 ────────────────────────────────
_UNVERIFIED_OVERSOLD = re.compile(
    r"高概率反弹|大概率反弹|必然反弹|必涨|卖在底部|卖在最低|局部底部|"
    r"超卖.{0,16}(?:反弹|底部)|(?:反弹|底部).{0,16}超卖|"
    r"high[- ]probability rebound|sold at (?:the )?bottom",
    re.I,
)


def oversold_review_evidence(bundle: dict | None = None) -> dict:
    """仅接受协议和原始离线材料，重新检验，不信任外部传入的 passed 标志。"""
    if bundle is None:
        return {
            "proposal": "EVO-20261028-1", "status": "证据不足", "passed": False,
            "research_only": True, "trading_authorized": False,
            "execution_improvement_verified": False,
            "reason": "未提供开发前登记协议及全量历史规则重放材料",
        }
    from research_validation import evaluate_oversold_study
    try:
        result = evaluate_oversold_study(
            bundle["protocol"], bundle["dataset"], code_version=bundle["code_version"])
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "proposal": "EVO-20261028-1", "status": "证据不足", "passed": False,
            "research_only": True, "trading_authorized": False,
            "execution_improvement_verified": False, "reason": str(exc),
        }
    # 完整分钟路径留在独立研究结果中，避免复盘提示词膨胀。
    periods = {}
    for name, period in result.get("periods", {}).items():
        periods[name] = {
            k: period[k] for k in (
                "bounds", "eligible", "independent", "exclusion_rate",
                "oversold", "control", "primary", "status")
        }
    return {
        k: v for k, v in result.items() if k != "periods"
    } | {"periods": periods}


def label_oversold_claims(analysis: dict) -> dict:
    """逐字段标记强结论；研究均值差即便通过也不验证个别交易卖在底部。"""
    claims = []

    def visit(value, path):
        if isinstance(value, str):
            if _UNVERIFIED_OVERSOLD.search(value):
                claims.append({"path": path, "original": value,
                               "label": "未验证假设"})
                return "【未验证假设】" + value
            return value
        if isinstance(value, list):
            return [visit(v, f"{path}[{i}]") for i, v in enumerate(value)]
        if isinstance(value, dict):
            return {k: visit(v, f"{path}.{k}") for k, v in value.items()}
        return value

    result = visit(analysis, "analysis")
    result["oversold_evidence_labels"] = claims
    if claims and isinstance(result.get("hindsight"), dict):
        result["hindsight"]["verdict"] = "难判断"
    return result


# ── AI ───────────────────────────────────────────────────────────────────────
def build_prompt(sell: dict, excerpt: list[str], news: list[dict], after_log: list[str], after_closes: list[dict],
                 *, research_evidence: dict | None = None) -> str:
    return f"""你是交易复盘分析师。下面是一个自动交易系统的一笔**卖出**和当时的原始材料。
只依据这些材料作答, 不要编造材料里没有的新闻或数字; 材料不足就明确写"材料不足".

## 卖出
标的 {sell['ticker']} | 卖出 {sell['qty']} 股 @ ${sell['price']} | 下单时间 (UTC) {sell['decision_ts']}
触发标签: {sell['tag'] or '无'}
卖出性质: {classify_sell(sell.get('tag'), sell.get('decision'))}{'（规则自动执行, 不是主观判断; 理由要说明规则为什么在那一刻触发, 以及当时的行情是否支持它）' if classify_sell(sell.get('tag')).startswith('强制调仓') else ''}
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
5. 不要建议把这只标的拉黑、永久禁止交易或"以后别碰": 亏损本身不是拉黑的理由 (交易是反人性的, 亏得多恰恰容易情绪化).
6. 超卖仅为复盘分组标签，不得作为暂停止损的理由。没有同期可得买卖报价，不得把价格差称为精确执行滑点；止损线到成交价的偏离不能全部归因于窗口检查、跳空或执行。
7. 超卖研究证据: {json.dumps(research_evidence or oversold_review_evidence(), ensure_ascii=False)}
   未通过或证据不足时，任何“高概率反弹”“局部底部”“卖在底部”等结论必须明确标为“未验证假设”。
   即便研究通过，也只可引用相应样本、收益均值差和置信区间；均值差不证明反弹概率很高或个别卖出错误。
   必须区分统计反弹与含报价、费用、再入场成本的可执行卖出改进；不得据此建议放松或暂停止损。
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
    return label_oversold_claims(d)


def collect_news_structured_for(sell: dict, t: datetime, *, window_hours: float = 48.0) -> dict:
    """EVO-20261014-2: 把 ±window_hours 候选 RSS 一次性经 Claude CLI 拆成结构化信号.

    结果只用于写进 postmortem 的 news_structured 字段, 决策链路零接触
    (TECHNICAL_ONLY 不变; 这里不是把消息面放回决策, 只是让复盘能回答
     '那笔强制卖出是否至少有事件/新闻背书'). CLI 调用 ≤ 1 次/笔.
    """
    try:
        from news_analyzer import collect_news_structured
        return collect_news_structured(
            news_symbols(sell["ticker"]), t,
            window_hours=window_hours, max_items=15,
            source_label=f"postmortem/{sell.get('order_id', '')}")
    except Exception as e:
        return {"items": [], "fetched": 0, "fallback": True,
                "error": str(e)[:200]}


def review_one(sell: dict, *, ai=None, live_fetch: bool = True,
               oversold_research: dict | None = None) -> dict:
    t = datetime.fromisoformat(sell["decision_ts"].replace("Z", "+00:00"))
    rows = load_log_window(t, BEFORE_H, AFTER_H)
    excerpt = select_excerpt(rows, sell["ticker"])
    news = news_from_cache(sell["ticker"], t) + (news_from_rss(sell["ticker"], t) if live_fetch else [])
    after_log: list[str] = []   # 不再用 log 价格做事后走势 (是日线参考价, 易误读)
    after_closes = closes_after(sell["ticker"], t) if live_fetch else []
    # EVO-20261014-2: 候选新闻结构化拆解 (只入 postmortem, 不回传决策)
    news_structured = (collect_news_structured_for(sell, t)
                        if live_fetch else
                        {"items": [], "fetched": 0, "fallback": None,
                         "skipped": "live_fetch=False"})
    research_evidence = oversold_review_evidence(oversold_research)
    prompt = build_prompt(sell, excerpt, news, after_log, after_closes,
                          research_evidence=research_evidence)
    if ai is None:
        from ai_prompt import query_ai_cli
        ai = lambda p: query_ai_cli(p, timeout=300, complexity="complex",
                                    caller="trade_postmortem")
    out, status, provider, fb = ai(prompt)
    parsed = parse_ai(out)
    if parsed is None and out:      # 保留原文便于排查 (下次运行会重试)
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / f"raw_failed_{sell['order_id']}.txt").write_text(out, encoding="utf-8")
        except OSError:
            pass
    return {"order_id": sell["order_id"], "ticker": sell["ticker"], "qty": sell["qty"], "price": sell["price"],
            "ts": sell["decision_ts"], "tag": sell["tag"], "nature": classify_sell(sell["tag"], sell.get("decision")),
            "provider": provider, "status": status,
            "ok": parsed is not None, "analysis": parsed,
            "material": {"excerpt_lines": len(excerpt), "news_items": len(news), "after_closes": after_closes},
            "news_structured": news_structured,
            "oversold_research": research_evidence,
            "reviewed_at": datetime.now(timezone.utc).isoformat()}


ARCHIVE = OUT_DIR / "archive.jsonl"


def cash_sell_record(s: dict) -> dict:
    """补现金卖单: 规则机械执行, 不调 AI, 用固定理由归档."""
    why = s["tag"].strip("[]").replace("REBALANCE CASH", "").strip()
    reason = ("现金为负, 规则 B 卖 SHY/IEI 把现金补回 ≥0 (不借保证金)" if "restore" in why
              else f"规则 A: 买入前现金不足, 先卖 SHY 凑钱 ({why})")
    return {"order_id": s["order_id"], "ticker": s["ticker"], "qty": s["qty"], "price": s["price"],
            "ts": s["decision_ts"], "tag": s["tag"], "nature": "强制调仓·现金纪律", "provider": "rule",
            "ok": True, "reason": reason, "reviewed_at": datetime.now(timezone.utc).isoformat()}


def append_archive(rec: dict, path: Path | None = None) -> bool:
    """按 order_id 去重追加到长期归档 (latest.json 只保留最近 20 份)."""
    path = Path(path or ARCHIVE)
    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(str(json.loads(line).get("order_id")))
            except json.JSONDecodeError:
                continue
    if str(rec.get("order_id")) in seen:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def replay_execution_audit(
    *,
    ledger_path: Path | None = None,
    fees_path: Path | None = None,
    trade_log_path: Path | None = None,
    archive_path: Path | None = None,
    reviewed_path: Path | None = None,
    expected_sells: int = 30,
) -> dict:
    """EVO-20261021-2：离线只读回放，不调用 AI、行情、订单或任何写盘函数。

    最近 expected_sells 笔用于覆盖检验；全部历史卖出用于跟踪止损统计。
    独立传入所有路径即可回放隔离材料，缺失材料明确列出。
    """
    import fill_ledger
    from execution_analytics import (
        CHANGE_DESCRIPTION, evidence_time, sell_cost_breakdown,
        trailing_stop_measurement,
    )

    errors = []

    def read_rows(path):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            errors.append(f"missing_file:{path}")
            return []
        result = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError
                result.append(row)
            except (ValueError, TypeError):
                errors.append(f"invalid_json:{path}:{number}")
        return result

    ledger = Path(ledger_path) if ledger_path is not None else fill_ledger.EXEC_LEDGER_PATH
    fee_file = Path(fees_path) if fees_path is not None else fill_ledger.FEES_PATH
    # 先验证原始文件，避免读取器跳过坏行后仍误报通过。
    read_rows(ledger)
    read_rows(fee_file)
    if ledger_path is None:
        read_rows(fill_ledger.BROKER_HISTORY_PATH)
    trades = read_rows(trade_log_path or SIGNALS / "trade_log.jsonl")
    archives = read_rows(archive_path or ARCHIVE)
    reviewed_file = Path(reviewed_path or REVIEWED)
    try:
        reviewed = json.loads(reviewed_file.read_text(encoding="utf-8"))
        if not isinstance(reviewed, dict):
            raise ValueError
    except (OSError, ValueError, TypeError):
        reviewed = {}
        errors.append(f"missing_or_invalid_reviewed:{reviewed_file}")
    by_trade, by_archive = {}, {}
    for row in trades:
        if row.get("order_id"):
            by_trade.setdefault(str(row["order_id"]), []).append(row)
    for row in archives:
        if row.get("order_id"):
            by_archive.setdefault(str(row["order_id"]), []).append(row)

    evidence = fill_ledger.sell_execution_evidence(ledger_path, fees_path)
    records, groups, trailing = [], {}, []
    unknown_reason = 0
    for original in evidence:
        item = dict(original)
        oid = item["order_id"]
        contexts = by_trade.get(oid, [])
        if len(contexts) > 1:
            item["anomalies"] = list(item["anomalies"]) + ["multiple_trade_contexts"]
        if contexts:
            trade = contexts[-1]
            # trade_log.ts 是决策/日志时间，不能补成真实触发时间。
            for key in ("tag", "reason", "trigger_ts", "confirmed_ts", "stop_line",
                        "trigger_quote", "submit_quote", "window_start_ts",
                        "previous_window_end_ts", "between_window_quotes",
                        "session", "leveraged", "oversold"):
                value = trade.get(key)
                if value is None:
                    continue
                if key in item and item[key] != value:
                    item["anomalies"] = list(item["anomalies"]) + [f"conflicting_trade_context:{key}"]
                else:
                    item[key] = value
        reason = item.get("tag") or item.get("reason")
        if not reason:
            unknown_reason += 1
        reviews = by_archive.get(oid, [])
        successful = [r for r in reviews if r.get("ok") is True]
        if successful:
            review_status, missing_reason = "reviewed", None
        elif reviews:
            review_status, missing_reason = "failed", "归档复盘未成功"
        elif oid in reviewed:
            review_status, missing_reason = "missing", "已复盘索引存在，但缺少成功归档"
        else:
            review_status, missing_reason = "missing", "无复盘归档及成功索引"
        cost = sell_cost_breakdown(item)
        record = {
            "order_id": oid, "ticker": item["ticker"],
            "review_status": review_status, "missing_reason": missing_reason,
            "reason": reason or "unknown",
            "session": item.get("session") or "unknown",
            "leveraged": item.get("leveraged") if isinstance(item.get("leveraged"), bool) else "unknown",
            "oversold": item.get("oversold") if isinstance(item.get("oversold"), bool) else "unknown",
            "cost": cost,
            "sort_ts": item.get("last_fill_ts") or item.get("submitted_ts"),
        }
        records.append(record)
        key = json.dumps([record[k] for k in ("reason", "session", "leveraged", "oversold")],
                         ensure_ascii=False)
        group = groups.setdefault(key, {
            "reason": record["reason"], "session": record["session"],
            "leveraged": record["leveraged"], "oversold": record["oversold"],
            "orders": 0, "complete_cost_orders": 0, "known_total_cost_usd": 0.0,
        })
        group["orders"] += 1
        if cost["status"] == "complete":
            group["complete_cost_orders"] += 1
            group["known_total_cost_usd"] += cost["total_cost_usd"]
        if "TRAILING_STOP" in str(reason or "").upper():
            measurement = trailing_stop_measurement(item)
            if cost["anomalies"]:
                measurement["window_extra_loss_usd"] = None
                measurement["status"] = "invalid"
            trailing.append(measurement)

    # 时区统一后再排序；无法排序的订单也进入覆盖清单，并阻止验证通过。
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    records.sort(key=lambda r: evidence_time(r["sort_ts"]) or epoch, reverse=True)
    selected = records[:max(0, expected_sells)]
    missing_sort_time = [r["order_id"] for r in records if evidence_time(r["sort_ts"]) is None]
    invalid = [r["order_id"] for r in records if r["cost"]["anomalies"]]
    coverage_ok = (
        expected_sells > 0 and len(selected) == expected_sells
        and all(r["review_status"] == "reviewed" or r["missing_reason"] for r in selected)
    )
    return {
        "proposal": "EVO-20261021-2",
        "change": {"description": CHANGE_DESCRIPTION},
        "read_only": True,
        "validation_passed": coverage_ok and not errors and not invalid and not missing_sort_time,
        "coverage": {
            "expected": expected_sells, "found": len(selected), "complete": coverage_ok,
            "reviewed": sum(r["review_status"] == "reviewed" for r in selected),
            "records": selected,
        },
        "all_sell_count": len(records),
        "groups": list(groups.values()),
        "all_historical_trailing_stops": trailing,
        "trailing_stop_summary": {
            "orders": len(trailing),
            "unknown_sell_reason_orders": unknown_reason,
            "historical_classification_complete": unknown_reason == 0 and not errors,
            "window_extra_loss_usd": None,
            "known_window_extra_loss_orders": 0,
            "unknown_window_extra_loss_orders": len(trailing),
            "reason": "不能将止损线至成交价偏离直接认定为窗口检查造成的额外损失",
        },
        "invalid_orders": invalid,
        "unknown_order_times": missing_sort_time,
        "source_errors": errors,
        "publication_authorized": False,
    }


def run(days: int = LOOKBACK_DAYS, *, ai=None, live_fetch: bool = True,
        oversold_research: dict | None = None) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reviewed = json.loads(REVIEWED.read_text(encoding="utf-8")) if REVIEWED.exists() else {}
    sells = collect_sells(days)
    done, mechanical = [], []
    for s in sells:
        if s["mechanical_cash"]:
            rec = cash_sell_record(s)
            mechanical.append(rec)
            append_archive(rec, ARCHIVE)
            continue
        if str(s["order_id"]) in reviewed:
            continue
        r = review_one(s, ai=ai, live_fetch=live_fetch,
                       oversold_research=oversold_research)
        print(f"[postmortem] {s['ticker']} {s['order_id']} ok={r['ok']} provider={r['provider']} status={str(r['status'])[:80]}")
        if r["ok"]:
            day = r["ts"][:10]
            (OUT_DIR / f"{day}_{r['ticker'].replace('US.', '')}_{r['order_id']}.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            reviewed[str(s["order_id"])] = r["reviewed_at"]
            append_archive(r, ARCHIVE)
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
