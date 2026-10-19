"""生成各时段的汇报 (markdown + 结构化 dict). 只读, 不下单.

时段 (日本时间):
  morning 09:00  隔夜/上个交易日以来的研报精选 + 昨夜已公布的重大数据 + 今天将公布的重大数据
  close   16:30  今天新出的研报精选 + 今天已公布的数据 (公布值 vs 预期/前值) + 今晚预告
  evening 21:00  今晚到明早将公布的中美日重大数据 (时间换算成日本时间)
  now            按需: 合并以上内容的即时版 (给机器人现场调用)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from . import sources  # noqa: E402

JST = timezone(timedelta(hours=9))
BJT = timezone(timedelta(hours=8))
OUT_DIR = Path(__file__).resolve().parents[1] / "signals" / "cn_brief"
SLOTS = ("morning", "close", "evening", "now")
SLOT_TITLES = {"morning": "A股盘前研报 & 今日数据", "close": "A股收盘后研报 & 数据回顾",
               "evening": "今晚重大经济数据提醒", "now": "A股研报 & 经济数据 (即时)"}

# ── 研报精选 ──────────────────────────────────────────────────────────────
RATING_RANK = {"买入": 5, "强烈推荐": 5, "强推": 5, "增持": 4, "推荐": 4, "优于大市": 4, "跑赢行业": 4,
               "谨慎推荐": 4, "持有": 3, "中性": 3, "同步大市": 3, "观望": 3, "减持": 2, "弱于大市": 2,
               "卖出": 1}


def rating_rank(name: str | None) -> int | None:
    if not name:
        return None
    n = re.sub(r"\(.*?\)|（.*?）", "", str(name)).strip()
    return RATING_RANK.get(n)


def rating_change(row: dict) -> str:
    """不依赖东方财富的 ratingChange 代码, 直接比较上次/本次评级."""
    cur, last = rating_rank(row.get("emRatingName")), rating_rank(row.get("lastEmRatingName"))
    if cur is None:
        return "无评级"
    if last is None:
        return "首次"
    return "调高" if cur > last else ("调低" if cur < last else "维持")


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def summarize_stock_reports(rows: list[dict], top: int = 12) -> dict:
    by_stock = defaultdict(list)
    for r in rows:
        if r.get("stockCode"):
            by_stock[(r.get("stockCode"), r.get("stockName"))].append(r)
    hot = sorted(by_stock.items(), key=lambda kv: (len({x.get("orgSName") for x in kv[1]}), len(kv[1])), reverse=True)
    hot = [{"code": c, "name": n, "n_reports": len(v), "orgs": sorted({x.get("orgSName") or "" for x in v}),
            "ratings": Counter(x.get("emRatingName") or "无" for x in v).most_common(),
            "titles": [x.get("title") for x in v][:3], "link": sources.report_link(v[0]),
            "industry": v[0].get("indvInduName") or ""} for (c, n), v in hot if len(v) >= 2][:top]

    def item(r):
        aim = _num(r.get("indvAimPriceT"))
        return {"code": r.get("stockCode"), "name": r.get("stockName"), "org": r.get("orgSName"),
                "rating": r.get("emRatingName") or "", "last_rating": r.get("lastEmRatingName") or "",
                "change": rating_change(r), "title": r.get("title"), "industry": r.get("indvInduName") or "",
                "eps_this_year": _num(r.get("predictThisYearEps")), "pe_this_year": _num(r.get("predictThisYearPe")),
                "target_price": aim, "date": str(r.get("publishDate") or "")[:10], "link": sources.report_link(r)}
    items = [item(r) for r in rows]
    upgrades = [x for x in items if x["change"] == "调高"]
    downgrades = [x for x in items if x["change"] == "调低"]
    first = [x for x in items if x["change"] == "首次" and rating_rank(x["rating"]) and rating_rank(x["rating"]) >= 4]
    inds = Counter(x["industry"] for x in items if x["industry"]).most_common(10)
    return {"n": len(items), "n_stocks": len(by_stock), "hot": hot, "upgrades": upgrades[:top],
            "downgrades": downgrades[:top], "first_coverage": first[:top], "industries": inds,
            "rating_mix": Counter(x["rating"] or "无评级" for x in items).most_common()}


def summarize_industry_reports(rows: list[dict], top: int = 10) -> dict:
    by_ind = defaultdict(list)
    for r in rows:
        by_ind[r.get("industryName") or "其他"].append(r)
    ranked = sorted(by_ind.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    return {"n": len(rows), "industries": [{"industry": k, "n": len(v),
                                            "ratings": Counter(x.get("emRatingName") or "无" for x in v).most_common(),
                                            "titles": [f"{x.get('orgSName')}: {x.get('title')}" for x in v][:3]}
                                           for k, v in ranked]}


def summarize_macro_reports(rows: list[dict], top: int = 8) -> list[str]:
    return [f"{r.get('orgSName')}: {r.get('title')}" for r in rows[:top]]


# ── 经济数据 ──────────────────────────────────────────────────────────────
REGIONS = ("中国", "美国", "日本")
KEY_PATTERNS = {
    "中国": r"CPI|PPI|PMI|GDP|社会融资|社融|M2|M1|新增人民币贷款|LPR|贷款市场报价利率|工业增加值|社会消费品零售|社零|"
            r"固定资产投资|出口|进口|贸易帐|城镇调查失业率|外汇储备|存款准备金",
    "美国": r"非农|失业率|CPI|PCE|核心|GDP|零售销售|ISM|联邦基金利率|利率决议|FOMC|美联储|初请失业金|"
            r"PPI|耐用品|新屋开工|成屋销售|消费者信心|密歇根|JOLTS|ADP",
    "日本": r"央行|日银|利率决议|CPI|GDP|短观|贸易帐|失业率|工业产出|PMI",
}
NOISE = r"库存变动|库存-每日|持仓变动|竞拍|钻井|DOE|API|EIA|每日|能源|进口量|出口量"
# 头部数据: 不看星级一律提醒 (其余关键词命中还要求 ≥2 星; 或任何 ≥3 星)
TOP_TIER = r"CPI|非农|GDP|PCE|利率决议|联邦基金利率|FOMC|美联储|LPR|贷款市场报价利率|社会融资|社融|央行|日银"


def is_important(ev: dict) -> bool:
    region = ev.get("region")
    if region not in REGIONS:
        return False
    title = str(ev.get("title") or "")
    if re.search(NOISE, title):
        return False
    star = int(_num(ev.get("star")) or 0)
    if star >= 3 or re.search(TOP_TIER, title):
        return True
    return star >= 2 and bool(re.search(KEY_PATTERNS[region], title))


def _val(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return None if s in ("", "None", "未公布", "--", "-") else s


def fmt_event(ev: dict, with_result: bool = True) -> str:
    t = ev.get("time_jst")
    ts = t.strftime("%m/%d %H:%M") if isinstance(t, datetime) else "时间未定"
    star = "★" * int(_num(ev.get("star")) or 0)
    former, pub, exp = _val(ev.get("formerVal")), _val(ev.get("pubVal")), _val(ev.get("expectVal") or ev.get("indicateVal"))
    parts = []
    if with_result and pub:
        parts.append(f"公布 **{pub}**")
    if exp:
        parts.append(f"预期 {exp}")
    if former:
        parts.append(f"前值 {former}")
    tail = f"（{'，'.join(parts)}）" if parts else ""
    note = ""
    if with_result and pub and exp and _num(pub) is not None and _num(exp) is not None:
        diff = _num(pub) - _num(exp)
        note = " ↑高于预期" if diff > 0 else (" ↓低于预期" if diff < 0 else " =符合预期")
    return f"- {ts} JST {ev.get('region')} {ev.get('title')} {star}{tail}{note}"


def event_id(ev: dict) -> str:
    return f"{ev.get('date')}|{ev.get('time')}|{ev.get('region')}|{ev.get('title')}"


def important_events(start_bjt: date, days: int) -> tuple[list[dict], list[str]]:
    rows = sources.calendar_range(start_bjt, days)
    errs = [r["_error"] for r in rows if "_error" in r]
    evs = [r for r in rows if "_error" not in r and is_important(r)]
    for e in evs:
        e["important"] = True
        e["id"] = event_id(e)
    evs.sort(key=lambda e: e.get("time_jst") or datetime.max.replace(tzinfo=JST))
    return evs, errs


def _empty(errs: list[str], ok_text: str) -> str:
    """没有事件时: 如果是因为获取失败, 如实写失败, 不能写成"没有数据"."""
    return f"_经济日历获取失败：{errs[0][:160]}_" if errs else ok_text


# ── AI 摘要 (可选; Claude 优先, 无额度时 Codex, 都不可用就如实写明) ──────────────
def ai_summary(stock: dict, ind: dict, macro: list[str]) -> tuple[str | None, str]:
    if os.environ.get("CN_BRIEF_NO_AI") == "1":
        return None, "disabled"
    lines = []
    for h in stock.get("hot", [])[:10]:
        lines.append(f"[多家覆盖] {h['name']}({h['code']}) {h['industry']} {len(h['orgs'])}家: " + " / ".join(h["titles"][:2]))
    for tag in ("upgrades", "first_coverage", "downgrades"):
        for x in stock.get(tag, [])[:8]:
            lines.append(f"[{x['change']}] {x['name']}({x['code']}) {x['org']} {x['last_rating']}→{x['rating']}: {x['title']}")
    for i in ind.get("industries", [])[:8]:
        lines.append(f"[行业 {i['industry']} {i['n']}篇] " + " / ".join(i["titles"][:2]))
    for m in macro[:6]:
        lines.append(f"[宏观] {m}")
    if not lines:
        return None, "no_input"
    prompt = ("你是A股卖方研报的整理助手。下面是今天东方财富研报中心的研报标题和评级变动(只有标题, 没有正文)。\n"
              "请用中文写 4-6 条要点: 卖方今天集中关注的方向/行业、评级调高或首次覆盖的重点、需要注意的下调或分歧。\n"
              "严格只依据下面给出的信息, 不要编造数字、股价或正文内容; 提到机构观点时沿用原标题措辞, 不要改写专有名词;\n"
              "不构成投资建议。每条一行, 以 '- ' 开头, 条目之间不要空行。\n\n"
              + "\n".join(lines[:60]))
    try:
        from ai_prompt import query_ai_cli
        complexity = os.environ.get("CN_BRIEF_AI_COMPLEXITY", "simple")   # 标题整理属于简单抽取 → Sonnet
        out, status, provider, fb = query_ai_cli(prompt, timeout=240, complexity=complexity)
        if out:
            return out.strip(), f"ok ({provider}{', 主通道失败后由备用接手' if fb else ''})"
        return None, f"AI 不可用 (Claude 与 Codex 均失败): {str(status)[:160]}"
    except Exception as e:      # noqa: BLE001
        return None, f"AI 调用失败: {e}"


# ── 组装 ─────────────────────────────────────────────────────────────────
def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _report_window(slot: str, today_bjt: date) -> tuple[date, date]:
    if slot == "close":
        return today_bjt, today_bjt
    return _prev_weekday(today_bjt), today_bjt       # morning / now: 上个交易日 (含周末) 到今天


def build_brief(slot: str = "now", now: datetime | None = None, use_ai: bool = True,
                write_files: bool = True) -> dict:
    if slot not in SLOTS:
        raise ValueError(f"slot 必须是 {SLOTS}")
    now = (now or datetime.now(JST)).astimezone(JST)
    today_bjt = now.astimezone(BJT).date()
    out = {"slot": slot, "title": SLOT_TITLES[slot], "generated_at": now.isoformat(timespec="seconds"),
           "errors": [], "reports": None, "events": {}}
    md = [f"## {SLOT_TITLES[slot]}  ·  {now.strftime('%Y-%m-%d %H:%M')} JST"]

    # 研报
    if slot != "evening":
        b, e = _report_window(slot, today_bjt)
        try:
            srows, irows, mrows = sources.stock_reports(b, e), sources.industry_reports(b, e), sources.macro_reports(b, e)
            st, ind, mac = summarize_stock_reports(srows), summarize_industry_reports(irows), summarize_macro_reports(mrows)
            out["reports"] = {"window": [b.isoformat(), e.isoformat()], "stock": st, "industry": ind, "macro": mac}
            md.append(f"\n### 📑 研报 ({b.isoformat()}{'' if b == e else ' ~ ' + e.isoformat()})："
                      f"个股 {st['n']} 篇 / {st['n_stocks']} 只，行业 {ind['n']} 篇，宏观 {len(mac)} 篇")
            if use_ai:
                summ, ai_status = ai_summary(st, ind, mac)
                out["ai"] = {"summary": summ, "status": ai_status}
                if summ:
                    md.append("**要点（AI 依据标题整理，仅供参考）**\n" + summ)
                elif ai_status not in ("disabled", "no_input"):
                    md.append(f"_要点摘要：{ai_status}_")
            if st["upgrades"]:
                md.append("**评级调高**")
                md += [f"- {x['name']}({x['code']}) {x['org']} {x['last_rating']}→**{x['rating']}**：{x['title']}" for x in st["upgrades"][:8]]
            if st["first_coverage"]:
                md.append("**首次覆盖（买入/增持）**")
                md += [f"- {x['name']}({x['code']}) {x['org']} {x['rating']}：{x['title']}" for x in st["first_coverage"][:8]]
            if st["hot"]:
                md.append("**多家机构同时覆盖**")
                md += [f"- {h['name']}({h['code']}) {h['industry']}：{len(h['orgs'])} 家（{'、'.join(h['orgs'][:4])}）— {h['titles'][0]}" for h in st["hot"][:8]]
            if st["downgrades"]:
                md.append("**评级调低 ⚠**")
                md += [f"- {x['name']}({x['code']}) {x['org']} {x['last_rating']}→**{x['rating']}**：{x['title']}" for x in st["downgrades"][:8]]
            if ind["industries"]:
                md.append("**行业研报热度**：" + "，".join(f"{i['industry']} {i['n']}" for i in ind["industries"][:8]))
            if mac:
                md.append("**宏观/策略**")
                md += [f"- {m}" for m in mac[:5]]
            if st["n"] == 0 and ind["n"] == 0:
                md.append("_暂无新研报（非交易日或尚未发布）_")
        except Exception as ex:       # noqa: BLE001
            out["errors"].append(f"研报获取失败: {ex}")
            md.append(f"\n### 📑 研报\n_获取失败：{ex}_")

    # 经济数据
    try:
        if slot == "evening":
            evs, errs = important_events(today_bjt, 2)
            upcoming = [e for e in evs if e.get("time_jst") and now <= e["time_jst"] <= now + timedelta(hours=14)]
            out["events"]["upcoming"] = upcoming
            md.append("\n### 📅 今晚到明早的重大数据（日本时间）")
            md += [fmt_event(e, with_result=False) for e in upcoming] or [_empty(errs, "_没有中美日的重大数据_")]
        else:
            evs, errs = important_events(today_bjt - timedelta(days=1), 3)
            released = [e for e in evs if e.get("time_jst") and now - timedelta(hours=24) <= e["time_jst"] <= now]
            upcoming = [e for e in evs if e.get("time_jst") and now < e["time_jst"] <= now + timedelta(hours=24)]
            out["events"] = {"released": released, "upcoming": upcoming}
            md.append("\n### 📊 过去 24 小时已公布（中美日）")
            md += [fmt_event(e) for e in released] or [_empty(errs, "_无_")]
            md.append("\n### 📅 未来 24 小时将公布（日本时间）")
            md += [fmt_event(e, with_result=False) for e in upcoming] or [_empty(errs, "_无_")]
        if errs and (out["events"].get("released") or out["events"].get("upcoming")):
            md.append(f"_部分日期的日历获取失败（{len(errs)} 天），以上可能不完整_")
        out["errors"] += errs
    except Exception as ex:           # noqa: BLE001
        out["errors"].append(f"经济日历获取失败: {ex}")
        md.append(f"\n### 📅 经济数据\n_获取失败：{ex}_")

    md.append("\n<sub>数据：东方财富研报中心、百度股市通财经日历；仅供参考，不构成投资建议。</sub>")
    out["markdown"] = "\n".join(md)
    if write_files:
        save(out)
    return out


def _jsonable(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(type(o))


def save(brief: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slot = brief["slot"]
    (OUT_DIR / f"latest_{slot}.md").write_text(brief["markdown"], encoding="utf-8")
    (OUT_DIR / f"latest_{slot}.json").write_text(json.dumps(brief, ensure_ascii=False, indent=1, default=_jsonable),
                                                encoding="utf-8")
    arch = OUT_DIR / "archive"
    arch.mkdir(exist_ok=True)
    stamp = brief["generated_at"][:16].replace(":", "").replace("T", "_")
    (arch / f"{stamp}_{slot}.md").write_text(brief["markdown"], encoding="utf-8")
    for old in sorted(arch.glob("*.md"))[:-400]:      # 只留最近 400 份
        try:
            old.unlink()
        except OSError:
            pass
    return OUT_DIR / f"latest_{slot}.md"
