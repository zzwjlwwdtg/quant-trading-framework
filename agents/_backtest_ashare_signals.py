"""A股: cn_brief 提供的信息有没有用? 预注册事件研究 (2026-10-19, 只读).

用法: python _backtest_ashare_signals.py reports | econ

[reports] 研报信号 → 个股超额收益
  数据: 东方财富研报中心 个股研报 (2023-01 ~ 2026-09); 行情 Yahoo 日线 (后复权 auto_adjust); 基准 510300.SS (沪深300ETF)
  事件 (按 股票×发布日 去重): UP 评级调高 / FIRST 首次覆盖且买入或增持 / MULTI3 6 天内 ≥3 家机构 (达到当天触发)
        / MAINT_BUY 维持买入 / DOWN 评级调低 / ALL 所有有研报的股票日 (对照)
  入场: 发布日之后第一个交易日开盘 (发布时刻未知, 不用发布日开盘, 避免偷看)
  出场: 入场后第 h 个交易日收盘, h ∈ {1, 5, 20}; 超额 = 个股收益 − 同期基准收益
  可成交: 入场日开盘 ≥ 前收 × (1 + 涨停幅度 − 0.2%) 视为买不进 (主板 10%, 300/688 20%, 北交所 30%), 另列
  预注册 "有用" 标准: 训练期 (2023-2024) 与样本外 (2025-01 ~ 2026-09) 的 20 日平均超额 同号、各期 n ≥ 100、
                     按入场日聚类的 |t_day| ≥ 2 (同一天的事件共享大盘波动, 不能当独立样本)
[econ] 经济数据意外 → 沪深300ETF 当期反应
  数据: 百度财经日历 2024-01 ~ 2026-09, 公布值 vs 预期 (indicateVal)
  反应日: 公布时刻 (北京时间) 落在交易日 9:30-15:00 之前或之中 → 当天; 否则 → 下一个交易日; 收益 = 当天收盘 / 前一交易日收盘 − 1
  分组: 中国增长 (GDP/工业/社零/PMI/出口), 中国通胀 (CPI/PPI), 美国 (CPI/非农/失业率)
  预注册: "增长数据好于预期日" 与 "差于预期日" 的平均反应之差 |t| ≥ 2 才算有信息量 (样本小, 预期大概率不显著)
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
OUT = HERE.parent / "development" / "2026-10-19" / "ashare_backtest"
CACHE = HERE / "signals" / "cn_brief" / "bt_cache"
TRAIN = ("2023-01-01", "2024-12-31")
OOS = ("2025-01-01", "2026-09-30")
HORIZONS = (1, 5, 20)
BENCH = "510300.SS"
PREREG_REPORTS = {"train": TRAIN, "oos": OOS, "horizons": HORIZONS, "bench": BENCH,
                  "entry": "first trading day after publishDate, open", "exit": "close of h-th trading day",
                  "useful_rule": "20d mean abnormal: same sign train & oos, |t_day|>=2 both, n>=100 both "
                                 "(t_day = t-stat of per-entry-date mean abnormal return, clustered by date)",
                  "limit_up_rule": "open >= prev_close*(1+limit-0.002); main 10%, 300/688 20%, BJ 30%",
                  "multi3": ">=3 distinct orgs within 6 calendar days, first day reaching 3"}
PREREG_ECON = {"period": ["2024-01-01", "2026-09-30"], "bench": BENCH,
               "groups": {"cn_growth": "GDP年率-单季度|规模以上工业增加值年率-单月|社会消费品零售总额年率|制造业PMI|出口年率",
                          "cn_inflation": "CPI年率|PPI年率", "us": "CPI年率|非农就业人数|失业率"},
               "rule": "mean(reaction | beat) - mean(reaction | miss): |t|>=2 → informative"}


def _get(url, params, referer, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA, "Referer": referer}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def tstat(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else None


def stats(xs):
    """xs: 数值列表, 或 (数值, 入场日) 列表 → 额外给按日期聚类的 t_day."""
    pairs = [x if isinstance(x, tuple) else (x, None) for x in xs]
    pairs = [(v, d) for v, d in pairs if v is not None and v == v]
    if not pairs:
        return {"n": 0}
    vals = [v for v, _ in pairs]
    s = sorted(vals)
    out = {"n": len(vals), "mean_pct": round(sum(vals) / len(vals) * 100, 3), "median_pct": round(s[len(s) // 2] * 100, 3),
           "win_rate": round(sum(1 for x in vals if x > 0) / len(vals), 3), "t": round(tstat(vals) or 0, 2)}
    if pairs[0][1] is not None:
        by = defaultdict(list)
        for v, d in pairs:
            by[d].append(v)
        dm = [sum(v) / len(v) for v in by.values()]
        out["n_days"] = len(dm)
        out["t_day"] = round(tstat(dm) or 0, 2)
    return out


# ── 研报 ────────────────────────────────────────────────────────────────
def fetch_reports():
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = []
    m = date(2023, 1, 1)
    while m <= date(2026, 9, 1):
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        f = CACHE / f"reports_{m:%Y%m}.json"
        if f.exists():
            month = json.loads(f.read_text(encoding="utf-8"))
        else:
            month, page = [], 1
            while True:
                j = _get("https://reportapi.eastmoney.com/report/list", dict(
                    industryCode="*", pageSize=100, industry="*", rating="*", ratingChange="*",
                    beginTime=m.isoformat(), endTime=(nxt - timedelta(days=1)).isoformat(), pageNo=page, fields="",
                    qType=0, orgCode="", code="*", rcode=""), "https://data.eastmoney.com/")
                d = j.get("data") or []
                month += [{k: x.get(k) for k in ("stockCode", "stockName", "orgSName", "emRatingName",
                                                  "lastEmRatingName", "publishDate", "indvInduName")} for x in d]
                if not d or page >= int(j.get("TotalPage") or 1):
                    break
                page += 1
                time.sleep(0.3)
            f.write_text(json.dumps(month, ensure_ascii=False), encoding="utf-8")
        rows += month
        print(f"  reports {m:%Y-%m}: {len(month)}", flush=True)
        m = nxt
    return rows


def yahoo_symbol(code):
    if code.startswith(("6", "9")):
        return f"{code}.SS"
    if code.startswith(("0", "3", "2")):
        return f"{code}.SZ"
    return None      # 北交所 (4/8/92) Yahoo 不全 → 走东方财富


def limit_pct(code):
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def _progress(msg):
    try:
        f = HERE / "logs" / "ashare_bt_reports_progress.txt"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def fetch_prices(codes):
    """Yahoo 分批下载 (每批落盘, 可断点续跑); Yahoo 缺的用东方财富兜底 (只试一次, 短超时)."""
    import yfinance as yf
    import pandas as pd
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "prices.pkl"
    tried_f = CACHE / "prices_tried.json"
    have = pd.read_pickle(f) if f.exists() else {}
    tried = set(json.loads(tried_f.read_text(encoding="utf-8"))) if tried_f.exists() else set()
    need = [c for c in codes if c not in have and c not in tried]
    syms = {c: yahoo_symbol(c) for c in need}
    ylist = [s for s in syms.values() if s]
    _progress(f"prices: have {len(have)}, need {len(need)} (yahoo {len(ylist)})")
    for i in range(0, len(ylist), 60):
        chunk = ylist[i:i + 60]
        try:
            df = yf.download(chunk, start="2022-12-01", end="2026-10-17", auto_adjust=True, group_by="ticker",
                             threads=True, progress=False, timeout=30)
        except Exception as e:      # noqa: BLE001
            _progress(f"yf chunk {i} failed: {e}")
            time.sleep(5)
            continue
        for c, s in syms.items():
            if s in chunk:
                tried.add(c)
                try:
                    sub = df[s][["Open", "Close"]].dropna() if len(chunk) > 1 else df[["Open", "Close"]].dropna()
                    if len(sub):
                        have[c] = sub
                except Exception:
                    pass
        pd.to_pickle(have, f)
        tried_f.write_text(json.dumps(sorted(tried)), encoding="utf-8")
        _progress(f"yahoo {min(i + 60, len(ylist))}/{len(ylist)}, have {len(have)}")
    miss = [c for c in codes if c not in have][:400]
    _progress(f"eastmoney fallback for {len(miss)}")
    for n, c in enumerate(miss):
        secid = ("1." if c.startswith(("6", "9")) and not c.startswith("92") else "0.") + c
        try:
            r = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", headers={
                "User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}, timeout=8, params=dict(
                secid=secid, fields1="f1,f2,f3", fields2="f51,f52,f53", klt=101, fqt=2, beg="20221201", end="20261017"))
            k = ((r.json().get("data") or {}).get("klines")) or []
            if k:
                idx = [pd.Timestamp(x.split(",")[0]) for x in k]
                have[c] = pd.DataFrame({"Open": [float(x.split(",")[1]) for x in k],
                                        "Close": [float(x.split(",")[2]) for x in k]}, index=idx)
        except Exception:
            pass
        if n % 50 == 49:
            pd.to_pickle(have, f)
            _progress(f"eastmoney {n + 1}/{len(miss)}, have {len(have)}")
        time.sleep(0.15)
    pd.to_pickle(have, f)
    return have


def run_reports():
    import pandas as pd
    from cn_brief.brief import rating_change, rating_rank
    OUT.mkdir(parents=True, exist_ok=True)
    rows = fetch_reports()
    print(f"total reports {len(rows)}", flush=True)
    _progress(f"reports {len(rows)}")
    # 股票×日 聚合
    sd = defaultdict(lambda: {"changes": set(), "orgs": set(), "ratings": set()})
    for r in rows:
        c = str(r.get("stockCode") or "")
        if not re.fullmatch(r"\d{6}", c):
            continue
        d = str(r.get("publishDate") or "")[:10]
        x = sd[(c, d)]
        x["changes"].add(rating_change(r))
        x["orgs"].add(r.get("orgSName"))
        x["ratings"].add(r.get("emRatingName") or "")
    # MULTI3: 6 天内 ≥3 家机构, 达到当天触发
    by_code = defaultdict(list)
    for (c, d), x in sd.items():
        by_code[c].append((d, x))
    multi = set()
    for c, lst in by_code.items():
        lst.sort()
        fired_until = ""
        for i, (d, x) in enumerate(lst):
            dd = date.fromisoformat(d)
            orgs = set()
            for d2, x2 in lst[:i + 1]:
                if (dd - date.fromisoformat(d2)).days <= 6:
                    orgs |= x2["orgs"]
            if len(orgs) >= 3 and d > fired_until:
                multi.add((c, d))
                fired_until = (dd + timedelta(days=6)).isoformat()
    events = []
    for (c, d), x in sd.items():
        cats = ["ALL"]
        if "调高" in x["changes"]:
            cats.append("UP")
        if "调低" in x["changes"]:
            cats.append("DOWN")
        if "首次" in x["changes"] and any((rating_rank(r) or 0) >= 4 for r in x["ratings"]):
            cats.append("FIRST")
        if x["changes"] == {"维持"} and "买入" in x["ratings"]:
            cats.append("MAINT_BUY")
        if (c, d) in multi:
            cats.append("MULTI3")
        events.append((c, d, cats))
    codes = sorted({c for c, _, _ in events})
    print(f"stock-days {len(events)}, stocks {len(codes)}", flush=True)
    px = fetch_prices(codes)
    import yfinance as yf
    bench = yf.Ticker(BENCH).history(start="2022-12-01", end="2026-10-17", auto_adjust=True)[["Open", "Close"]]
    bench.index = bench.index.tz_localize(None).normalize()
    print(f"prices for {len(px)}/{len(codes)} stocks; bench {len(bench)} bars", flush=True)

    res = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))   # cat → period → key → [ar]
    skipped = defaultdict(int)
    for c, d, cats in events:
        period = "train" if TRAIN[0] <= d <= TRAIN[1] else ("oos" if OOS[0] <= d <= OOS[1] else None)
        if not period:
            continue
        df = px.get(c)
        if df is None or len(df) < 30:
            skipped["no_price"] += 1
            continue
        idx = df.index.tz_localize(None).normalize() if getattr(df.index, "tz", None) else df.index
        pos = idx.searchsorted(pd.Timestamp(d) + pd.Timedelta(days=1))
        if pos <= 0 or pos >= len(df):
            skipped["no_entry_bar"] += 1
            continue
        entry_day = idx[pos]
        o, prev_c = float(df["Open"].iloc[pos]), float(df["Close"].iloc[pos - 1])
        limit_up = o >= prev_c * (1 + limit_pct(c) - 0.002)
        bpos = bench.index.searchsorted(entry_day)
        if bpos >= len(bench) or bench.index[bpos] != entry_day:
            skipped["bench_missing"] += 1
            continue
        for h in HORIZONS:
            ep = pos + h - 1
            if ep >= len(df) or bpos + h - 1 >= len(bench):
                continue
            r = float(df["Close"].iloc[ep]) / o - 1
            br = float(bench["Close"].iloc[bpos + h - 1]) / float(bench["Open"].iloc[bpos]) - 1
            dkey = str(entry_day.date())
            for cat in cats:
                res[cat][period][f"h{h}"].append((r - br, dkey))
                if not limit_up:
                    res[cat][period][f"h{h}_tradeable"].append((r - br, dkey))
        for cat in cats:
            res[cat][period]["n_limit_up"].append(1 if limit_up else 0)
    summary = {}
    for cat, per in res.items():
        summary[cat] = {}
        for period, d in per.items():
            summary[cat][period] = {k: stats(v) for k, v in d.items() if k != "n_limit_up"}
            lu = d.get("n_limit_up", [])
            summary[cat][period]["limit_up_share"] = round(sum(lu) / len(lu), 3) if lu else None
    verdicts = {}
    for cat in ("UP", "FIRST", "MULTI3", "MAINT_BUY", "DOWN", "ALL"):
        tr = summary.get(cat, {}).get("train", {}).get("h20", {})
        oo = summary.get(cat, {}).get("oos", {}).get("h20", {})
        ok = (tr.get("n", 0) >= 100 and oo.get("n", 0) >= 100 and tr.get("t_day") and oo.get("t_day")
              and abs(tr["t_day"]) >= 2 and abs(oo["t_day"]) >= 2 and (tr["mean_pct"] > 0) == (oo["mean_pct"] > 0))
        verdicts[cat] = "useful" if ok else "not_proven"
    out = {"prereg": PREREG_REPORTS, "n_reports": len(rows), "n_stock_days": len(events), "n_stocks": len(codes),
           "n_priced": len(px), "skipped": dict(skipped), "summary": summary, "verdicts": verdicts}
    (OUT / "report_signals.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        from backtest_verdicts import write_verdict
        write_verdict("ashare_report_signals", "pass" if any(v == "useful" for k, v in verdicts.items() if k != "ALL") else "reject",
                      conclusion=json.dumps(verdicts, ensure_ascii=False), metrics={"summary": summary},
                      params=PREREG_REPORTS, should_integrate=False, recommendation="仅作信息参考, 不接入交易")
    except Exception as e:      # noqa: BLE001
        print("verdict write failed", e)
    print(json.dumps(verdicts, ensure_ascii=False))


# ── 经济数据意外 ─────────────────────────────────────────────────────────
GROUPS = {k: re.compile(v) for k, v in PREREG_ECON["groups"].items()}
GROUP_REGION = {"cn_growth": "中国", "cn_inflation": "中国", "us": "美国"}
EXCLUDE = re.compile(r"年初至今|月率|季率|核心|非制造业|服务业|综合|财新服务|进口|总量")


def _f(v):
    try:
        x = float(str(v).replace("%", ""))
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def fetch_calendar():
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "calendar.json"
    have = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    d = date(2024, 1, 1)
    while d <= date(2026, 9, 30):
        k = d.isoformat()
        if k not in have:
            rows = []
            for pn in range(0, 10):
                try:
                    j = _get("https://finance.pae.baidu.com/sapi/v1/financecalendar", dict(
                        start_date=k, end_date=k, pn=pn, rn=30, cate="economic_data", finClientType="pc"),
                        "https://gushitong.baidu.com/", tries=2)
                except Exception:
                    break
                info = (j.get("Result") or {}).get("calendarInfo") or []
                page = [x for b in info for x in (b.get("list") or [])]
                rows += [x for x in page if x.get("region") in ("中国", "美国")]
                if len(page) < 30:
                    break
                time.sleep(0.15)
            have[k] = [{kk: x.get(kk) for kk in ("region", "title", "time", "pubVal", "indicateVal", "formerVal", "star")}
                       for x in rows]
            if d.day == 1:
                f.write_text(json.dumps(have, ensure_ascii=False), encoding="utf-8")
                print(f"  calendar {k}", flush=True)
        d += timedelta(days=1)
    f.write_text(json.dumps(have, ensure_ascii=False), encoding="utf-8")
    return have


def run_econ():
    import pandas as pd
    import yfinance as yf
    OUT.mkdir(parents=True, exist_ok=True)
    cal = fetch_calendar()
    bench = yf.Ticker(BENCH).history(start="2023-12-01", end="2026-10-17", auto_adjust=True)[["Close"]]
    bench.index = bench.index.tz_localize(None).normalize()
    ret = bench["Close"].pct_change()
    obs = defaultdict(lambda: {"beat": [], "miss": [], "inline": [], "rows": []})
    for day, rows in cal.items():
        for x in rows:
            title = str(x.get("title") or "")
            if EXCLUDE.search(title):
                continue
            grp = next((g for g, p in GROUPS.items() if p.search(title) and x.get("region") == GROUP_REGION[g]), None)
            if not grp:
                continue
            pub, exp = _f(x.get("pubVal")), _f(x.get("indicateVal"))
            if pub is None or exp is None:
                continue
            try:
                hh, mm = [int(t) for t in str(x.get("time") or "00:00").split(":")[:2]]
            except ValueError:
                hh, mm = 0, 0
            ts = pd.Timestamp(day)
            after_close = (hh * 60 + mm) >= 15 * 60
            pos = ret.index.searchsorted(ts + (pd.Timedelta(days=1) if after_close else pd.Timedelta(0)))
            if pos >= len(ret):
                continue
            r = float(ret.iloc[pos])
            if r != r:
                continue
            # 符号约定: 增长类 "好于预期"=公布>预期; 失业率反向; 通胀类按 "高于预期" 记 beat (方向本身就有争议)
            diff = pub - exp
            if "失业率" in title:
                diff = -diff
            key = "beat" if diff > 0 else ("miss" if diff < 0 else "inline")
            obs[grp][key].append(r)
            obs[grp]["rows"].append({"day": day, "title": title, "pub": pub, "exp": exp, "react_day": str(ret.index[pos].date()),
                                     "ret_pct": round(r * 100, 3), "side": key})
    out = {"prereg": PREREG_ECON, "groups": {}}
    for g, o in obs.items():
        b, m = o["beat"], o["miss"]
        diff_t = None
        if len(b) >= 3 and len(m) >= 3:
            mb, mm_ = sum(b) / len(b), sum(m) / len(m)
            vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
            vm = sum((x - mm_) ** 2 for x in m) / (len(m) - 1)
            se = math.sqrt(vb / len(b) + vm / len(m))
            diff_t = round((mb - mm_) / se, 2) if se > 0 else None
        out["groups"][g] = {"beat": stats(b), "miss": stats(m), "inline": stats(o["inline"]), "diff_t": diff_t,
                            "informative": bool(diff_t is not None and abs(diff_t) >= 2), "sample": o["rows"][-12:]}
    (OUT / "econ_surprise.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        from backtest_verdicts import write_verdict
        inf = {g: v["informative"] for g, v in out["groups"].items()}
        write_verdict("ashare_econ_surprise", "pass" if any(inf.values()) else "reject",
                      conclusion=json.dumps(inf, ensure_ascii=False), metrics={g: {k: v[k] for k in ("beat", "miss", "diff_t")}
                                                                             for g, v in out["groups"].items()},
                      params=PREREG_ECON, should_integrate=False, recommendation="仅作信息参考, 不接入交易")
    except Exception as e:      # noqa: BLE001
        print("verdict write failed", e)
    print(json.dumps({g: (v["diff_t"], v["beat"].get("n"), v["miss"].get("n")) for g, v in out["groups"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "reports"
    status = HERE / "logs" / f"ashare_bt_{mode}_status.txt"   # 看门狗后台任务的屏幕输出进不了日志, 状态单独落盘
    status.write_text(f"start {datetime.now().isoformat()}\n", encoding="utf-8")
    try:
        (run_econ if mode == "econ" else run_reports)()
        status.write_text(status.read_text(encoding="utf-8") + f"done {datetime.now().isoformat()}\n", encoding="utf-8")
    except Exception:
        import traceback
        status.write_text(status.read_text(encoding="utf-8") + traceback.format_exc(), encoding="utf-8")
        raise
