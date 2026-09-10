"""_backtest_reviewer.py — Independent AI validator for backtest scripts.

背景 (2026-09-09):
    Chang/Roan GPT-6 Astra paper 里 8-bot architecture 有个 "Independent Validator"
    层专门查其它 layer 输出 (data leakage / look-ahead / walk-forward strictness /
    multiple testing correction). 你现在的系统 _backtest_*.py 都是 "同一 AI 写
    同一 AI 评", 缺 independent audit.

    本 module 让 codex CLI 独立读 backtest .py 源码, 按固定 checklist 审计,
    输出 audit report 到 signals/backtest_audit/. 不做 rerun (rerun 由 weekly
    review 负责), 只做 code-level static audit.

Audit checklist (硬编码, 每次同样问):
  1. **Data leakage**: 用未来数据算过去信号 (常见: rolling window 用 .rolling().mean()
     无 shift(1), 或 sklearn train-test split 时训练集含 t 之后的 sample)
  2. **Look-ahead in features**: pct_change / z-score / max-drawdown 时用了 t 时刻
     还不可知的信息
  3. **Walk-forward strictness**: 用 train/test split 时是否严格时间切分, 用
     rolling percentile 时 window 是否 shift(1)
  4. **Multiple testing correction**: 是否测试了多个阈值/参数, 有没有做 Bonferroni
     或 PBO (probability of backtest overfit); 若无, 通过 pass 应加严
  5. **Survivor bias**: universe 是否含已退市 ticker (只用当前存活 = biased)
  6. **Transaction cost / slippage**: 有没有模型或假设; 若无, 高频信号 realized
     可能远差于纸面
  7. **In-sample bias**: 阈值/参数是否**在同批数据上**先看结果再改 (data snooping)
  8. **Sample size**: n 是否足够 (feedback_oos_required: N ≤ 5 立 hard rule = 过拟合)

Output 结构:
  signals/backtest_audit/<script_stem>_<timestamp>.md
  {
    "script": "_backtest_stop_distance.py",
    "audited_at": "2026-09-09T14:00:00Z",
    "verdict": "PASS" / "PASS_WITH_CAVEATS" / "SUSPICIOUS" / "REJECT",
    "findings": [ {severity, category, line, description, fix_suggestion}, ... ],
    "summary": "<one-paragraph>",
    "reviewer_model": "complexity=complex"
  }

CLI:
    python _backtest_reviewer.py --script _backtest_stop_distance.py
    python _backtest_reviewer.py --all           # 审所有 _backtest_*.py
    python _backtest_reviewer.py --changed 30    # 只审最近 30d 有改动的
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.stdout.reconfigure(line_buffering=True)

from config import SIGNALS_DIR

_HERE = Path(__file__).parent
_AUDIT_DIR = Path(SIGNALS_DIR) / "backtest_audit"

_CHECKLIST = """
你是量化系统的 independent code auditor. 你**独立**审查回测脚本, 找 8 类常见 bug.
你**不必**rerun 代码, 只做 static analysis. 用 **中文** 输出, 简洁准确.

**审查 checklist**:

1. **DATA_LEAKAGE**: 用了未来数据算过去信号? 常见:
   - `df.rolling(N).mean()` 未 shift(1) → 当前 bar 用了自己
   - train/test split 时时间乱序 / random split (时间序列必须严格切分)
   - fwd_return 和 signal 用同一 bar 的 close 造成 signal 看到自己触发的价

2. **LOOK_AHEAD**: percentile / z-score / max drawdown 使用了 t 时刻不可知的信息?
   - `.quantile()` on 全期数据然后用来分桶
   - rolling percentile 用全 window 未 shift

3. **WALK_FORWARD_STRICTNESS**: 用了 train/test 但边界不严? train 结束是否 <= test 开始?

4. **MULTIPLE_TESTING**: 测了多个阈值/参数, 是否 Bonferroni 或 PBO 校正?
   若测了 N 个策略/参数还是用单一 t>2, 则真实 t 门槛应约 sqrt(2×log(N)) 后再判.
   常见: 5 个 stop 阈值都跑, 报最好那个 → 严重过拟合

5. **SURVIVOR_BIAS**: universe 只含存活 ticker (yfinance 不给已退市)? 结果偏乐观.

6. **TRANSACTION_COST**: 有 slippage/spread/commission 模型? 无则高频 signal 纸面 -0.1%
   实际可能 -0.5%.

7. **IN_SAMPLE_TUNING**: 阈值/pass 条件在**同批**数据上跑完再定? (data snooping)
   通过条件应 **硬编码在头部, 跑前定**.

8. **SAMPLE_SIZE**: n 是否足以有统计意义? memory 里已知 N ≤ 5 立 hard rule 是过拟合.

**输出格式** (严格 JSON, 无其它文字, 无 markdown code fence):
{
  "verdict": "PASS" | "PASS_WITH_CAVEATS" | "SUSPICIOUS" | "REJECT",
  "summary": "<一段话总结, 100-200 字>",
  "findings": [
    {
      "severity": "critical" | "high" | "medium" | "low" | "info",
      "category": "DATA_LEAKAGE" | "LOOK_AHEAD" | "WALK_FORWARD_STRICTNESS" | ... (上面 8 类之一),
      "line": <相关行号 int, 未知填 0>,
      "description": "<描述具体问题>",
      "fix_suggestion": "<具体建议改法>"
    }
  ]
}

verdict 判定:
  PASS: 无 medium+ 问题
  PASS_WITH_CAVEATS: 只有 low/info 问题, 或 medium 但已在脚本注释里 caveat 声明
  SUSPICIOUS: 至少 1 个 medium 问题, 或 2+ low
  REJECT: 至少 1 个 critical/high 问题
"""


def _list_backtests() -> list[Path]:
    """所有 _backtest_*.py (不含 __pycache__ / tests)."""
    return sorted(p for p in _HERE.glob("_backtest_*.py")
                   if p.is_file() and "tests" not in str(p))


def _find_changed(days: int) -> list[Path]:
    cutoff = time.time() - days * 86400
    return [p for p in _list_backtests() if p.stat().st_mtime > cutoff]


def _extract_json(text: str) -> Optional[dict]:
    """From AI response text, extract the first valid JSON object."""
    if not text:
        return None
    # 优先直接 parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # 找 ```json ... ``` fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 找第一个 { 到最后一个 }
    m = re.search(r"(\{.*\})", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def audit_script(script_path: Path, timeout: int = 300) -> dict:
    """Static audit one backtest script via AI CLI. Returns report dict.
    静默失败 → verdict=UNKNOWN 但不 raise."""
    stem = script_path.stem
    try:
        src = script_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {
            "script":     stem,
            "audited_at": datetime.now(timezone.utc).isoformat(),
            "verdict":    "UNKNOWN",
            "summary":    f"read source failed: {e}",
            "findings":   [],
        }

    prompt = (
        _CHECKLIST +
        f"\n\n=== SCRIPT: {stem}.py ===\n\n"
        + src +
        "\n\n=== END SCRIPT ===\n\n"
        "只输出 JSON, 无其它文字."
    )

    try:
        from ai_prompt import query_ai_cli
        output, status, provider, _ = query_ai_cli(prompt, timeout=timeout,
                                                    complexity="complex")
    except Exception as e:
        return {
            "script":     stem,
            "audited_at": datetime.now(timezone.utc).isoformat(),
            "verdict":    "UNKNOWN",
            "summary":    f"AI CLI unavailable: {e}",
            "findings":   [],
        }

    parsed = _extract_json(output or "") or {}
    report = {
        "script":         stem,
        "script_mtime":   datetime.fromtimestamp(script_path.stat().st_mtime,
                                                  tz=timezone.utc).isoformat(),
        "audited_at":     datetime.now(timezone.utc).isoformat(),
        "reviewer_model": f"ai_cli={provider}, complexity=complex",
        "verdict":        parsed.get("verdict", "UNKNOWN"),
        "summary":        parsed.get("summary", output[:500] if output else "no output"),
        "findings":       parsed.get("findings", []),
        "cli_status":     status,
    }
    return report


def _save_report(report: dict) -> Path:
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = _AUDIT_DIR / f"{report['script']}_{stamp}.md"
    lines = [
        f"# Backtest Audit · {report['script']}.py",
        f"",
        f"- **Verdict**: {report.get('verdict')}",
        f"- **Audited at**: {report.get('audited_at')}",
        f"- **Script mtime**: {report.get('script_mtime')}",
        f"- **Reviewer**: {report.get('reviewer_model')}",
        f"- **CLI status**: {report.get('cli_status', 'n/a')}",
        f"",
        f"## Summary",
        f"",
        report.get("summary", "(no summary)"),
        f"",
        f"## Findings ({len(report.get('findings', []))})",
        f"",
    ]
    for f in report.get("findings", []):
        lines.append(f"### [{f.get('severity','?')}] {f.get('category','?')} · line {f.get('line', '?')}")
        lines.append(f"")
        lines.append(f"**Description**: {f.get('description','')}")
        lines.append(f"")
        lines.append(f"**Fix**: {f.get('fix_suggestion','')}")
        lines.append(f"")
    lines.append(f"---")
    lines.append(f"```json")
    lines.append(json.dumps(report, ensure_ascii=False, indent=2))
    lines.append(f"```")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _short_verdict(report: dict) -> str:
    v = report.get("verdict", "?")
    n = len(report.get("findings", []))
    n_crit = sum(1 for f in report.get("findings", []) if f.get("severity") == "critical")
    n_high = sum(1 for f in report.get("findings", []) if f.get("severity") == "high")
    icon = {"PASS":"✓", "PASS_WITH_CAVEATS":"⚠", "SUSPICIOUS":"⚠", "REJECT":"✗", "UNKNOWN":"?"}.get(v, "?")
    detail = f" (crit={n_crit}, high={n_high}, total={n})" if n else ""
    return f"{icon} {v}{detail}"


def run(scripts: list[Path], write_reports: bool = True) -> list[dict]:
    reports = []
    for i, sp in enumerate(scripts, 1):
        print(f"\n[{i}/{len(scripts)}] auditing {sp.name} ...")
        r = audit_script(sp)
        reports.append(r)
        if write_reports:
            p = _save_report(r)
            print(f"  {_short_verdict(r)}  → {p.relative_to(_HERE)}")
        else:
            print(f"  {_short_verdict(r)}")
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--script", help="单个脚本文件名 (e.g. _backtest_stop_distance.py)")
    grp.add_argument("--all", action="store_true", help="审所有 _backtest_*.py")
    grp.add_argument("--changed", type=int, metavar="DAYS",
                     help="只审最近 N 天有改动的")
    parser.add_argument("--no-write", action="store_true",
                        help="不落盘, 只 print")
    args = parser.parse_args()

    if args.script:
        p = _HERE / args.script
        if not p.exists():
            print(f"! not found: {p}")
            sys.exit(1)
        scripts = [p]
    elif args.all:
        scripts = _list_backtests()
    else:
        scripts = _find_changed(args.changed)

    if not scripts:
        print("no scripts to audit")
        return

    print(f"auditing {len(scripts)} script(s):")
    for p in scripts:
        print(f"  - {p.name}")

    reports = run(scripts, write_reports=not args.no_write)

    # 汇总
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in reports:
        print(f"  {r['script']:<35} {_short_verdict(r)}")
    verdicts = [r.get("verdict") for r in reports]
    n_reject = verdicts.count("REJECT")
    n_susp = verdicts.count("SUSPICIOUS")
    if n_reject:
        print(f"\n✗ {n_reject} REJECT — 立刻 review + 修")
    if n_susp:
        print(f"⚠ {n_susp} SUSPICIOUS — 查明后再信 verdict")


if __name__ == "__main__":
    main()
