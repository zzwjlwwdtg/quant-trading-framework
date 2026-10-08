"""强美元阶段里, 系统可交易的资产怎么表现? —— 预注册检验 (2026-10-08).

用户设定的交易主线: 强美元, 后续可能抽干世界其他地区的流动性.
主线本身是用户的判断; 本检验只回答"在强美元阶段, 每类资产历史上是相对跑赢还是跑输",
用来决定主线落到哪些标的上 (白名单 / 提高门槛), 不拍脑袋.

强美元阶段 (只用当天及以前的数据):
  A: 美元指数 DXY 20 日涨幅 处于过去 252 日前 10%   (急涨)
  B: 美元指数 DXY 60 日涨幅 处于过去 252 日前 20%   (持续走强)
结果 (次日收盘起算):
  · 相对表现: 资产 20 日 / 60 日收益 − SPY 同期收益 (SPY 自身看绝对收益)
  · 显著性: 随机平移检验 2000 次, 双侧 p; 独立事件 = 间隔 >10 个交易日
分期: 训练 2003-2014 / 样本外 2015-至今 (资产上市晚的从上市第 2 年起算)
判定 (按阶段 A、B 分别给, 以 20 日相对收益为主指标):
  强美元下跑输: 训练期 差<0 且 p<0.05, 样本外同方向, 两期各 ≥8 个独立事件
  强美元下跑赢: 训练期 差>0 且 p<0.05, 样本外同方向, 两期各 ≥8 个独立事件
  无稳定关系 / 数据不足: 其他
"流动性被抽干"的直接检验看 EEM / FXI / EWJ / EFA (非美股市) 和 HYG (美国信用) 的结果.
只读研究, 不下单.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import _flow_history_test as ft

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "development" / "2026-10-08" / "usd_regime"
CACHE = OUT_DIR / "cache"
ASSETS = {
    "SPY": "美股大盘", "QQQ": "纳指100", "IWM": "美国小盘(内需)", "XLV": "医疗(防御)",
    "MSFT": "微软", "AAPL": "苹果", "SOXX": "半导体",
    "EEM": "新兴市场", "FXI": "中国大盘", "EWJ": "日本", "EFA": "发达市场(非美)",
    "GLD": "黄金", "USO": "原油", "SHY": "1-3年美债", "IEI": "3-7年美债", "TLT": "20年+美债",
    "HYG": "美国高收益债",
}
MIN_EP = 8
PERIODS = [("train", "2003-01-01", "2014-12-31"), ("oos", "2015-01-01", "2100-01-01")]


def _px(tk: str) -> pd.Series:
    f = CACHE / f"px_{tk.replace('^', '').replace('.', '_').replace('-', '_')}.csv"
    if not f.exists() or time.time() - f.stat().st_mtime > 86400:
        import yfinance as yf
        df = yf.Ticker(tk).history(start="1995-01-01", interval="1d", auto_adjust=True)
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
        df[["Close"]].to_csv(f)
    s = pd.read_csv(f, index_col=0, parse_dates=True)["Close"].sort_index()
    s.index = s.index.normalize()
    return s[~s.index.duplicated(keep="last")]


def regime_flags(dxy: pd.Series) -> dict[str, pd.Series]:
    def tail(chg, q):
        r = ft.rolling_rank(chg)
        return (r >= q).astype(float).where(r.notna())
    return {"A_20d_top10": tail(dxy.pct_change(20), 0.90), "B_60d_top20": tail(dxy.pct_change(60), 0.80)}


def fwd(px: pd.Series, h: int) -> pd.Series:
    return px.shift(-1 - h) / px.shift(-1) - 1


def evaluate_asset(flag: pd.Series, asset: pd.Series, spy: pd.Series, relative: bool) -> dict:
    idx = flag.dropna().index.intersection(asset.index).intersection(spy.index)
    a, s = asset.reindex(idx), spy.reindex(idx)
    first_valid = asset.first_valid_index()
    res = {}
    for name, lo, hi in PERIODS:
        lo_eff = max(pd.Timestamp(lo), first_valid + pd.Timedelta(days=365)) if first_valid is not None else pd.Timestamp(lo)
        sel = (idx >= lo_eff) & (idx <= pd.Timestamp(hi))
        if sel.sum() < 500:
            res[name] = {"insufficient": True, "n_days": int(sel.sum())}
            continue
        f = (flag.reindex(idx)[sel] == 1)
        out = {"from": str(idx[sel][0].date()), "to": str(idx[sel][-1].date()),
               "n_signal_days": int(f.sum()), "episodes": ft.count_episodes(f)}
        for h in (20, 60):
            y = fwd(a, h) - (fwd(s, h) if relative else 0)
            yv = y[sel].values.astype(float)
            diff, p = ft.rotation_pvalue(f.values, yv)
            out[f"h{h}"] = {"cond": round(float(np.nanmean(yv[f.values])), 5) if f.any() else None,
                            "base": round(float(np.nanmean(yv)), 5), "diff": round(diff, 5), "p": round(p, 4)}
        res[name] = out
    return res


def verdict(ev: dict) -> str:
    tr, oo = ev.get("train", {}), ev.get("oos", {})
    if tr.get("insufficient") or oo.get("insufficient"):
        return "insufficient_data"
    if tr["episodes"] < MIN_EP or oo["episodes"] < MIN_EP:
        return "insufficient_episodes"
    t, o = tr["h20"], oo["h20"]
    if t["diff"] < 0 and t["p"] < 0.05 and o["diff"] < 0:
        return "underperform"
    if t["diff"] > 0 and t["p"] < 0.05 and o["diff"] > 0:
        return "outperform"
    return "no_stable_relation"


VZ = {"underperform": "强美元下跑输", "outperform": "强美元下跑赢", "no_stable_relation": "无稳定关系",
      "insufficient_data": "数据不足", "insufficient_episodes": "独立事件太少"}


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    dxy = _px("DX-Y.NYB")
    spy = _px("SPY")
    print(f"[px] DXY {len(dxy)} {dxy.index.min().date()}~{dxy.index.max().date()}")
    flags = regime_flags(dxy)
    results = {}
    for tk, label in ASSETS.items():
        try:
            px = _px(tk)
        except Exception as e:  # noqa: BLE001
            print(f"[px] {tk} fail {e}"); continue
        rel = tk != "SPY"
        results[tk] = {"label": label, "relative_to_spy": rel, "regimes": {}}
        for rname, f in flags.items():
            ev = evaluate_asset(f, px, spy, rel)
            results[tk]["regimes"][rname] = {"verdict": verdict(ev), "eval": ev}
        print(f"[result] {tk}: " + ", ".join(f"{r}={v['verdict']}" for r, v in results[tk]["regimes"].items()))
    cur = {"dxy": round(float(dxy.iloc[-1]), 2),
           "dxy_20d_pct": round(float(dxy.iloc[-1] / dxy.iloc[-21] - 1) * 100, 2),
           "dxy_60d_pct": round(float(dxy.iloc[-1] / dxy.iloc[-61] - 1) * 100, 2),
           "A_active": bool(flags["A_20d_top10"].iloc[-1] == 1), "B_active": bool(flags["B_60d_top20"].iloc[-1] == 1)}
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "preregistered": __doc__,
           "current": cur, "results": results}
    (OUT_DIR / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / "report.md").write_text(render(out), encoding="utf-8")
    print("written", OUT_DIR, cur)


def render(out: dict) -> str:
    L = ["# 强美元阶段的资产表现 (预注册)", "", f"生成: {out['generated_at']}", "",
         f"当前: {json.dumps(out['current'], ensure_ascii=False)}", "",
         "A = DXY 20 日急涨 (前 10%); B = DXY 60 日持续走强 (前 20%). 非 SPY 资产为相对 SPY 的超额收益. p = 随机平移检验.", "",
         "| 资产 | 阶段 | 结论 | 训练期 20日差 (p) | 样本外 20日差 (p) | 训练/样本外 60日差 | 事件数 训练/样本外 |",
         "|---|---|---|---|---|---|---|"]
    for tk, r in out["results"].items():
        for rn, v in r["regimes"].items():
            e = v["eval"]; tr, oo = e.get("train", {}), e.get("oos", {})
            if tr.get("insufficient") or oo.get("insufficient"):
                L.append(f"| {tk} {r['label']} | {rn[0]} | {VZ[v['verdict']]} | — | — | — | — |"); continue
            L.append(f"| {tk} {r['label']} | {rn[0]} | {VZ[v['verdict']]} | "
                     f"{tr['h20']['diff']*100:+.2f}% ({tr['h20']['p']:.3f}) | {oo['h20']['diff']*100:+.2f}% ({oo['h20']['p']:.3f}) | "
                     f"{tr['h60']['diff']*100:+.2f}% / {oo['h60']['diff']*100:+.2f}% | {tr['episodes']}/{oo['episodes']} |")
    return "\n".join(L) + "\n"


def _run_logged():
    import traceback
    log = HERE / "logs" / "usd_regime_test.log"
    with open(log, "w", encoding="utf-8") as fh:
        class Tee:
            def write(self, s): fh.write(s); fh.flush()
            def flush(self): fh.flush()
        sys.stdout = sys.stderr = Tee()
        try:
            main(); return 0
        except Exception:
            traceback.print_exc(); return 1


if __name__ == "__main__":
    sys.exit(_run_logged())
