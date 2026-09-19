"""_reconcile_cohorts.py — 只读对账 trade_log vs execution_ledger.

F04 deep fix (audit 2026-09-19): 之前 cohort_tracker 在 _log_trade (submit 时)
fire, 未成交/撤单也污染 cohort. 已 forward-only 修复 (paper_trader commit
7b9d44235), 但历史 cohort 仍是 stale. Audit 要求:
    "对旧 trade/cohort 与券商历史做只读对账；证据不足的历史标为
     legacy_unreconciled. 先生成修复预览/备份, 再进行显式数据迁移, 绝不删原始账本."

本脚本:
    1. 扫 trade_log.jsonl (每条 = 一次 submit)
    2. 扫 execution_ledger.jsonl (每条 = submit/partial/filled/cancelled 事件)
    3. 用 order_id 关联; 无 fill 事件的 submit 标 legacy_unreconciled=True
    4. 写 signals/cohort_reconciliation_report.jsonl (只读报告)
    5. 打印摘要: N reconciled / M unreconciled / X orphan (fill 无 submit)
    6. 不动 trade_log / cohort ledger — 如要 rebuild cohort 用 --rebuild-dry (未实现)

CLI: python _reconcile_cohorts.py [--print-details] [--min-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TRADE_LOG = SCRIPT_DIR / "signals" / "trade_log.jsonl"
EXEC_LEDGER = SCRIPT_DIR / "signals" / "execution_ledger.jsonl"
REPORT_PATH = SCRIPT_DIR / "signals" / "cohort_reconciliation_report.jsonl"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _index_fill_events(ledger: list[dict]) -> dict[str, dict]:
    """按 order_id 聚合最终状态. 返 {oid: {status, dealt_qty, avg_fill, ts, events}}."""
    by_oid: dict[str, dict] = defaultdict(lambda: {
        "events": [],
        "dealt_qty": 0.0,
        "avg_fill": None,
        "final_status": None,
        "final_ts": None,
    })
    for ev in ledger:
        oid = str(ev.get("order_id") or "")
        if not oid:
            continue
        event = ev.get("event", "")
        by_oid[oid]["events"].append(event)
        if event in ("filled", "partial"):
            by_oid[oid]["dealt_qty"] = float(ev.get("dealt_qty", 0) or 0)
            avg = ev.get("average_fill_price")
            if avg is not None:
                by_oid[oid]["avg_fill"] = float(avg)
            by_oid[oid]["final_status"] = event
            by_oid[oid]["final_ts"] = ev.get("ts")
        elif event == "cancelled":
            by_oid[oid]["final_status"] = "cancelled"
            by_oid[oid]["final_ts"] = ev.get("ts")
        elif event == "submitted" and not by_oid[oid]["final_status"]:
            by_oid[oid]["final_status"] = "submitted_only"
            by_oid[oid]["final_ts"] = ev.get("ts")
    return dict(by_oid)


def reconcile(trade_log: list[dict], fills_by_oid: dict[str, dict],
               min_date: str | None = None) -> dict:
    """Match trades → fill events. 返统计 + per-trade 状态列表.

    trade 状态:
      · reconciled_filled: 有 fill 事件, dealt == requested (或几乎)
      · reconciled_partial: fill 事件里 dealt < requested (成交不足)
      · reconciled_cancelled: 有 cancelled 事件
      · legacy_unreconciled: 无 order_id 或 order_id 不在 ledger (通常是 DRY 或早于 ledger)
      · dry_run: 明确 DRY_RUN=true 或 order_id="DRY"
    """
    per_trade = []
    for t in trade_log:
        ts = t.get("ts", "")
        if min_date and ts < min_date:
            continue
        oid = str(t.get("order_id") or "")
        ticker = t.get("ticker", "")
        side = t.get("side", "")
        qty = int(t.get("qty", 0) or 0)
        price = float(t.get("price", 0) or 0)
        # DRY 明确标注
        if oid == "DRY" or t.get("dry_run") is True:
            per_trade.append({
                "ts": ts, "ticker": ticker, "side": side, "qty": qty,
                "price": price, "order_id": oid,
                "reconcile_status": "dry_run",
                "note": "DRY_RUN, no broker interaction expected",
            })
            continue
        # LIVE: 找 fill 事件
        fill = fills_by_oid.get(oid)
        if not fill:
            per_trade.append({
                "ts": ts, "ticker": ticker, "side": side, "qty": qty,
                "price": price, "order_id": oid,
                "reconcile_status": "legacy_unreconciled",
                "note": "no fill event in execution_ledger (pre-ledger trade OR ledger lost this oid)",
            })
            continue
        status = fill["final_status"]
        dealt = fill["dealt_qty"]
        avg_fill = fill["avg_fill"]
        if status == "cancelled":
            per_trade.append({
                "ts": ts, "ticker": ticker, "side": side, "qty": qty,
                "price": price, "order_id": oid,
                "reconcile_status": "reconciled_cancelled",
                "dealt_qty": dealt, "avg_fill": avg_fill,
                "note": "phantom cohort risk if pre-fix",
            })
        elif status == "submitted_only":
            per_trade.append({
                "ts": ts, "ticker": ticker, "side": side, "qty": qty,
                "price": price, "order_id": oid,
                "reconcile_status": "submitted_only",
                "note": "submitted event exists but no fill/cancel — order still open OR reconcile stopped",
            })
        elif status in ("filled", "partial") and dealt > 0:
            fill_pct = (dealt / qty * 100) if qty else None
            per_trade.append({
                "ts": ts, "ticker": ticker, "side": side, "qty": qty,
                "price": price, "order_id": oid,
                "reconcile_status": f"reconciled_{status}",
                "dealt_qty": dealt, "avg_fill": avg_fill,
                "fill_pct": round(fill_pct, 1) if fill_pct else None,
                "price_diff_bps": round((avg_fill - price) / price * 10000, 1) if avg_fill and price else None,
            })
        else:
            per_trade.append({
                "ts": ts, "ticker": ticker, "side": side, "qty": qty,
                "price": price, "order_id": oid,
                "reconcile_status": "unknown",
                "final_status": status,
            })

    # Aggregate
    counts = defaultdict(int)
    for p in per_trade:
        counts[p["reconcile_status"]] += 1
    return {
        "total_trades":  len(per_trade),
        "counts":        dict(counts),
        "per_trade":     per_trade,
    }


def _write_report(report: dict, path: Path = REPORT_PATH) -> Path:
    """写只读报告. Append mode 保留每次运行历史."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "type": "cohort_reconciliation_summary",
        "total_trades": report["total_trades"],
        "counts": report["counts"],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        for p in report["per_trade"]:
            f.write(json.dumps({"type": "trade_detail", **p}, ensure_ascii=False) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--print-details", action="store_true",
                    help="Print per-trade status to stdout")
    ap.add_argument("--min-date", default=None,
                    help="Only include trades with ts >= YYYY-MM-DD")
    ap.add_argument("--no-write", action="store_true",
                    help="Do not write report file (stdout only)")
    args = ap.parse_args()

    trades = _load_jsonl(TRADE_LOG)
    ledger = _load_jsonl(EXEC_LEDGER)
    print(f"[reconcile] trade_log: {len(trades)} entries")
    print(f"[reconcile] execution_ledger: {len(ledger)} events")

    fills_by_oid = _index_fill_events(ledger)
    print(f"[reconcile] unique order_ids in ledger: {len(fills_by_oid)}")

    report = reconcile(trades, fills_by_oid, min_date=args.min_date)

    print(f"\n=== Reconciliation summary ===")
    print(f"Total trades: {report['total_trades']}")
    for status, n in sorted(report["counts"].items(), key=lambda x: -x[1]):
        print(f"  {status:26s}: {n}")

    unreconciled_pct = (report["counts"].get("legacy_unreconciled", 0)
                        / max(1, report["total_trades"]) * 100)
    if unreconciled_pct > 10:
        print(f"\n⚠ {unreconciled_pct:.0f}% trades legacy_unreconciled — cohort 统计不能视作权威")

    if args.print_details:
        print(f"\n=== Per-trade detail ===")
        for p in report["per_trade"]:
            print(f"  {p['ts'][:19]} {p['ticker']:10s} {p['side']:5s} "
                  f"qty={p['qty']:>5} oid={p.get('order_id', ''):>8} → {p['reconcile_status']}")

    if not args.no_write:
        path = _write_report(report)
        print(f"\n[report] appended {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
