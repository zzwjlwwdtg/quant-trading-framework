"""外围隔夜指标 → A股 开盘跳空 / 日内走势 的预测力 (预注册, 只读; 2026-10-19).

对齐: A股交易日 D 的 "隔夜外围" = Close(最近一个 < D 的外围交易日) / Close(最近一个 < 上一个A股交易日 的外围交易日) − 1
      (即上一个 A股收盘之后到 D 开盘之前完成的外围交易日累计变化; 长假自动累计)
目标: gap = Open_D / Close_{D-1} − 1;  intraday = Close_D / Open_D − 1;  cc = Close_D / Close_{D-1} − 1
标的: 510300.SS (沪深300ETF), 159915.SZ (创业板ETF)
外围: ^GSPC 标普500, ^IXIC 纳指, ^HXC 纳斯达克中国金龙 (缺则 KWEB), CNH=X 美元/离岸人民币, ^TNX 美10年期收益率 (差值), DX-Y.NYB 美元指数
期间: 训练 2016-01-01 ~ 2021-12-31, 样本外 2022-01-01 ~ 数据结束
预注册 "有预测力": 训练与样本外 相关系数同号, |t_r| ≥ 2, 且符号命中率 ≥ 55% (外围与目标同涨同跌的比例)
结果用于 cn_brief 盘前: 通过的指标才附 "历史上…" 统计, 否则只给数字并注明未证明.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OUT = HERE.parent / "development" / "2026-10-19" / "overnight"
STATUS = HERE / "logs" / "overnight_bt_status.txt"
TARGETS = {"510300.SS": "沪深300ETF", "159915.SZ": "创业板ETF"}
DRIVERS = {"^GSPC": "标普500", "^IXIC": "纳指", "^HXC": "中概金龙", "CNH=X": "美元/离岸人民币",
           "^TNX": "美10年期收益率", "DX-Y.NYB": "美元指数"}
LEVEL_DIFF = {"^TNX"}          # 用差值而非涨跌幅
TRAIN = ("2016-01-01", "2021-12-31")
OOS = ("2022-01-01", "2099-12-31")
PREREG = {"targets": TARGETS, "drivers": DRIVERS, "train": TRAIN, "oos": OOS,
          "rule": "corr same sign train & oos, |t_r|>=2 both, sign hit >= 0.55 both",
          "alignment": "cumulative driver change over sessions completed after previous A close and before A open"}


def log(msg):
    with open(STATUS, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} {msg}\n")


def corr_stats(xs, ys):
    n = len(xs)
    if n < 30:
        return {"n": n}
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return {"n": n}
    r = sxy / math.sqrt(sxx * syy)
    t = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
    beta = sxy / sxx
    nz = [(x, y) for x, y in zip(xs, ys) if x != 0 and y != 0]
    hit = sum(1 for x, y in nz if (x > 0) == (y > 0)) / len(nz) if nz else None
    q = sorted(zip(xs, ys))
    k = max(1, n // 5)
    lo, hi = q[:k], q[-k:]
    return {"n": n, "r": round(r, 3), "t_r": round(t, 2), "beta": round(beta, 4), "hit": round(hit, 3) if hit else None,
            "top20_mean_pct": round(sum(y for _, y in hi) / k * 100, 3),
            "bottom20_mean_pct": round(sum(y for _, y in lo) / k * 100, 3)}


def main():
    import yfinance as yf
    import pandas as pd
    OUT.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(f"start {datetime.now().isoformat()}\n", encoding="utf-8")
    # 2026-10-19: Yahoo 上 ^HXC / CNH=X 只给 1 根 K 线 → 按顺序换备用代码 (cn_brief.overseas 实盘取数用同一张表)
    from cn_brief.overseas import ALTERNATES
    px, used = {}, {}
    for tk in list(TARGETS) + list(DRIVERS):
        for cand in [tk] + ALTERNATES.get(tk, []):
            try:
                df = yf.Ticker(cand).history(start="2015-06-01", auto_adjust=True)
                if len(df) < 500:
                    df = yf.Ticker(cand).history(period="max", auto_adjust=True)
                if getattr(df.index, "tz", None) is not None:
                    df.index = df.index.tz_localize(None)
                df.index = df.index.normalize()
                df = df[df.index >= "2015-06-01"]
                log(f"{tk} via {cand}: {len(df)} bars {str(df.index.min())[:10]}~{str(df.index.max())[:10]}")
                if len(df) >= 500:
                    px[tk] = df[["Open", "Close"]].dropna()
                    used[tk] = cand
                    break
            except Exception as e:      # noqa: BLE001
                log(f"{tk} via {cand}: failed {e}")
    for tk, cand in used.items():
        if cand != tk and tk in DRIVERS:
            DRIVERS[tk] = f"{DRIVERS[tk]} (用 {cand} 代替)"
    results = {}
    for tgt, tname in TARGETS.items():
        a = px.get(tgt)
        if a is None or len(a) < 500:
            continue
        a = a.copy()
        a["prev_close"] = a["Close"].shift(1)
        a["prev_date"] = pd.Series(a.index, index=a.index).shift(1)
        a = a.dropna()
        for drv, dname in DRIVERS.items():
            d = px.get(drv)
            if d is None or len(d) < 500:
                continue
            dc = d["Close"]
            pos_now = dc.index.searchsorted(a.index, side="left") - 1          # 最近一个 < D
            pos_prev = dc.index.searchsorted(pd.DatetimeIndex(a["prev_date"]), side="left") - 1
            rows = {"train": {"gap": [], "intraday": [], "cc": [], "x": []}, "oos": {"gap": [], "intraday": [], "cc": [], "x": []}}
            for i, D in enumerate(a.index):
                p1, p0 = pos_now[i], pos_prev[i]
                if p1 < 0 or p0 < 0 or p1 <= p0:
                    continue        # 两次 A股收盘之间没有新的外围收盘 → 无隔夜信息
                c1, c0 = float(dc.iloc[p1]), float(dc.iloc[p0])
                x = (c1 - c0) if drv in LEVEL_DIFF else (c1 / c0 - 1)
                row = a.iloc[i]
                per = "train" if TRAIN[0] <= str(D.date()) <= TRAIN[1] else "oos"
                rows[per]["x"].append(x)
                rows[per]["gap"].append(float(row["Open"]) / float(row["prev_close"]) - 1)
                rows[per]["intraday"].append(float(row["Close"]) / float(row["Open"]) - 1)
                rows[per]["cc"].append(float(row["Close"]) / float(row["prev_close"]) - 1)
            res = {}
            for per, r in rows.items():
                res[per] = {y: corr_stats(r["x"], r[y]) for y in ("gap", "intraday", "cc")}
                res[per]["x_std"] = round((sum(v * v for v in r["x"]) / max(1, len(r["x"]))) ** 0.5, 5)
            verdict = {}
            for y in ("gap", "intraday", "cc"):
                tr, oo = res["train"][y], res["oos"][y]
                ok = all(k in tr and k in oo for k in ("r", "t_r")) and (tr["r"] > 0) == (oo["r"] > 0) \
                    and abs(tr["t_r"]) >= 2 and abs(oo["t_r"]) >= 2 and (tr.get("hit") or 0) >= 0.55 and (oo.get("hit") or 0) >= 0.55
                verdict[y] = "useful" if ok else "not_proven"
            res["verdict"] = verdict
            results.setdefault(tgt, {})[drv] = res
            log(f"{tgt} {drv}: {verdict}")
    # 跳空后日内是延续还是回落 (节奏): corr(gap, intraday)
    fade = {}
    for tgt in TARGETS:
        a = px.get(tgt)
        if a is None:
            continue
        g = (a["Open"] / a["Close"].shift(1) - 1).dropna()
        it = (a["Close"] / a["Open"] - 1).reindex(g.index)
        for per, (s, e) in {"train": TRAIN, "oos": OOS}.items():
            m = (g.index >= s) & (g.index <= e)
            fade.setdefault(tgt, {})[per] = corr_stats(list(g[m]), list(it[m]))
    out = {"prereg": PREREG, "drivers": DRIVERS, "symbols_used": used, "results": results, "gap_vs_intraday": fade,
           "run_at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "overnight.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        from backtest_verdicts import write_verdict
        useful = {f"{t}|{d}|{y}": v for t, dd in results.items() for d, r in dd.items() for y, v in r["verdict"].items() if v == "useful"}
        write_verdict("overnight_ashare", "pass" if useful else "reject", conclusion=json.dumps(useful, ensure_ascii=False),
                      metrics={"results": results, "gap_vs_intraday": fade}, params=PREREG, should_integrate=False,
                      recommendation="cn_brief 盘前: 通过的指标附历史统计; 不接入交易")
    except Exception as e:      # noqa: BLE001
        log(f"verdict failed {e}")
    log("done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log(traceback.format_exc())
        raise
