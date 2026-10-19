"""EVO-20261013-3 重做: 盘前止盈延迟到 RTH+15min 的长周期回测 (只读, 不下单).

2026-10-19 用户退回原版: 原版样本 = 系统自己的成交记录, 最早只到 2026 年, 训练期 0 笔, 回测不成立.
本版改用历史行情 + 合成入场, 不依赖成交记录.

数据: Yahoo 1 小时 K 线 (含盘前/盘后, prepost=True), Yahoo 上限约 730 天.
时点与实盘 config.DAILY_WINDOWS_ET 对齐, 每个交易日 4 个检查点:
  pre-market 08:30 ET  → 08:00 那根 1h bar 的 (O+C)/2
  post-open  10:00 ET  → 09:30 那根 1h bar 的 (O+C)/2   (延迟止盈在这里执行, ≥09:45)
  midday     12:00 ET  → 11:30 那根 1h bar 的 (O+C)/2
  pre-close  15:50 ET  → 15:30 那根 1h bar 的 Close
入场 (合成, 预注册): 空仓时, pre-close 价格 ≥ 前 20 个交易日 pre-close 价最高值 → 以该价买入 1.0 单位.
出场: 阶梯止盈 (+15/+30/+50% × √杠杆, 卖原始仓位 30/30/40%), 移动止损 8% × √杠杆 (从入场后最高检查价回撤),
      持有满 60 个交易日 pre-close 平仓. 止损两种策略完全相同, 只有盘前止盈的处理不同:
  A (现制度): pre-market 检查点价格 ≥ 止盈线 → 立即按该价卖.
  B (EVO-3):  pre-market 命中 → 延迟; post-open 价格仍 ≥ 止盈线 → 按 post-open 价卖, 否则取消, 之后窗口照常重新评估.

预注册通过标准 (跑前写死, 见 PREREG):
  训练期 / 样本外 各自: 盘前止盈事件 ≥ 20 个 (否则 inconclusive);
  avg_delta_pp (B 成交价 vs A 成交价, 仅 B 成交的事件) ≥ 0;
  组合总收益 total_B ≥ total_A - 0.02 × |total_A|.
  两期都满足 → pass; 任一期不满足 → reject.
敏感性 (只报告, 不参与判定): pre-market 用 08:00 bar Close; post-open 用 09:30 bar Open.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

NAME = "premarket_tp_defer_long"
PREREG = {
    "proposal": "EVO-20261013-3",
    "version": "long-v1 (2026-10-19, 用户退回后重做)",
    "data": "yfinance 1h prepost=True period=730d",
    "train": ["start", "2025-12-31"],
    "oos": ["2026-01-01", "end"],
    "check_points_et": {"pre-market": "08:00 bar mid", "post-open": "09:30 bar mid",
                        "midday": "11:30 bar mid", "pre-close": "15:30 bar close"},
    "entry": "flat & pre-close price >= max(prior 20 pre-close prices)",
    "tp_levels_base": [[0.15, 0.30, "tp15"], [0.30, 0.30, "tp30"], [0.50, 0.40, "tp50"]],
    "trailing_stop_base": 0.08,
    "hold_days": 60,
    "min_events_per_period": 20,
    "fill_delta_min_pp": 0.0,
    "total_tolerance": 0.02,
    "pass_rule": "train & oos: events>=20, avg_delta_pp>=0, total_B >= total_A - 0.02*|total_A|",
}
TP_LEVELS = [(0.15, 0.30, "tp15"), (0.30, 0.30, "tp30"), (0.50, 0.40, "tp50")]
TRAIL = 0.08
HOLD_DAYS = 60
BREAKOUT_N = 20


def universe() -> list[str]:
    """预注册股票池: 系统交易过的全部代码 ∪ config.TICKERS ∪ 杠杆 ETF 表."""
    from config import LEVERAGE_FACTORS, TICKERS
    s = set(TICKERS) | set(LEVERAGE_FACTORS)
    log = HERE / "signals" / "trade_log.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                s.add(json.loads(line)["ticker"])
            except Exception:
                pass
    return sorted(t for t in s if t.startswith("US."))


def lev_sqrt(ticker: str) -> float:
    from config import LEVERAGE_FACTORS
    return math.sqrt(LEVERAGE_FACTORS.get(ticker, 1.0))


def fetch(ticker: str):
    import yfinance as yf
    df = yf.Ticker(ticker.split(".", 1)[1]).history(period="730d", interval="1h", prepost=True,
                                                    auto_adjust=True)
    if df is None or df.empty:
        return None
    df.index = df.index.tz_convert("America/New_York")
    return df


def daily_points(df, sens: bool = False) -> list[dict]:
    """每个交易日 → {date, pm, po, md, pc}; 缺 post-open 或 pre-close 的日子丢弃."""
    out = []
    for d, g in df.groupby(df.index.date):
        bars = {(ts.hour, ts.minute): row for ts, row in g.iterrows()}

        def mid(key):
            r = bars.get(key)
            return None if r is None else (float(r["Open"]) + float(r["Close"])) / 2

        pm_bar = bars.get((8, 0))
        po_bar = bars.get((9, 30))
        pc_bar = bars.get((15, 30))
        if po_bar is None or pc_bar is None:
            continue
        if sens:
            pm = None if pm_bar is None else float(pm_bar["Close"])
            po = float(po_bar["Open"])
        else:
            pm = mid((8, 0))
            po = mid((9, 30))
        out.append({"date": d.isoformat(), "pm": pm, "po": po, "md": mid((11, 30)),
                    "pc": float(pc_bar["Close"])})
    return out


def simulate(days: list[dict], ticker: str, policy: str) -> tuple[list[dict], list[dict]]:
    """返回 (trades, events). trade: entry_date, pnl (归一化收益, 1.0 仓位); event: 盘前止盈事件."""
    s = lev_sqrt(ticker)
    levels = [(t * s, f, lab) for t, f, lab in TP_LEVELS]
    trail = TRAIL * s
    trades, events = [], []
    i = BREAKOUT_N
    while i < len(days):
        prior = max(x["pc"] for x in days[i - BREAKOUT_N:i])
        if days[i]["pc"] < prior:
            i += 1
            continue
        entry = days[i]["pc"]
        remaining, proceeds, high = 1.0, 0.0, entry
        hit, deferred = set(), None
        j, closed = i + 1, False
        while j < len(days) and j <= i + HOLD_DAYS and not closed:
            d = days[j]
            for win in ("pm", "po", "md", "pc"):
                px = d[win]
                if px is None or remaining <= 1e-9:
                    continue
                high = max(high, px)
                if px <= high * (1 - trail):          # 移动止损: 两种策略相同, 立即
                    proceeds += remaining * px
                    remaining = 0.0
                    closed = True
                    break
                gain = px / entry - 1
                # B: 处理盘前延迟下来的止盈
                if policy == "B" and deferred and win == "po":
                    lab, thr, ev = deferred
                    deferred = None
                    if gain >= thr and lab not in hit:
                        frac = next(f for t, f, l in levels if l == lab)
                        q = min(frac, remaining)
                        proceeds += q * px
                        remaining -= q
                        hit.add(lab)
                        ev.update(b_price=px, b_filled=True)
                    else:
                        ev.update(b_price=px, b_filled=False)
                for thr, frac, lab in levels:
                    if gain >= thr and lab not in hit:
                        if win == "pm":
                            ev = {"ticker": ticker, "date": d["date"], "label": lab, "a_price": px,
                                  "entry": entry}
                            events.append(ev)
                            if policy == "B":
                                if not deferred:
                                    deferred = (lab, thr, ev)
                                break
                        q = min(frac, remaining)
                        proceeds += q * px
                        remaining -= q
                        hit.add(lab)
            j += 1
        if remaining > 1e-9:                          # 到期或数据结束: 按最后 pre-close 平
            last = days[min(j - 1, len(days) - 1)]["pc"]
            proceeds += remaining * last
        trades.append({"ticker": ticker, "entry_date": days[i]["date"],
                       "pnl": proceeds / entry - 1.0})
        i = j                                         # 平仓后才能再入场
    return trades, events


def period_of(date: str, split: str = "2025-12-31") -> str:
    return "train" if date <= split else "oos"


def summarize(trA, trB, evB) -> dict:
    out = {}
    for per in ("train", "oos"):
        a = [t["pnl"] for t in trA if period_of(t["entry_date"]) == per]
        b = [t["pnl"] for t in trB if period_of(t["entry_date"]) == per]
        ev = [e for e in evB if period_of(e["date"]) == per and "b_price" in e]
        filled = [e for e in ev if e.get("b_filled")]
        deltas = [(e["b_price"] / e["a_price"] - 1) * 100 for e in filled]
        out[per] = {
            "n_trades": len(a),
            "n_events": len(ev),
            "n_b_filled": len(filled),
            "n_b_canceled": len(ev) - len(filled),
            "avg_delta_pp": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "median_delta_pp": round(sorted(deltas)[len(deltas) // 2], 4) if deltas else None,
            "total_a_pct": round(sum(a) * 100, 3),
            "total_b_pct": round(sum(b) * 100, 3),
        }
    return out


def judge(s: dict) -> tuple[str, str]:
    why = []
    inconclusive = False
    for per in ("train", "oos"):
        x = s[per]
        if x["n_events"] < PREREG["min_events_per_period"]:
            inconclusive = True
            why.append(f"{per}: 事件 {x['n_events']} < {PREREG['min_events_per_period']}")
            continue
        if x["avg_delta_pp"] is None or x["avg_delta_pp"] < PREREG["fill_delta_min_pp"]:
            why.append(f"{per}: avg_delta {x['avg_delta_pp']} < 0")
        floor = x["total_a_pct"] - PREREG["total_tolerance"] * abs(x["total_a_pct"])
        if x["total_b_pct"] < floor:
            why.append(f"{per}: total_B {x['total_b_pct']} < {floor:.3f}")
    if inconclusive:
        return "inconclusive", "; ".join(why)
    return ("pass", "train & OOS 均通过") if not why else ("reject", "; ".join(why))


def run() -> int:
    from backtest_verdicts import write_verdict
    write_verdict(NAME, "inconclusive", conclusion="预注册: 参数和通过标准跑前写死, 等待结果",
                  params=PREREG, should_integrate=False,
                  recommendation="pre-registration placeholder")
    tickers = universe()
    print(f"universe ({len(tickers)}): {tickers}")
    res = {}
    for sens in (False, True):
        trA, trB, evB, span = [], [], [], []
        for t in tickers:
            try:
                df = fetch(t)
            except Exception as e:
                print(f"  {t}: fetch failed {e}")
                continue
            if df is None:
                print(f"  {t}: no data")
                continue
            days = daily_points(df, sens=sens)
            if len(days) <= BREAKOUT_N + 1:
                continue
            span += [days[0]["date"], days[-1]["date"]]
            a, _ = simulate(days, t, "A")
            b, ev = simulate(days, t, "B")
            trA += a; trB += b; evB += ev
        s = summarize(trA, trB, evB)
        s["data_span"] = [min(span), max(span)] if span else None
        res["sensitivity" if sens else "primary"] = s
        res.setdefault("events_primary", evB) if not sens else None
    verdict, why = judge(res["primary"])
    sens_v, sens_why = judge(res["sensitivity"])
    print(json.dumps({k: v for k, v in res.items() if k != "events_primary"}, ensure_ascii=False, indent=1))
    print(f"【VERDICT】 {verdict} — {why}")
    print(f"(敏感性, 不参与判定: {sens_v} — {sens_why})")
    write_verdict(NAME, verdict, conclusion=why,
                  metrics={"primary": res["primary"], "sensitivity": res["sensitivity"],
                           "sensitivity_verdict": f"{sens_v}: {sens_why}"},
                  params=PREREG, should_integrate=(verdict == "pass"),
                  recommendation=("可以重新提交 EVO-3 上线" if verdict == "pass"
                                  else "维持现制度 (盘前命中立即止盈)"))
    out = HERE.parent / "development" / datetime.now(timezone.utc).strftime("%Y-%m-%d") / "premarket_tp_long"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                     encoding="utf-8")
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
