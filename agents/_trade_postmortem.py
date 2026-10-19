"""
_trade_postmortem.py
────────────────────
读 signals/trade_log.jsonl，把 BUY/SELL 配对，算每笔交易的：
  · 持仓时长
  · 实现 P&L (%)
  · 触发原因（决策 reason / 退出原因 tag）
  · 触发时市场状态 (regime / RSI / CCI / trend)

输出归因报告：哪类信号最赚，哪类最亏。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

LOG_PATH = Path(__file__).parent / "signals" / "trade_log.jsonl"
REPORT_PATH = Path(__file__).parent / "signals" / "trade_postmortem.md"
POSTMORTEM_ARCHIVE = Path(__file__).parent / "signals" / "postmortem" / "archive.jsonl"


def load_news_structured_index(path: Path | None = None) -> dict:
    """EVO-20261014-2: 按 order_id 索引 postmortem 归档里的 news_structured.

    只读 trade_postmortem.py 写出的 archive.jsonl, 决策链路零接触 —
    这里只是把 '那笔强制卖出有没有事件/新闻背书' 的结构化证据挂到回测报告上.
    """
    p = Path(path or POSTMORTEM_ARCHIVE)
    out = {}
    if not p.exists():
        return out
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ns = rec.get("news_structured") or {}
        oid = str(rec.get("order_id") or "")
        if not oid:
            continue
        out[oid] = ns
    return out


def load_log() -> list:
    if not LOG_PATH.exists():
        return []
    out = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _extract_prices(ev: dict) -> dict:
    """EVO-20261013-2: 从 trade_log 条目抽三端价差字段 (老记录全 None 兼容)."""
    return {
        "ref_price":       ev.get("ref_price"),
        "live_quote":      ev.get("live_quote"),
        "trigger_price":   ev.get("trigger_price") or ev.get("price"),
        "divergence_pct":  ev.get("divergence_pct"),
        "divergence_flag": ev.get("divergence_flag"),
        "quote_source":    ev.get("quote_source"),
        "staleness_sec":   ev.get("staleness_sec"),
    }


def pair_trades(events: list) -> list[dict]:
    """把同 ticker 的 BUY -> SELL 配对（FIFO）。
    PYRAMID 算追加仓位（加权平均 entry_price），TRAILING/TP/SELL 都算退出事件。

    EVO-20261013-2: 为每笔配对交易展开 entry/exit 两端的 price_snapshot
    (ref_price / live_quote / trigger_price / divergence_pct / divergence_flag),
    让 report() 能直接按价差分桶归因."""
    open_positions = defaultdict(list)   # ticker -> [{qty, price, ts, decision, mkt}]
    closed_trades = []

    for ev in events:
        if ev.get("dry_run"):
            continue   # 只复盘真实成交（也可放开看 dry）
        ticker = ev["ticker"]
        side = ev["side"]
        qty = ev["qty"]
        price = ev["price"]
        ts = ev["ts"]
        if side == "BUY":
            open_positions[ticker].append({
                "qty": qty, "price": price, "ts": ts,
                "decision": ev.get("decision", {}), "mkt": ev.get("market", {}),
                "tag": ev.get("tag", ""),
                "order_id": str(ev.get("order_id") or ""),
                "price_snapshot": _extract_prices(ev),
            })
        elif side == "SELL":
            remaining = qty
            while remaining > 0 and open_positions[ticker]:
                pos = open_positions[ticker][0]
                close_qty = min(remaining, pos["qty"])
                entry_dt = datetime.fromisoformat(pos["ts"].replace("Z", "+00:00"))
                exit_dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hold_days = (exit_dt - entry_dt).total_seconds() / 86400
                pnl_pct = (price - pos["price"]) / pos["price"] * 100
                entry_prices = pos.get("price_snapshot") or {}
                exit_prices  = _extract_prices(ev)
                closed_trades.append({
                    "ticker":     ticker,
                    "entry_ts":   pos["ts"],
                    "exit_ts":    ts,
                    "entry_order_id": pos.get("order_id", ""),
                    "exit_order_id":  str(ev.get("order_id") or ""),
                    "hold_days":  round(hold_days, 1),
                    "entry_price": pos["price"],
                    "exit_price": price,
                    "qty":        close_qty,
                    "pnl_pct":    round(pnl_pct, 2),
                    "pnl_usd":    round((price - pos["price"]) * close_qty, 2),
                    "entry_reason": pos["decision"].get("reason", ""),
                    "entry_action": pos["decision"].get("action"),
                    "entry_conf":   pos["decision"].get("confidence"),
                    "entry_regime": pos["decision"].get("regime"),
                    "exit_tag":     ev.get("tag", ""),
                    "entry_rsi":    pos["mkt"].get("rsi"),
                    "entry_cci":    pos["mkt"].get("cci"),
                    "entry_trend":  pos["mkt"].get("trend"),
                    # EVO-20261013-2 三端价差: 老记录字段为 None, 不影响归因主流程
                    "entry_ref_price":       entry_prices.get("ref_price"),
                    "entry_live_quote":      entry_prices.get("live_quote"),
                    "entry_trigger_price":   entry_prices.get("trigger_price"),
                    "entry_divergence_pct":  entry_prices.get("divergence_pct"),
                    "entry_divergence_flag": entry_prices.get("divergence_flag"),
                    "exit_ref_price":        exit_prices.get("ref_price"),
                    "exit_live_quote":       exit_prices.get("live_quote"),
                    "exit_trigger_price":    exit_prices.get("trigger_price"),
                    "exit_divergence_pct":   exit_prices.get("divergence_pct"),
                    "exit_divergence_flag":  exit_prices.get("divergence_flag"),
                })
                pos["qty"] -= close_qty
                if pos["qty"] <= 0:
                    open_positions[ticker].pop(0)
                remaining -= close_qty
    return closed_trades


def classify_exit(tag: str) -> str:
    tag = tag.upper()
    if "TRAILING-STOP" in tag: return "TRAILING_STOP"
    if "TAKE-PROFIT" in tag or "TP_" in tag: return "TAKE_PROFIT"
    if "STOP" in tag: return "STOP"
    if "PYRAMID" in tag: return "PYRAMID_EXIT"
    if "REBUY" in tag: return "REBUY"
    return "NORMAL"


def report(trades: list[dict]):
    if not trades:
        print("❌ trade_log.jsonl 无 BUY/SELL 配对（DRY_RUN 过滤后）。")
        print("   → 等 LIVE 模式下跑几笔后再来 / 或临时关闭 DRY 过滤")
        return

    n = len(trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    total_pnl_usd = sum(t["pnl_usd"] for t in trades)
    avg_hold = sum(t["hold_days"] for t in trades) / n
    win_rate = len(wins) / n * 100 if n else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    print(f"\n{'='*70}")
    print(f"  Trade Postmortem ({n} 笔已平仓交易)")
    print(f"{'='*70}")
    print(f"  胜率 : {win_rate:.0f}% ({len(wins)}/{n})")
    print(f"  平均赢 : +{avg_win:.2f}%   平均亏 : {avg_loss:.2f}%")
    print(f"  盈亏比 : {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "  盈亏比 : ∞")
    print(f"  总 P&L (USD): {total_pnl_usd:+,.2f}")
    print(f"  平均持仓 : {avg_hold:.1f} 天")

    # 按入场原因
    print(f"\n  ── 按入场 reason 归因 ──")
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t["entry_reason"][:50]].append(t)
    for reason, ts in sorted(by_reason.items(), key=lambda x: -sum(t["pnl_pct"] for t in x[1])):
        wr = sum(1 for t in ts if t["pnl_pct"] > 0) / len(ts) * 100
        avg = sum(t["pnl_pct"] for t in ts) / len(ts)
        print(f"    {reason or '(unknown)':<50} n={len(ts):>3}  胜{wr:>3.0f}%  avg{avg:>+6.2f}%")

    # 按退出原因
    print(f"\n  ── 按退出 tag 归因 ──")
    by_exit = defaultdict(list)
    for t in trades:
        by_exit[classify_exit(t["exit_tag"])].append(t)
    for tag, ts in sorted(by_exit.items(), key=lambda x: -sum(t["pnl_pct"] for t in x[1])):
        wr = sum(1 for t in ts if t["pnl_pct"] > 0) / len(ts) * 100
        avg = sum(t["pnl_pct"] for t in ts) / len(ts)
        print(f"    {tag:<20} n={len(ts):>3}  胜{wr:>3.0f}%  avg{avg:>+6.2f}%")

    # 按 ticker
    print(f"\n  ── 按 ticker 归因 ──")
    by_tk = defaultdict(list)
    for t in trades:
        by_tk[t["ticker"]].append(t)
    for tk, ts in sorted(by_tk.items(), key=lambda x: -sum(t["pnl_usd"] for t in x[1])):
        wr = sum(1 for t in ts if t["pnl_pct"] > 0) / len(ts) * 100
        avg = sum(t["pnl_pct"] for t in ts) / len(ts)
        usd = sum(t["pnl_usd"] for t in ts)
        print(f"    {tk:<12} n={len(ts):>3}  胜{wr:>3.0f}%  avg{avg:>+6.2f}%  ${usd:+,.0f}")

    # EVO-20261013-2: 三端价差 (入场 ref vs live) 分布 + 影响
    print(f"\n  ── 入场三端价差 (ref vs live) ──")
    with_div = [t for t in trades if t.get("entry_divergence_pct") is not None]
    if not with_div:
        print("    (老记录无 price_snapshot 字段, 影子期上线后开始累计)")
    else:
        flagged = [t for t in with_div if t.get("entry_divergence_flag") == "PRICE_DIVERGENCE"]
        n_all = len(with_div)
        rate = len(flagged) / n_all * 100 if n_all else 0.0
        print(f"    样本 n={n_all}, PRICE_DIVERGENCE={len(flagged)} ({rate:.1f}%)")
        if flagged:
            avg_div = sum(abs(t["entry_divergence_pct"]) for t in flagged) / len(flagged) * 100
            avg_pnl = sum(t["pnl_pct"] for t in flagged) / len(flagged)
            print(f"    超阈值样本 avg|divergence|={avg_div:.2f}%  avg_pnl={avg_pnl:+.2f}%")
            for t in sorted(flagged, key=lambda x: -abs(x["entry_divergence_pct"]))[:5]:
                print(f"      {t['ticker']:<10} entry {t['entry_ts'][:10]}  "
                      f"ref=${t['entry_ref_price']}  live=${t['entry_live_quote']}  "
                      f"div={t['entry_divergence_pct']*100:+.2f}%  pnl={t['pnl_pct']:+.2f}%")

    # EVO-20261014-2: 卖出结构化新闻覆盖率 (只是展示, 不参与归因 — 消息面不进决策)
    ns_idx = load_news_structured_index()
    with_sell_id = [t for t in trades if t.get("exit_order_id")]
    if ns_idx and with_sell_id:
        print(f"\n  ── 卖出 ±48h 结构化新闻覆盖 (postmortem archive) ──")
        hit = [t for t in with_sell_id
               if (ns_idx.get(t["exit_order_id"]) or {}).get("items")]
        total = len(with_sell_id)
        rate = len(hit) / total * 100 if total else 0.0
        print(f"    有新闻背书 {len(hit)}/{total} ({rate:.0f}%); 其余为 '难判断/证据不足'")
        et_count: dict = defaultdict(int)
        for t in hit:
            for it in (ns_idx.get(t["exit_order_id"]) or {}).get("items") or []:
                et_count[it.get("event_type", "NOISE")] += 1
        for et, c in sorted(et_count.items(), key=lambda x: -x[1])[:6]:
            print(f"      {et:<16} n={c}")


def main():
    events = load_log()
    if not events:
        print("❌ signals/trade_log.jsonl 不存在或为空 — 等 trade 累积后再来")
        return 1
    trades = pair_trades(events)
    report(trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
