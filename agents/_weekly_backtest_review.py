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
