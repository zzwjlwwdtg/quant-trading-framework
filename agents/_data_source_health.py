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
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# ── 退市/长期 404 proxy 符号登记簿 ────────────────────────────────────────────
# EVO-20261014-3 (2026-10-19): 日本/台湾代理符号 (6488.TW / 3688.T 等) 持续 404,
# 旧行为是整个 PCA 轮次跳过, 向下传染到 regime 层. 现在把"连续 N 天 404"的符号
# 写入 signals/retired_symbols.json 单一源, 让 PCA / data_quality / moomoo_pool
# 等调用方统一跳过它们, 并把健康报告里 'critical' 的噪音分离到 'retired' 分类.
#
# 文件 schema:
#   {
#     "schema_version": 1,
#     "updated_at": "<ISO>",
#     "threshold_days": 3,
#     "symbols": {
#       "6488.TW": {
#         "status": "retired" | "watching" | "ok",
#         "miss_days": int,                 # 连续失败的日历天数
#         "first_miss_date": "YYYY-MM-DD",
#         "last_miss_date":  "YYYY-MM-DD",
#         "last_success_date": "YYYY-MM-DD" | null,
#         "retired_at": "<ISO>" | null,
#         "reason": "..."
#       }, ...
#     }
#   }
RETIRED_THRESHOLD_DAYS = 3

_logger = logging.getLogger(__name__)


def _retired_state_path() -> Path:
    """signals/retired_symbols.json 的解析路径 (懒加载; 容忍 config 不可用)."""
    try:
        from config import SIGNALS_DIR
        return Path(SIGNALS_DIR) / "retired_symbols.json"
    except Exception:
        return Path(__file__).resolve().parent / "signals" / "retired_symbols.json"


def _now_date_str(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _now_iso(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_retired_state(path: Optional[Path] = None) -> dict:
    """读 retired_symbols.json. 文件不存在或损坏时返回空结构."""
    p = Path(path) if path else _retired_state_path()
    if not p.exists():
        return {"schema_version": 1, "updated_at": None,
                "threshold_days": RETIRED_THRESHOLD_DAYS, "symbols": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "updated_at": None,
                "threshold_days": RETIRED_THRESHOLD_DAYS, "symbols": {}}
    if not isinstance(data, dict):
        return {"schema_version": 1, "updated_at": None,
                "threshold_days": RETIRED_THRESHOLD_DAYS, "symbols": {}}
    data.setdefault("symbols", {})
    data.setdefault("threshold_days", RETIRED_THRESHOLD_DAYS)
    return data


def _save_retired_state(state: dict, path: Optional[Path] = None) -> None:
    p = Path(path) if path else _retired_state_path()
    state["updated_at"] = _now_iso()
    try:
        from atomic_io import atomic_write_json
        atomic_write_json(p, state)
    except Exception:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def list_retired(path: Optional[Path] = None) -> list[str]:
    """返回已标 retired 的符号列表 (status == 'retired')."""
    state = load_retired_state(path)
    return sorted(
        s for s, meta in state.get("symbols", {}).items()
        if isinstance(meta, dict) and meta.get("status") == "retired"
    )


def is_retired(symbol: str, path: Optional[Path] = None) -> bool:
    state = load_retired_state(path)
    meta = state.get("symbols", {}).get(symbol) or {}
    return meta.get("status") == "retired"


def record_fetch_result(
    symbol: str,
    ok: bool,
    *,
    reason: str = "",
    threshold_days: int = RETIRED_THRESHOLD_DAYS,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> dict:
    """登记一次数据拉取结果.

    - ok=True  → 清零 miss_days / 复活已 retired 的符号, status 回到 ok
    - ok=False → 按日历天数计数 (同一天多次 miss 只算 1 次),
                 miss_days ≥ threshold_days 自动打 'retired' 标签

    返回 {status, miss_days, newly_retired}.
    """
    state = load_retired_state(path)
    sym_db = state.setdefault("symbols", {})
    cur = dict(sym_db.get(symbol) or {})
    today = _now_date_str(now)
    now_iso = _now_iso(now)
    newly_retired = False

    if ok:
        cur_status = "ok"
        cur = {
            "status": cur_status,
            "miss_days": 0,
            "first_miss_date": None,
            "last_miss_date": cur.get("last_miss_date"),
            "last_success_date": today,
            "retired_at": None,
            "reason": "",
        }
    else:
        last_miss_date = cur.get("last_miss_date")
        # 同一天内多次失败只累加一次, 避免高频调用误标 retired.
        if last_miss_date == today:
            miss_days = int(cur.get("miss_days") or 0)
        else:
            miss_days = int(cur.get("miss_days") or 0) + 1
        was_retired = cur.get("status") == "retired"
        new_status = "retired" if miss_days >= threshold_days else "watching"
        if new_status == "retired" and not was_retired:
            newly_retired = True
        cur = {
            "status": new_status,
            "miss_days": miss_days,
            "first_miss_date": cur.get("first_miss_date") or today,
            "last_miss_date": today,
            "last_success_date": cur.get("last_success_date"),
            "retired_at": now_iso if new_status == "retired" and not was_retired
                          else cur.get("retired_at"),
            "reason": reason or cur.get("reason") or "",
        }
        if newly_retired:
            # retire 事件只打一次 WARNING, 后续沉默, 避免同样的 404 继续刷日志.
            try:
                _logger.warning(
                    "[retired] %s 连续 %d 天 404/empty, 标记 retired; reason=%s",
                    symbol, miss_days, reason or "n/a",
                )
            except Exception:
                pass

    sym_db[symbol] = cur
    state["threshold_days"] = threshold_days
    _save_retired_state(state, path=path)
    return {
        "symbol": symbol,
        "status": cur["status"],
        "miss_days": cur["miss_days"],
        "newly_retired": newly_retired,
    }


def mark_many(
    symbols: list[str],
    ok: bool,
    *,
    reason: str = "",
    threshold_days: int = RETIRED_THRESHOLD_DAYS,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> dict[str, dict]:
    """批量登记. 便于 PCA 等批量拉取的场景一次性收尾."""
    out = {}
    for sym in symbols:
        out[sym] = record_fetch_result(
            sym, ok, reason=reason, threshold_days=threshold_days,
            now=now, path=path,
        )
    return out


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
