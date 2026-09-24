"""只读对账: moomoo OpenD 模拟账户历史订单 + 当前持仓 vs 本地 execution_ledger.

安全边界:
- 只调用 history_order_list_query / position_list_query (白名单代理, 其他方法直接报错).
- 不下单、不改单、不撤单、不解锁交易.
- 不修改 execution_ledger / cohort / state. 结果只写到 development/<日期>/broker_history/.
- 输出文件里不写账户 ID.

说明: 模拟账户不支持 history_deal_list_query (历史成交), 因此用历史订单的
dealt_qty / dealt_avg_price (订单累计成交) 作为成交事实.

用法 (Windows, OpenD 运行中):
  python agents\\_broker_history_reconcile.py                      # 默认 6 只 unreconciled 标的, 近 400 天
  python agents\\_broker_history_reconcile.py --start 2025-01-01 --tickers ALL
  python agents\\_broker_history_reconcile.py --selftest            # 无 OpenD, 内置样例自检
  python agents\\_broker_history_reconcile.py --import-baseline <broker_fills_*.jsonl>
                                                   # 写券商历史基线 (不连 OpenD)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

AGENTS = Path(__file__).resolve().parent
ROOT = AGENTS.parent
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

DEFAULT_TICKERS = ["US.DRAM", "US.MSFT", "US.QRVO", "US.SOXL", "US.TSLA", "US.USO"]
CHUNK_DAYS = 30          # 每次查询的日期跨度
CALL_INTERVAL_S = 3.2    # history_order_list_query 频率限制 (约 10 次 / 30 秒)
ALLOWED = {"history_order_list_query", "position_list_query", "close"}


# ---------- 时间: 美股回报时间 (美东) → UTC ----------
def _us_eastern_offset(naive: datetime) -> timedelta:
    try:
        from zoneinfo import ZoneInfo
        return naive.replace(tzinfo=ZoneInfo("America/New_York")).utcoffset()
    except Exception:
        # 无 tzdata 时的美国夏令时规则: 3 月第二个周日 2:00 ~ 11 月第一个周日 2:00
        y = naive.year
        mar = datetime(y, 3, 8) + timedelta(days=(6 - datetime(y, 3, 8).weekday()) % 7)
        nov = datetime(y, 11, 1) + timedelta(days=(6 - datetime(y, 11, 1).weekday()) % 7)
        dst = mar.replace(hour=2) <= naive < nov.replace(hour=2)
        return timedelta(hours=-4 if dst else -5)


def broker_time_to_utc_iso(s: str) -> str:
    s = str(s or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return ""
    return (naive - _us_eastern_offset(naive)).replace(tzinfo=timezone.utc).isoformat()


# ---------- 只读代理 ----------
class ReadOnlyCtx:
    def __init__(self, ctx):
        self._ctx = ctx

    def __getattr__(self, name):
        if name not in ALLOWED:
            raise PermissionError(f"read-only reconcile: {name} is not allowed")
        return getattr(self._ctx, name)


def open_ctx():
    from moomoo import OpenSecTradeContext, TrdMarket, SecurityFirm
    from config import OPEND_HOST, OPEND_PORT
    return ReadOnlyCtx(OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                                           host=OPEND_HOST, port=OPEND_PORT,
                                           security_firm=SecurityFirm.FUTUSECURITIES))


def fetch_orders(ctx, trd_env, acc_id, start: date, end: date, sleep=time.sleep):
    import pandas as pd
    frames, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        ret, df = ctx.history_order_list_query(
            trd_env=trd_env, acc_id=acc_id,
            start=f"{cur} 00:00:00", end=f"{stop} 23:59:59")
        if ret != 0:
            raise RuntimeError(f"history_order_list_query {cur}~{stop} failed: {df}")
        if df is not None and len(df):
            frames.append(df)
        print(f"  orders {cur} ~ {stop}: {0 if df is None else len(df)} rows")
        cur = stop + timedelta(days=1)
        if cur <= end:
            sleep(CALL_INTERVAL_S)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["order_id"] = out["order_id"].astype(str)
    return out.drop_duplicates("order_id", keep="last")


def fetch_positions(ctx, trd_env, acc_id):
    ret, df = ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
    if ret != 0:
        raise RuntimeError(f"position_list_query failed: {df}")
    return df


# ---------- 归一化 ----------
_SIDE = {"BUY": "BUY", "BUY_BACK": "BUY", "SELL": "SELL", "SELL_SHORT": "SELL"}


def broker_fill_events(orders) -> list[dict]:
    out = []
    if orders is None or not len(orders):
        return out
    for _, r in orders.iterrows():
        dealt = float(r.get("dealt_qty") or 0)
        avg = float(r.get("dealt_avg_price") or 0)
        if dealt <= 0 or avg <= 0:
            continue
        raw_side = str(r.get("trd_side") or "").upper()
        out.append({
            "event": "filled", "source": "broker_history",
            "order_id": str(r.get("order_id")), "ticker": str(r.get("code")),
            "side": _SIDE.get(raw_side, raw_side), "broker_side": raw_side,
            "broker_status": str(r.get("order_status") or ""),
            "requested_qty": float(r.get("qty") or 0),
            "dealt_qty": dealt, "average_fill_price": avg,
            "ts": broker_time_to_utc_iso(r.get("updated_time") or r.get("create_time")),
            "create_time_broker": str(r.get("create_time") or ""),
        })
    return out


def positions_by_ticker(pos) -> dict:
    out = {}
    if pos is None or not len(pos):
        return out
    for _, r in pos.iterrows():
        q = float(r.get("qty") or 0)
        if q == 0:
            continue
        cost = r.get("average_cost", r.get("cost_price"))
        out[str(r.get("code"))] = {"qty": q, "broker_avg_cost": float(cost) if cost is not None else None}
    return out


# ---------- 对账 ----------
def reconcile(local_events, broker_events, broker_pos, tickers):
    import fill_ledger as fl
    import cohort_tracker as ct
    canon = fl.canonical_ticker
    local_fills = [e for e in local_events if e.get("event") in ("filled", "partial")]
    all_t = sorted({canon(e["ticker"]) for e in broker_events} | {canon(e.get("ticker")) for e in local_fills})
    targets = all_t if tickers == "ALL" else [canon(t) for t in tickers]

    # 本地每订单最终累计
    local_final = {}
    for e in sorted(local_fills, key=lambda x: x.get("ts", "")):
        local_final[str(e.get("order_id"))] = e
    broker_by_oid = {e["order_id"]: e for e in broker_events}

    broker_only = [e for e in broker_events if e["order_id"] not in local_final]
    combined = local_fills + broker_only

    orig = fl._load_ledger
    per = {}
    try:
        for t in targets:
            fl._load_ledger = lambda path=None: local_fills
            before = fl.get_position(t)
            fl._load_ledger = lambda path=None: combined
            after = fl.get_position(t)
            b_only = [e for e in broker_only if canon(e["ticker"]) == t]
            mism = []
            for oid, le in local_final.items():
                if canon(le.get("ticker")) != t or oid not in broker_by_oid:
                    continue
                be = broker_by_oid[oid]
                if abs(float(le["dealt_qty"]) - be["dealt_qty"]) > 1e-6 or \
                   abs(float(le["average_fill_price"]) - be["average_fill_price"]) > 0.005:
                    mism.append({"order_id": oid,
                                 "local": [le["dealt_qty"], le["average_fill_price"]],
                                 "broker": [be["dealt_qty"], be["average_fill_price"]]})
            local_missing_at_broker = [oid for oid, le in local_final.items()
                                       if canon(le.get("ticker")) == t and oid not in broker_by_oid]
            bq = broker_pos.get(t, {}).get("qty", 0.0)
            per[t] = {
                "broker_only_orders": len(b_only),
                "broker_only_buy_qty": sum(e["dealt_qty"] for e in b_only if e["side"] == "BUY"),
                "broker_only_sell_qty": sum(e["dealt_qty"] for e in b_only if e["side"] == "SELL"),
                "earliest_broker_fill": min((e["ts"] for e in b_only), default=None),
                "short_side_orders": sum(1 for e in b_only if e["broker_side"] in ("SELL_SHORT", "BUY_BACK")),
                "before": {k: before[k] for k in ("qty", "avg_cost", "realized_pnl", "unreconciled_sells")},
                "after": {k: after[k] for k in ("qty", "avg_cost", "realized_pnl", "unreconciled_sells")},
                "broker_position_qty": bq,
                "broker_avg_cost": broker_pos.get(t, {}).get("broker_avg_cost"),
                "qty_matches_broker": abs(float(after["qty"] or 0) - bq) < 1e-6,
                "fill_mismatches": mism,
                "local_orders_missing_at_broker": local_missing_at_broker,
            }
        fl._load_ledger = lambda path=None: local_fills
        stats_before = {d: ct.stats_from_fills(d) for d in (30, 60)}
        fl._load_ledger = lambda path=None: combined
        stats_after = {d: ct.stats_from_fills(d) for d in (30, 60)}
    finally:
        fl._load_ledger = orig
    keep = ("n_roundtrips", "n_winners", "n_losers", "total_pnl_usd", "authority",
            "unreconciled_tickers", "unreconciled_sells_qty")
    return {
        "targets": targets, "per_ticker": per,
        "stats_local_only": {d: {k: s.get(k) for k in keep} for d, s in stats_before.items()},
        "stats_with_broker_history": {d: {k: s.get(k) for k in keep} for d, s in stats_after.items()},
        "n_broker_fill_orders": len(broker_events), "n_broker_only_orders": len(broker_only),
    }


def render_md(rep, meta) -> str:
    L = ["# 券商历史订单对账 (只读)", "",
         f"生成: {meta['generated_at']} · 查询区间 {meta['start']} ~ {meta['end']} · 模拟账户",
         f"券商有成交订单 {rep['n_broker_fill_orders']} 笔, 其中本地账本没有的 {rep['n_broker_only_orders']} 笔.",
         "", "## 各标的", "",
         "| 标的 | 本地缺的券商订单 | 补回买入股 | 对账前 未配对卖出 | 对账后 未配对卖出 | 推算持仓 | 券商持仓 | 一致 |",
         "|---|---:|---:|---:|---:|---:|---:|---|"]
    for t, p in rep["per_ticker"].items():
        L.append(f"| {t} | {p['broker_only_orders']} | {p['broker_only_buy_qty']:g} | "
                 f"{p['before']['unreconciled_sells']} | {p['after']['unreconciled_sells']} | "
                 f"{p['after']['qty']} | {p['broker_position_qty']:g} | {'是' if p['qty_matches_broker'] else '否'} |")
    L += ["", "## 统计对比 (近 30 / 60 天)", "", "| | 往返 | 胜/负 | 已实现 P&L | 状态 | 未配对卖出股 |", "|---|---:|---|---:|---|---:|"]
    for label, key in (("仅本地", "stats_local_only"), ("加券商历史", "stats_with_broker_history")):
        for d, s in rep[key].items():
            L.append(f"| {label} {d}天 | {s['n_roundtrips']} | {s['n_winners']}/{s['n_losers']} | "
                     f"{s['total_pnl_usd']:+,.2f} | {s['authority']} | {s['unreconciled_sells_qty']} |")
    issues = [(t, p) for t, p in rep["per_ticker"].items()
              if p["fill_mismatches"] or p["local_orders_missing_at_broker"] or p["short_side_orders"]]
    if issues:
        L += ["", "## 需注意", ""]
        for t, p in issues:
            if p["fill_mismatches"]:
                L.append(f"- {t}: {len(p['fill_mismatches'])} 笔订单本地与券商成交量/均价不一致")
            if p["local_orders_missing_at_broker"]:
                L.append(f"- {t}: 本地有、券商历史查不到的订单 {p['local_orders_missing_at_broker']}")
            if p["short_side_orders"]:
                L.append(f"- {t}: 含卖空/回补订单 {p['short_side_orders']} 笔")
    L += ["", "说明: 这是只读诊断, 没有写入 execution_ledger. 未包含费用. 若 '一致' 为否, "
          "说明查询起点之前仍有持仓来源 (可用 --start 往前查)."]
    return "\n".join(L) + "\n"


def run(args, ctx=None, sleep=time.sleep):
    import fill_ledger as fl
    from moomoo import TrdEnv
    from config import MOOMOO_ACC_ID
    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=400)
    tickers = "ALL" if args.tickers.upper() == "ALL" else [t.strip() for t in args.tickers.split(",") if t.strip()]
    own = ctx is None
    ctx = ctx or open_ctx()
    try:
        print(f"[reconcile] 只读查询 {start} ~ {end}")
        orders = fetch_orders(ctx, TrdEnv.SIMULATE, MOOMOO_ACC_ID, start, end, sleep=sleep)
        pos = fetch_positions(ctx, TrdEnv.SIMULATE, MOOMOO_ACC_ID)
    finally:
        if own:
            try: ctx.close()
            except Exception: pass
    broker_events = broker_fill_events(orders)
    rep = reconcile(fl._load_ledger(include_broker_history=False), broker_events,
                    positions_by_ticker(pos), tickers)
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "start": str(start), "end": str(end)}
    out_dir = Path(args.out) if args.out else ROOT / "development" / date.today().isoformat() / "broker_history"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    (out_dir / f"broker_fills_{stamp}.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in broker_events), encoding="utf-8")
    (out_dir / f"reconcile_{stamp}.json").write_text(
        json.dumps({"meta": meta, **rep}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = render_md(rep, meta)
    (out_dir / f"reconcile_{stamp}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"[reconcile] 输出: {out_dir}")
    return rep


def _system_order_ids(logs_dir: Path) -> set[str]:
    """系统下单日志里出现过的订单号: '[trader...] BUY/SELL ... order=<id>'."""
    import re
    pat = re.compile(r"\[trader[^\]]*\]\s*(?:BUY|SELL)\b.*?order=(\d+)")
    ids: set[str] = set()
    for f in sorted(Path(logs_dir).glob("*.log")):
        try:
            with open(f, encoding="utf-8", errors="replace") as h:
                for line in h:
                    if "order=" in line:
                        m = pat.search(line)
                        if m:
                            ids.add(m.group(1))
        except OSError:
            continue
    return ids


def import_baseline(src: Path, out: Path, exec_path: Path | None = None,
                    logs_dir: Path | None = None) -> int:
    """把券商导出 (broker_fills_*.jsonl) 中本地账本缺失的成交写成基线文件.

    不连 OpenD; 不改 execution_ledger. origin: 系统日志/本地账本有该订单 → system,
    否则 manual. 返回写入行数. 目标文件整体重写 (可重复执行).
    """
    import fill_ledger as fl
    exec_path = Path(exec_path or fl.EXEC_LEDGER_PATH)
    logs_dir = Path(logs_dir or AGENTS / "logs")
    local = {str(r.get("order_id")) for r in fl._read_jsonl(exec_path)}
    sys_ids = _system_order_ids(logs_dir) | local
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for r in fl._read_jsonl(Path(src)):
        oid = str(r.get("order_id"))
        if r.get("event") != "filled" or oid in local:
            continue
        rows.append({**r, "source": "broker_history",
                     "origin": "system" if oid in sys_ids else "manual",
                     "imported_at": now, "import_file": Path(src).name})
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(out)
    return len(rows)


def selftest():
    """无 OpenD: 伪造券商返回, 验证对账逻辑 (本地缺 SOXL 期初买入)."""
    import tempfile
    import pandas as pd
    from unittest.mock import patch
    import fill_ledger as fl
    local = [
        {"event": "submitted", "order_id": "S1", "ticker": "US.SOXL", "side": "SELL"},
        {"event": "filled", "order_id": "S1", "ticker": "US.SOXL", "side": "SELL",
         "dealt_qty": 100.0, "average_fill_price": 130.0, "ts": "2026-08-18T15:27:10+00:00"},
    ]
    orders = pd.DataFrame([
        dict(order_id="B0", code="US.SOXL", trd_side="BUY", qty=150, dealt_qty=150,
             dealt_avg_price=120.0, order_status="FILLED_ALL",
             create_time="2026-08-01 09:31:00", updated_time="2026-08-01 09:31:05.000"),
        dict(order_id="S1", code="US.SOXL", trd_side="SELL", qty=100, dealt_qty=100,
             dealt_avg_price=130.0, order_status="FILLED_ALL",
             create_time="2026-08-18 11:27:00", updated_time="2026-08-18 11:27:10.000"),
    ])
    pos = pd.DataFrame([dict(code="US.SOXL", qty=50, average_cost=120.0)])

    class FakeCtx:
        def history_order_list_query(self, **kw):
            s, e = kw["start"][:10], kw["end"][:10]
            d = orders[orders.updated_time.str[:10].between(s, e)]
            return 0, d.copy()
        def position_list_query(self, **kw):
            return 0, pos.copy()
        def place_order(self, *a, **k):
            raise AssertionError("must never be reachable")

    ctx = ReadOnlyCtx(FakeCtx())
    try:
        ctx.place_order
        raise AssertionError("read-only guard failed")
    except PermissionError:
        pass
    assert broker_time_to_utc_iso("2026-08-01 09:31:05.000") == "2026-08-01T13:31:05+00:00"
    assert broker_time_to_utc_iso("2026-01-05 09:30:00") == "2026-01-05T14:30:00+00:00"
    with tempfile.TemporaryDirectory() as td, patch.object(fl, "_load_ledger", lambda path=None, **kw: local):
        a = argparse.Namespace(start="2026-07-20", end="2026-08-31", tickers="US.SOXL", out=td)
        rep = run(a, ctx=ctx, sleep=lambda s: None)
    p = rep["per_ticker"]["US.SOXL"]
    assert p["before"]["unreconciled_sells"] == 100, p
    assert p["after"]["unreconciled_sells"] == 0, p
    assert p["after"]["realized_pnl"] == 1000.0, p
    assert p["after"]["qty"] == 50 and p["qty_matches_broker"], p
    assert rep["n_broker_only_orders"] == 1
    print("SELFTEST OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--import-baseline", metavar="BROKER_FILLS_JSONL",
                    help="不连 OpenD: 把已导出的券商成交写入 signals/broker_history_fills.jsonl")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    if args.selftest:
        selftest()
    elif args.import_baseline:
        import fill_ledger as fl
        n = import_baseline(Path(args.import_baseline), fl.BROKER_HISTORY_PATH)
        print(f"[baseline] wrote {n} rows → {fl.BROKER_HISTORY_PATH}")
    else:
        run(args)


if __name__ == "__main__":
    main()
