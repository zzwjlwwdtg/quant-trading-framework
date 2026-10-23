"""
回测菜单 — 一键跑各个回测脚本，按 [feedback_backtest_gate.md] 规则验证不退化。

bat 入口：backtest.bat
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).parent.resolve()
PY = sys.executable


def run(args):
    subprocess.run([PY, "-X", "utf8", "-u"] + args, cwd=SCRIPT_DIR)


def pause():
    input("\n按回车继续...")


def cmd_regime():
    print("\n[1/1] 跑 regime 单一源回测（250 天，3 只 ETF）...")
    run(["_backtest_regime_fix.py"])
    pause()


def cmd_news():
    print("\n[1/1] 跑新闻管道回测（keyword vs CLI 法，8 个事件）...")
    run(["_backtest_news_pipeline.py"])
    pause()


def cmd_trump():
    print("\n[1/1] 跑 Trump signal 回测（25 天采样 × CLI，约 15-25 分钟）...")
    print("      数据源: F:/trump-code/data/predictions_log.json")
    if input("  确认开跑？(y/N): ").strip().lower() != "y":
        return
    run(["_backtest_trump_signal.py"])
    pause()


def cmd_engine_lite():
    print("\n[1/1] backtest_engine Lite — 信号方向命中率（60 天）...")
    run(["-c", "from backtest_engine import run_lite, report_lite\n"
                "r = run_lite()\n"
                "[print(l) for l in report_lite(r)]"])
    pause()


def cmd_engine_mid():
    print("\n[1/1] backtest_engine Mid — 完整 trader 模拟 + P&L（60 天）...")
    run(["-c", "from backtest_engine import run_mid, report_mid\n"
                "r = run_mid()\n"
                "[print(l) for l in report_mid(r)]"])
    pause()


def cmd_modules():
    print("\n[1/1] 模块准确率 — 各系统模块 × 1d/5d/10d/20d × 3 标的（约 3-5 分钟）...")
    print("      输出到 signals/module_accuracy.md，AI prompt 会自动注入")
    if input("  确认开跑？(y/N): ").strip().lower() != "y":
        return
    run(["_backtest_modules_accuracy.py"])
    pause()


def cmd_all():
    print("\n顺序跑：regime → news → modules → trump（约 25-35 分钟）")
    if input("  确认开跑？(y/N): ").strip().lower() != "y":
        return
    cmd_regime()
    cmd_news()
    cmd_modules()
    cmd_trump()


MENU = [
    ("1", "regime 单一源",      "decision_agent 改 regime 路径必跑",  cmd_regime),
    ("2", "新闻管道",           "events_watch 改 RSS / CLI 解析必跑", cmd_news),
    ("3", "Trump signal",       "trump_signal / events_watch 改必跑（约 20 分钟）", cmd_trump),
    ("4", "engine Lite",        "两档回测中的轻量信号方向命中率",    cmd_engine_lite),
    ("5", "engine Mid",         "完整 trader 模拟 P&L vs B&H",      cmd_engine_mid),
    ("6", "模块准确率",         "各模块 × 1d/5d/10d/20d → 写 signals/module_accuracy.md（推荐周末跑）", cmd_modules),
    ("a", "全跑（1-6 顺序）",   "约 25-35 分钟",                     cmd_all),
]


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def main():
    while True:
        clear()
        print("=" * 62)
        print("  Backtest 菜单 — 改完代码必跑，新版 ≥ 旧版才算改完")
        print("=" * 62)
        print()
        for k, label, desc, _ in MENU:
            print(f"   {k}.  {label:<18} {desc}")
        print()
        print("   q.  退出")
        print()
        choice = input("请选择 (1-6 / a / q): ").strip().lower()
        if choice == "q":
            return
        for k, _, _, fn in MENU:
            if choice == k:
                try:
                    fn()
                except KeyboardInterrupt:
                    print("\n[已中断]")
                    pause()
                break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n再见")
