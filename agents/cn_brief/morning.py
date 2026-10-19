"""9:00 盘前精简版: 一句话定调 + 今日关键时刻 + 外围隔夜 + 风险名单 + 特殊日 (2026-10-19 用户要求).

每条附带的 "历史上…" 统计都来自预注册回测的结论文件; 没通过检验的不附统计, 并注明.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import brief as B
from . import overseas
from . import sources

HERE = Path(__file__).resolve().parent
VERDICTS = HERE.parents[0] / "signals" / "backtest_verdicts"
WATCHLIST = HERE / "watchlist.txt"
WEEKDAY = "一二三四五六日"

ECON_GROUPS = {"cn_growth": (re.compile(r"GDP年率-单季度|规模以上工业增加值年率-单月|社会消费品零售总额年率|制造业PMI|出口年率"), "中国"),
               "cn_inflation": (re.compile(r"CPI年率|PPI年率"), "中国"),
               "us": (re.compile(r"CPI年率|非农就业人数|失业率"), "美国")}
ECON_EXCLUDE = re.compile(r"年初至今|月率|季率|核心|非制造业|服务业|综合|财新服务|进口|总量")


def _verdict(name: str) -> dict:
    try:
        return json.loads((VERDICTS / f"{name}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_watchlist() -> list[str]:
    try:
        return [m.group(0) for line in WATCHLIST.read_text(encoding="utf-8").splitlines()
                if (m := re.match(r"\s*(\d{6})", line.split("#")[0]))]
    except OSError:
        return []


def econ_group(ev: dict) -> str | None:
    t = str(ev.get("title") or "")
    if ECON_EXCLUDE.search(t):
        return None
    return next((g for g, (p, reg) in ECON_GROUPS.items() if p.search(t) and ev.get("region") == reg), None)


def econ_note(group: str | None, verdict: dict) -> str | None:
    if not group:
        return None
    try:
        informative = json.loads(verdict.get("conclusion") or "{}").get(group)
    except Exception:
        informative = None
    m = (verdict.get("metrics") or {}).get(group) or {}
    if informative and m.get("beat", {}).get("n"):
        return (f"历史：好于预期当天沪深300ETF平均 {m['beat']['mean_pct']:+.2f}%，"
                f"差于预期 {m['miss']['mean_pct']:+.2f}%（2024–26，t={m.get('diff_t')}）")
    if informative is False:
        return "历史：好坏对A股当天无显著影响"
    return None


def third_friday(d: date) -> date:
    first = d.replace(day=1)
    fri = first + timedelta(days=(4 - first.weekday()) % 7)
    return fri + timedelta(days=14)


def special_days(today: date) -> list[str]:
    out = []
    tf = third_friday(today)
    if today == tf:
        out.append("今天是股指期货/期权交割日（IF/IH/IC/IM，每月第三个周五），尾盘波动可能放大")
    elif 0 < (tf - today).days <= 3:
        out.append(f"{tf.month}/{tf.day}（周五）股指期货交割日")
    return out


def risk_list(today_bjt: date, watch: list[str], verdict: dict, top: int = 5) -> dict:
    rows = sources.stock_reports(today_bjt - timedelta(days=7), today_bjt)
    down, by_code = [], defaultdict(list)
    for r in rows:
        c = str(r.get("stockCode") or "")
        if not c:
            continue
        by_code[c].append(r)
        if B.rating_change(r) == "调低":
            down.append(r)
    crowded = []
    for c, lst in by_code.items():
        orgs = {x.get("orgSName") for x in lst}
        if len(orgs) >= 3:
            crowded.append((c, lst[0].get("stockName"), len(orgs), lst[0].get("indvInduName") or ""))
    if watch:
        down = [r for r in down if r.get("stockCode") in watch]
        crowded = [x for x in crowded if x[0] in watch]
    crowded.sort(key=lambda x: -x[2])
    summ = ((verdict.get("metrics") or {}).get("summary") or {})

    def st(cat):
        o = ((summ.get(cat) or {}).get("oos") or {}).get("h20") or {}
        return f"样本外20日平均 {o['mean_pct']:+.2f}%，跑赢比例 {o['win_rate'] * 100:.0f}%" if o.get("n") else None
    return {"watchlist": watch, "downgrades": [{"code": r.get("stockCode"), "name": r.get("stockName"), "org": r.get("orgSName"),
                                                "from": r.get("lastEmRatingName"), "to": r.get("emRatingName"),
                                                "date": str(r.get("publishDate"))[:10]} for r in down[:top]],
            "crowded": [{"code": c, "name": n, "n_orgs": k, "industry": i} for c, n, k, i in crowded[:top]],
            "down_note": st("DOWN"), "crowded_note": st("MULTI3")}


def build(now: datetime) -> tuple[dict, list[str], list[str]]:
    today_bjt = now.astimezone(B.BJT).date()
    data, md, errors = {}, [], []
    econ_v = _verdict("ashare_econ_surprise")
    rep_v = _verdict("ashare_report_signals")
    on_v = overseas.load_verdict()
    head = []

    # 今日关键时刻 (今天日本时间 0:00 ~ 明天 6:00, 只列尚未公布的)
    try:
        evs, errs = B.important_events(today_bjt, 2)
        errors += errs
        end = now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)
        key = [e for e in evs if e.get("time_jst") and now <= e["time_jst"] <= end]
        data["key_events"] = key
        lines = []
        for e in key:
            lines.append(B.fmt_event(e, with_result=False))
            note = econ_note(econ_group(e), econ_v)
            if note:
                lines.append(f"  └ {note}")
        hi = [e for e in key if econ_group(e) == "cn_growth" or re.search(B.TOP_TIER, str(e.get("title")))]
        if hi:
            times = sorted({e["time_jst"].strftime("%H:%M") for e in hi})
            head.append(f"{'、'.join(times)} 有{('中国' if all(e['region'] == '中国' for e in hi) else '')}重大数据")
        elif not errs:
            head.append("今天没有中美日重大数据（安静日）")
        md.append("**今日关键时刻（日本时间）**")
        md += lines or ([B._empty(errs, "_无_")])
    except Exception as ex:     # noqa: BLE001
        errors.append(f"经济日历: {ex}")
        md += ["**今日关键时刻**", f"_经济日历获取失败：{ex}_"]

    # 外围隔夜
    try:
        on = overseas.overnight(today_bjt)
        errors += on["errors"]
        data["overnight"] = on
        items = on["items"]
        if items:
            md.append(f"\n**外围隔夜**（{on['last_a_close']} A股收盘以来）")
            md.append(" | ".join(overseas.fmt_item(it) for it in items))
            imp = overseas.implied_open(items, on_v)
            data["implied_open"] = imp
            if imp:
                rng = (f"{imp['low'] * 100:+.2f}%" if abs(imp["high"] - imp["low"]) < 0.0005
                       else f"{imp['low'] * 100:+.2f}% ~ {imp['high'] * 100:+.2f}%")
                md.append(f"按历史关系，沪深300ETF 开盘约 **{rng}**（"
                          + "；".join(f"{p['driver']} → {p['implied_gap'] * 100:+.2f}%" for p in imp["parts"])
                          + f"；样本外方向命中 {imp['hit_range'][0] * 100:.0f}–{imp['hit_range'][1] * 100:.0f}%）")
                md.append("_外围只影响开盘：回测显示对开盘后的日内走势没有预测力，高开/低开后延续还是回落也没有稳定规律_")
                mid = (imp["low"] + imp["high"]) / 2
                lo, hi = sorted((abs(imp["low"]) * 100, abs(imp["high"]) * 100))
                size = f"{lo:.1f}%" if f"{lo:.1f}" == f"{hi:.1f}" else f"{lo:.1f}–{hi:.1f}%"
                head.append(f"外围指向{'高开' if mid > 0 else '低开'} {size}"
                            if (imp["low"] > 0) == (imp["high"] > 0) else "外围方向不一致")
            elif on_v:
                md.append("_回测未证明这些指标能预测A股开盘，仅供参考_")
            else:
                md.append("_外围对A股开盘的回测尚未完成，仅供参考_")
            fade = overseas.fade_note(on_v)
            if fade:
                md.append(f"_{fade}_")
            spx = next((it for it in items if it["ticker"] == "^GSPC"), None)
            if spx and not imp:
                head.append(f"标普 {spx['change'] * 100:+.1f}%")
        else:
            md += ["\n**外围隔夜**", "_获取失败_" if on["errors"] else "_无数据_"]
    except Exception as ex:     # noqa: BLE001
        errors.append(f"外围: {ex}")
        md += ["\n**外围隔夜**", f"_获取失败：{ex}_"]

    # 风险名单
    try:
        watch = load_watchlist()
        rl = risk_list(today_bjt, watch, rep_v)
        data["risk"] = rl
        md.append(f"\n**风险名单**（近 7 天{'，自选股' if watch else '，全市场前 5'}；只做提醒，不是卖出信号）")
        if rl["downgrades"]:
            md.append("评级调低" + (f"（{rl['down_note']}）" if rl["down_note"] else "") + "：" +
                      "；".join(f"{x['name']}({x['code']}) {x['org']} {x['from']}→{x['to']}" for x in rl["downgrades"]))
        if rl["crowded"]:
            md.append("一周内 ≥3 家扎堆覆盖" + (f"（{rl['crowded_note']}）" if rl["crowded_note"] else "") + "：" +
                      "；".join(f"{x['name']}({x['code']}) {x['n_orgs']}家" for x in rl["crowded"]))
        if not rl["downgrades"] and not rl["crowded"]:
            md.append("_无_")
        n_risk = len(rl["downgrades"]) + len(rl["crowded"])
        if watch and n_risk:
            head.append(f"自选股风险 {n_risk} 只")
        if not watch:
            md.append("_在 agents/cn_brief/watchlist.txt 填自选股代码后，这里只显示你的股票_")
    except Exception as ex:     # noqa: BLE001
        errors.append(f"风险名单: {ex}")
        md += ["\n**风险名单**", f"_获取失败：{ex}_"]

    sp = special_days(today_bjt)
    data["special_days"] = sp
    if sp:
        md.append("\n**特殊日**")
        md += [f"- {s}" for s in sp]
        if "今天是" in sp[0]:
            head.append("期指交割日")

    title = f"## 盘前定调 · {now.month}/{now.day}（周{WEEKDAY[now.weekday()]}）{now.strftime('%H:%M')} JST"
    one = "；".join(head) if head else "暂无要点"
    md = [title, f"**一句话**：{one}", ""] + md
    md.append("\n<sub>研报详情见 16:30 收盘汇报或即时调用（run --slot now）。数据：百度财经日历、Yahoo、东方财富；仅供参考。</sub>")
    data["headline"] = one
    return data, md, errors
