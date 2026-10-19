"""
_data_source_health.py
──────────────────────
检查所有数据源是否健康。失败的源 → 自动 fallback / 警报。

当前依赖：
  · yfinance      : 价格 / 期权 / earnings_dates       (free, ~15min delay)
  · FRED          : VIX / F&G / T10Y2Y / FEDFUNDS    (API key 需要)
  · moomoo OpenD  : 真实订单 + 持仓 + 实时报价         (登录态)
  · AI CLI        : Claude（默认）/ Codex（备用, 2026-10-08 起）  (本机登录态)
  · 期权链        : yfinance options（无 OI，只用 today volume）

已知缺口（P5.2 仍开放）:
  · 财报日期：yfinance 偶尔比官方早 1 天（MU 2026-06-25 实际 6-24 AMC）
  · 期权 OI：yfinance 不返回，导致 GEX proxy 用今日 volume 代理（有偏）
  · 备份源：未接入 polygon.io / earningswhispers / TastyTrade

接入：
  · weekly.bat 已集成（每周末检查一次）
  · 失败时邮件 / AI 报告（待加）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def check_yfinance() -> tuple[bool, str]:
    try:
        import yfinance as yf
        t = yf.Ticker("SPY")
        df = t.history(period="3d", interval="1d", auto_adjust=True)
        if df is None or df.empty:
            return False, "yfinance SPY history empty"
        # 期权链
        try:
            opts = t.options
            if not opts:
                return False, "yfinance options 空（可能 SPY 数据有问题）"
        except Exception as e:
            return False, f"yfinance options 抓取失败: {e}"
        return True, f"yfinance OK (SPY last close ${float(df['Close'].iloc[-1]):.2f}, {len(opts)} expiries)"
    except Exception as e:
        return False, f"yfinance import/fetch 失败: {e}"


def check_fred() -> tuple[bool, str]:
    try:
        from config import _cfg
        key = _cfg("FRED_API_KEY")
        if not key:
            return False, "FRED_API_KEY 未配置"
        import urllib.request
        url = (f"https://api.stlouisfed.org/fred/series/observations?"
                f"series_id=VIXCLS&api_key={key}&file_type=json&limit=1&sort_order=desc")
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
        if not data.get("observations"):
            return False, "FRED 返回为空"
        return True, f"FRED OK (VIX latest: {data['observations'][0]['value']})"
    except Exception as e:
        return False, f"FRED 失败: {e}"


def check_moomoo() -> tuple[bool, str]:
    try:
        from moomoo import OpenSecTradeContext, TrdMarket, SecurityFirm, RET_OK
        from config import OPEND_HOST, OPEND_PORT, MOOMOO_ACC_ID
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=OPEND_HOST, port=OPEND_PORT,
            security_firm=SecurityFirm.FUTUINC,
        )
        ret, info = ctx.get_acc_list()
        ctx.close()
        if ret != RET_OK:
            return False, f"OpenD acc_list 失败: {info}"
        return True, f"moomoo OK ({len(info)} accounts)"
    except Exception as e:
        return False, f"moomoo 失败: {e}"


def check_ai_cli() -> tuple[bool, str]:
    try:
        from ai_prompt import (
            _find_claude_cli,
            _find_codex_cli,
            get_ai_cli_policy,
        )
        policy = get_ai_cli_policy()
        primary = policy["primary"]
        fallback = policy["fallback"]
        finders = {"codex": _find_codex_cli, "claude": _find_claude_cli}
        path = finders[primary]()
        if path:
            extra = f"，fallback={fallback}" if fallback != "none" else "，无自动 fallback"
            return True, f"主 CLI={primary}: {path}{extra}"
        return False, f"主 CLI={primary} 未安装 / 不在 PATH"
    except Exception as e:
        return False, f"AI CLI 检查失败: {e}"


def check_calibration() -> tuple[bool, str]:
    try:
        path = Path(__file__).parent / "signals" / "confidence_calibration.json"
        if not path.exists():
            return False, "校准文件不存在 — 跑 _calibrate_confidence.py"
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data.get("ts", "unknown")
        age_days = (datetime.now() - datetime.fromisoformat(ts)).days
        msg = f"校准文件年龄 {age_days} 天"
        if age_days > 14:
            return False, msg + " (太老，建议重跑 weekly.bat)"
        return True, msg
    except Exception as e:
        return False, f"校准文件读取失败: {e}"


def check_hmm() -> tuple[bool, str]:
    try:
        from hmm_regime import load
        info = load()
        if not info:
            return False, "HMM state 文件不存在 — 跑 hmm_regime.py"
        ts = info.get("ts", "unknown")
        age_days = (datetime.now() - datetime.fromisoformat(ts)).days
        label = info.get("current_label", "?")
        prob = info.get("current_prob", 0)
        msg = f"HMM={label} (prob={prob:.2f}, 文件年龄 {age_days}天)"
        if age_days > 3:
            return False, msg + " (太老)"
        return True, msg
    except Exception as e:
        return False, f"HMM 读取失败: {e}"


def check_point_in_time_and_signals() -> tuple[bool, str]:
    try:
        from data_quality import audit_latest_signal_files
        base = Path(__file__).parent / "signals"
        report = audit_latest_signal_files(base)
        pit = base / "point_in_time_market.jsonl"
        pit_note = "PIT已启用" if pit.exists() else "PIT将在下个交易cycle建立"
        ok = report["critical_files"] == 0
        return ok, (
            f"latest={report['checked_files']} critical={report['critical_files']} "
            f"warning={report['warning_files']} · {pit_note}"
        )
    except Exception as e:
        return False, f"point-in-time/信号审计失败: {e}"


def check_execution_audit() -> tuple[bool, str]:
    try:
        from execution_analytics import verify_execution_log
        path = Path(__file__).parent / "signals" / "execution_ledger.jsonl"
        result = verify_execution_log(path)
        if not result["valid"]:
            return False, f"执行审计链损坏: {result}"
        return True, f"执行审计链 OK (hashed={result['checked']}, legacy={result['legacy_unhashed']})"
    except Exception as e:
        return False, f"执行审计链检查失败: {e}"


def main():
    print("=" * 78)
    print("  数据源健康检查")
    print("=" * 78)
    checks = [
        ("yfinance",     check_yfinance),
        ("FRED",         check_fred),
        ("moomoo OpenD", check_moomoo),
        ("AI CLI",       check_ai_cli),
        ("校准文件",     check_calibration),
        ("HMM 状态",     check_hmm),
        ("PIT/信号质量", check_point_in_time_and_signals),
        ("执行审计链",   check_execution_audit),
    ]
    fail_count = 0
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"check 异常: {e}"
        symbol = "✅" if ok else "❌"
        print(f"  {symbol} {name:<14}: {msg}")
        if not ok:
            fail_count += 1
    print()
    if fail_count == 0:
        print("  全部 OK")
        return 0
    else:
        print(f"  {fail_count} 项失败 — 检查并修复")
        return fail_count


if __name__ == "__main__":
    sys.exit(main())
