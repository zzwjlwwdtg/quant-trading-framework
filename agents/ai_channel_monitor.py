"""AI CLI 通道持续故障告警 (2026-11-02).

背景: Claude CLI 的登录在 2026-10-21 过期, 之后 12 天每次调用都 401,
系统一直靠 Codex 兜底, 没有任何告警; 自我进化开发 (只用 Claude) 全部停摆.

本模块只读 signals/ai_calls.jsonl (每次 AI 调用都会记一行), 判断每个通道
是否"连续失败超过 STREAK_MIN 分钟且至少 MIN_CALLS 次", 是则每个 ET 日推送一次
告警 (走 notifications.send_alert → critical_alerts.jsonl → 机器人推送).
通道恢复后推送一次恢复通知. 不改任何交易逻辑, 不调用 AI.

用法: python ai_channel_monitor.py tick   (由 _webui_watchdog 每 ~30 分钟触发)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import SIGNALS_DIR

STATE_PATH = Path(SIGNALS_DIR) / "ai_channel_monitor.json"
STREAK_MIN = 60          # 连续失败超过 60 分钟才告警 (偶发失败不打扰)
MIN_CALLS = 3            # 且至少 3 次失败调用
TAIL_LINES = 3000
CHANNELS = ("claude", "codex")

_FIX_HINT = {
    "auth": "登录已失效，需要在交易电脑上运行 `claude` 并输入 `/login` 重新登录",
    "quota": "额度用完，等额度恢复（或检查订阅）",
}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_tail(path: Path, n: int = TAIL_LINES) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _is_ok(row: dict) -> bool:
    return str(row.get("status") or "").strip().lower() in ("ok", "cache_hit")


def channel_status(rows: list[dict], provider: str, now: datetime) -> dict:
    """返回某通道当前状态: ok / failing / degraded(未达阈值) / no_data."""
    from claude_gate import classify_cli_failure
    calls = []
    for r in rows:
        if str(r.get("provider") or "").lower() != provider:
            continue
        ts = _parse_ts(r.get("ts"))
        if ts is not None and ts <= now:
            calls.append((ts, r))
    if not calls:
        return {"state": "no_data"}
    calls.sort(key=lambda x: x[0])
    last_ts, last = calls[-1]
    if _is_ok(last):
        return {"state": "ok", "last_ok_at": last_ts.isoformat()}
    streak = []
    last_ok_at = None
    for ts, r in reversed(calls):
        if _is_ok(r):
            last_ok_at = ts.isoformat()
            break
        streak.append((ts, r))
    first_fail = streak[-1][0]
    classes = {}
    for _, r in streak:
        c = classify_cli_failure(str(r.get("status") or ""))
        classes[c] = classes.get(c, 0) + 1
    main_class = max(classes, key=classes.get)
    minutes = (now - first_fail).total_seconds() / 60
    state = "failing" if (minutes >= STREAK_MIN and len(streak) >= MIN_CALLS) else "degraded"
    return {
        "state": state, "failure_class": main_class, "fail_count": len(streak),
        "failing_since": first_fail.isoformat(), "minutes": round(minutes),
        "last_ok_at": last_ok_at, "last_status": str(last.get("status") or "")[:160],
    }


def _load_state() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def tick(now: datetime | None = None, send=None) -> dict:
    now = now or datetime.now(timezone.utc)
    import ai_prompt
    import notifications
    from atomic_io import atomic_write_json
    send = send or notifications.send_alert
    rows = _read_tail(Path(ai_prompt.AI_CALL_LOG_PATH))
    prev = _load_state()
    result = {"ts": now.isoformat(), "channels": {}, "alerts": []}
    for ch in CHANNELS:
        st = channel_status(rows, ch, now)
        result["channels"][ch] = st
        prev_ch = (prev.get("channels") or {}).get(ch) or {}
        if st["state"] == "failing":
            cls = st["failure_class"]
            event = f"ai_channel:{ch}:{cls}"
            if not notifications._alert_sent_today(event, "warning"):
                hint = _FIX_HINT.get(cls, "检查 CLI 安装/网络")
                since_h = st["minutes"] / 60
                msg = (f"⚠️ AI 通道 {ch} 连续失败 {since_h:.1f} 小时（{st['fail_count']} 次，类型 {cls}）。"
                       f"{hint}。期间由另一通道兜底；自我进化开发只用 Claude，会停摆。"
                       f"\n最近错误: {st['last_status'][:120]}")
                res = send(msg, level="warning", dedup=False)
                if notifications.alert_delivered(res):
                    notifications._record_alert_sent(event, "warning")
                result["alerts"].append(event)
        elif st["state"] == "ok" and prev_ch.get("state") == "failing":
            msg = f"✅ AI 通道 {ch} 已恢复（之前类型 {prev_ch.get('failure_class')}）"
            send(msg, level="recovery", dedup=False)
            result["alerts"].append(f"ai_channel:{ch}:recovered")
    try:
        atomic_write_json(STATE_PATH, result)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tick":
        print(json.dumps(tick(), ensure_ascii=False, indent=1))
    else:
        print("usage: python ai_channel_monitor.py tick")
