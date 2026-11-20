"""_weekly_backtest_review.py — 自动重跑 verdict 已过期的 backtest, diff alert

流程 (weekly.bat 调):
    1. list_verdicts_needing_review() → 拿到 next_review_days 到期的 verdict
    2. 每个到期 verdict, subprocess run 对应 _backtest_*.py 脚本
    3. 新 verdict 写入后, 与老 verdict diff:
       - verdict 值变化 (pass→edge / edge→reject) → 写 signals/verdict_change_log.jsonl + notify
       - metrics 关键值变化 > threshold → 也 log
    4. 新老都保留, 归档到 signals/backtest_verdicts/archive/

映射: verdict_name → script_path (硬编码, 新加 backtest 需在此加一行)

CLI: python _weekly_backtest_review.py [--force-all]  (--force-all 无视 review_days 全跑)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from config import SIGNALS_DIR
from backtest_verdicts import (
    all_verdicts,
    list_verdicts_needing_review,
    read_verdict,
)
from atomic_io import append_jsonl

# 映射: verdict name → 可执行脚本. 新加 backtest 需在此登记.
_BACKTEST_SCRIPTS = {
    "claude_gate_health": "_backtest_claude_gate.py",
    "stop_distance":      "_backtest_stop_distance.py",
    "bvc_flow":           "_backtest_bvc_flow.py",
    "rotation_regime":    "_backtest_rotation_regime.py",
    "cta_levels":         "_backtest_cta_levels.py",
    "crack_spread_oos":   "_backtest_crack_spread_oos.py",
}

_ARCHIVE_DIR = Path(SIGNALS_DIR) / "backtest_verdicts" / "archive"
_CHANGE_LOG = Path(SIGNALS_DIR) / "verdict_change_log.jsonl"

_HERE = Path(__file__).parent
_PYTHON = sys.executable

# 仅表示可能有助于补充证据；提案状态仍需人工核实。
_GAP_EVOS = {
    "slippage_attribution": ("EVO-20261105-2", "EVO-20261115-2"),
    "orderbook_missing": ("EVO-20261112-1",),
    "signal_price_consistency": ("EVO-20261112-1", "EVO-20261115-2"),
    "data_freshness": ("EVO-20261112-2",),
}


def summarize_evidence_gaps(rows: list, *, limit: int = 30, top_n: int = 7) -> dict:
    """按卖出时间取最近 N 份成功复盘；每笔每维度最多计一次。"""
    from trade_postmortem import classify_evidence_gaps, EVIDENCE_GAP_DIMS

    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def timestamp(row):
        try:
            value = datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    valid = [r for r in rows if isinstance(r, dict) and r.get("ok") is True]
    unknown_time = sum(timestamp(r) is None for r in valid)
    valid.sort(key=lambda r: (timestamp(r) or epoch, str(r.get("order_id") or "")), reverse=True)
    selected = valid[:max(0, limit)]
    counts = {dim: 0 for dim in EVIDENCE_GAP_DIMS}
    for row in selected:
        analysis = row.get("analysis")
        limitations = analysis.get("limitations", []) if isinstance(analysis, dict) else []
        gaps = classify_evidence_gaps(limitations)
        for dim in {g["dim"] for g in gaps}:
            counts[dim] += 1

    ranking = sorted(
        ({"dim": dim, "orders": count} for dim, count in counts.items() if count),
        key=lambda x: (-x["orders"], x["dim"]),
    )
    priorities = {}
    for row in ranking:
        for evo in _GAP_EVOS.get(row["dim"], ()):
            item = priorities.setdefault(evo, {"evo_id": evo, "gap_dims": [], "orders_by_dim": {}})
            item["gap_dims"].append(row["dim"])
            item["orders_by_dim"][row["dim"]] = row["orders"]
    suggestions = sorted(
        priorities.values(),
        key=lambda x: (-max(x["orders_by_dim"].values()), x["evo_id"]),
    )
    return {
        "proposal": "EVO-20261118-2", "read_only": True,
        "selection": "成功复盘按卖出时间降序取最近 N 条；每条每维度最多计一次",
        "limit": limit, "selected": len(selected), "unknown_time": unknown_time,
        "counts": counts, "top_gaps": ranking[:max(0, top_n)],
        "suggestions": suggestions,
        "note": "关键词分类和映射仅供人工核查；提案当前状态未核实，新闻、regime 和其它缺口暂无指定映射。",
    }


def evidence_gap_report(*, archive_path: Path | None = None, limit: int = 30,
                        top_n: int = 7) -> dict:
    """只读归档，旧记录按原始 limitations 即时分类，不回写。"""
    from trade_postmortem import load_review_archive

    path = Path(archive_path) if archive_path is not None else Path(SIGNALS_DIR) / "postmortem" / "archive.jsonl"
    try:
        if not path.exists():
            raise FileNotFoundError
        result = summarize_evidence_gaps(load_review_archive(path), limit=limit, top_n=top_n)
        result["source_status"] = "ok"
    except (OSError, ValueError, TypeError) as exc:
        result = summarize_evidence_gaps([], limit=limit, top_n=top_n)
        result["source_status"] = "unavailable"
        result["error"] = type(exc).__name__
    return result


def _archive_old(name: str, old_v: dict) -> None:
    """把旧 verdict 归档 (加时间戳, 供后续 diff)."""
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = _ARCHIVE_DIR / f"{name}_{stamp}.json"
    dst.write_text(json.dumps(old_v, indent=2, ensure_ascii=False), encoding="utf-8")


def _diff_verdict(old: dict, new: dict) -> dict:
    """返 {verdict_changed, metric_changes, ...}."""
    diff = {"verdict_changed": old.get("verdict") != new.get("verdict"),
            "old_verdict": old.get("verdict"),
            "new_verdict": new.get("verdict"),
            "metric_deltas": {}}
    old_m = old.get("metrics") or {}
    new_m = new.get("metrics") or {}
    for k, v_new in new_m.items():
        v_old = old_m.get(k)
        if isinstance(v_new, (int, float)) and isinstance(v_old, (int, float)):
            delta = v_new - v_old
            if abs(delta) > 0.001:   # 忽略浮点噪声
                diff["metric_deltas"][k] = {"old": v_old, "new": v_new, "delta": round(delta, 4)}
    diff["should_integrate_changed"] = old.get("should_integrate") != new.get("should_integrate")
    return diff


def _run_backtest(name: str, script: str, timeout_sec: int = 1200) -> tuple[bool, str]:
    """跑 backtest 子进程. 返 (success, tail_output)."""
    script_path = _HERE / script
    if not script_path.exists():
        return False, f"script not found: {script_path}"
    print(f"\n{'=' * 70}")
    print(f"[rerun] {name} → {script}")
    print("=" * 70)
    try:
        proc = subprocess.run(
            [_PYTHON, "-X", "utf8", "-u", str(script_path)],
            cwd=str(_HERE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        tail = "\n".join((proc.stdout or "").splitlines()[-15:])
        if proc.returncode != 0:
            err_tail = "\n".join((proc.stderr or "").splitlines()[-5:])
            return False, f"exit={proc.returncode}\nstdout tail:\n{tail}\nstderr tail:\n{err_tail}"
        return True, tail
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as ex:
        return False, f"{ex}"


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force-all", action="store_true",
                        help="无视 next_review_days, 强制跑所有已登记 backtest")
    args = parser.parse_args()

    print("=" * 70)
    print(f"Weekly Backtest Review @ {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 70)

    # 即使没有到期回测，也输出只读证据缺口周报。
    try:
        print("\n证据缺口周报:")
        print(json.dumps(evidence_gap_report(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"证据缺口周报不可用: {type(exc).__name__}")

    if args.force_all:
        candidates = list(_BACKTEST_SCRIPTS.keys())
        print(f"[force-all] 跑所有 {len(candidates)} 个已登记 backtest")
    else:
        stale = list_verdicts_needing_review()
        candidates = [s["name"] for s in stale if s["name"] in _BACKTEST_SCRIPTS]
        print(f"[stale] {len(stale)} 个 verdict 过期, {len(candidates)} 个已登记可自动跑")
        for s in stale:
            registered = "✓" if s["name"] in _BACKTEST_SCRIPTS else "✗ (未登记)"
            print(f"  {registered}  {s['name']:<25} age={s['age_days']}d  verdict={s.get('verdict')}")

    if not candidates:
        print("\n无 backtest 需跑, 退出.")
        return

    changes_summary = []
    for name in candidates:
        script = _BACKTEST_SCRIPTS[name]
        old_v = read_verdict(name)
        if old_v:
            _archive_old(name, old_v)

        ok, output = _run_backtest(name, script, timeout_sec=1200)
        if not ok:
            print(f"  ✗ {name} FAIL: {output[:200]}")
            continue

        new_v = read_verdict(name)
        if not new_v:
            print(f"  ✗ {name} 跑完但未写新 verdict (脚本可能没接入 write_verdict)")
            continue

        if old_v:
            diff = _diff_verdict(old_v, new_v)
            if diff["verdict_changed"] or diff["should_integrate_changed"] or diff["metric_deltas"]:
                print(f"\n  ⚠ {name} VERDICT CHANGED:")
                print(f"     verdict: {diff['old_verdict']} → {diff['new_verdict']}")
                if diff["should_integrate_changed"]:
                    print(f"     should_integrate: {old_v.get('should_integrate')} → {new_v.get('should_integrate')}")
                for k, d in diff["metric_deltas"].items():
                    print(f"     {k}: {d['old']} → {d['new']} (Δ {d['delta']:+.4f})")
                # log
                try:
                    append_jsonl(_CHANGE_LOG, {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "name": name,
                        "diff": diff,
                    })
                except Exception as ex:
                    print(f"  [change_log] 失败: {ex}")
                changes_summary.append((name, diff))
            else:
                print(f"  ✓ {name}: verdict 未变 ({new_v.get('verdict')})")
        else:
            print(f"  · {name}: 首次运行, 记录 baseline")

    # 汇总 + notify
    print("\n" + "=" * 70)
    print(f"汇总: 跑了 {len(candidates)}, 变化 {len(changes_summary)}")
    if changes_summary:
        try:
            from notifications import send_alert
            lines = [f"{n}: {d['old_verdict']}→{d['new_verdict']}" for n, d in changes_summary]
            msg = "📊 **Backtest Verdict Changed**\n" + "\n".join(lines)
            send_alert(msg, level="warning", dedup=True)
            print("[notify] verdict change alert sent")
        except Exception:
            pass


if __name__ == "__main__":
    main()
