"""
notifications.py
─────────────────
主动推送模块 — trade / crisis / 死机时把消息推到 Telegram / Discord。

支持双通道（配置了哪个用哪个，都配了都发）：
  · Discord webhook（简单，一个 URL 完事）
  · Telegram bot（需 bot token + chat_id）

配置在 secrets.local.json:
  {
    "DISCORD_WEBHOOK_URL":  "https://discord.com/api/webhooks/xxx",
    "TELEGRAM_BOT_TOKEN":   "1234:xxxxxxxxxx",
    "TELEGRAM_CHAT_ID":     "-1001234567890"
  }
如都不配 → send_alert() 直接 return，无副作用。

去重：相同 msg 5 分钟内只推一次，避免刷屏（用 signals/notify_dedup.json 存最近发送）。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from config import _cfg, SIGNALS_DIR

DEDUP_PATH   = Path(SIGNALS_DIR) / "notify_dedup.json"
DEDUP_WINDOW = 300   # 5 分钟内相同消息只推一次
TIMEOUT      = 8

LEVEL_EMOJI = {
    "info":     "ℹ️",
    "trade":    "💰",
    "crisis":   "🚨",
    "warning":  "⚠️",
    "recovery": "✅",
    "error":    "❌",
}


def _dedup_check(msg_hash: str) -> bool:
    """True = 已在 dedup window 内发过（应跳过），False = 可以发。"""
    if not DEDUP_PATH.exists():
        return False
    try:
        data = json.loads(DEDUP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    ts = data.get(msg_hash, 0)
    return (time.time() - ts) < DEDUP_WINDOW


def _dedup_record(msg_hash: str) -> None:
    """记录 msg_hash 发送时间。同时清理老的（超过 window 的）。"""
    try:
        DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if DEDUP_PATH.exists():
            try:
                data = json.loads(DEDUP_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        now = time.time()
        data[msg_hash] = now
        # 清理老 entry
        data = {k: v for k, v in data.items() if now - v < DEDUP_WINDOW * 2}
        DEDUP_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _send_discord(webhook_url: str, msg: str) -> bool:
    try:
        payload = json.dumps({"content": msg}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status in (200, 204)
    except Exception:
        return False


def _send_telegram(bot_token: str, chat_id: str, msg: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    chat_id,
            "text":       msg,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
            return bool(body.get("ok"))
    except Exception:
        return False


def send_alert(msg: str, level: str = "info", dedup: bool = True) -> dict:
    """推送主入口。

    Args:
        msg: 消息正文。前面自动加 emoji + timestamp。
        level: info / trade / crisis / warning / recovery / error
        dedup: True 时同一消息 5 分钟内只推一次

    Returns:
        {"sent": [channels_ok], "skipped": [channels_missing], "dedup": bool}
    """
    emoji = LEVEL_EMOJI.get(level, "ℹ️")
    ts_local = datetime.now().strftime("%m-%d %H:%M")
    full_msg = f"{emoji} `[{ts_local}]` {msg}"

    if dedup:
        msg_hash = hashlib.md5(msg.encode("utf-8")).hexdigest()[:12]
        if _dedup_check(msg_hash):
            return {"sent": [], "skipped": ["dedup"], "dedup": True}

    result = {"sent": [], "skipped": [], "dedup": False}

    dc = _cfg("DISCORD_WEBHOOK_URL", "")
    if dc:
        if _send_discord(dc, full_msg):
            result["sent"].append("discord")
        else:
            result["skipped"].append("discord_failed")
    else:
        result["skipped"].append("discord_no_config")

    tg_token = _cfg("TELEGRAM_BOT_TOKEN", "")
    tg_chat  = _cfg("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        if _send_telegram(tg_token, tg_chat, full_msg):
            result["sent"].append("telegram")
        else:
            result["skipped"].append("telegram_failed")
    else:
        result["skipped"].append("telegram_no_config")

    if dedup and result["sent"]:
        _dedup_record(hashlib.md5(msg.encode("utf-8")).hexdigest()[:12])

    # P0-S3 fix (2026-09-05): 若 sent=[] (无 channel 配置或全 fail), 强制 fail-loud:
    # 1) 写 signals/critical_alerts.jsonl 作永久 audit trail
    # 2) 高 level (crisis/warning/error) 用 stderr 显式 print, 让 stdout 消费者也看得到
    # 避免 verdict change / thesis invalidation 静默漏发.
    if not result["sent"] and level in ("crisis", "warning", "error", "trade"):
        try:
            crit_path = Path(SIGNALS_DIR) / "critical_alerts.jsonl"
            crit_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts":       datetime.now(timezone.utc).isoformat(),
                "level":    level,
                "msg":      msg,
                "skipped":  result["skipped"],
            }
            with open(crit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            result["fallback"] = "critical_alerts.jsonl"
        except Exception:
            pass
        # stderr echo — 让 orchestrator log / systemd journal 能捕获
        try:
            import sys
            print(f"[ALERT-FALLBACK {level.upper()}] {msg[:200]}", file=sys.stderr, flush=True)
        except Exception:
            pass

    return result


# ── 语义化便捷函数（业务侧调用） ──────────────────────────────────────
def notify_trade(ticker: str, side: str, qty: int, price: float,
                 tag: str = "", conf: int | None = None) -> dict:
    conf_str = f" conf={conf}" if conf else ""
    msg = f"**{side} {qty} {ticker}** @ ${price:.2f}{conf_str}\n{tag}"
    return send_alert(msg, level="trade", dedup=False)  # trades 不去重


def notify_crisis(ticker: str, pct_chg: float, reason: str = "") -> dict:
    msg = f"**CRISIS** {ticker} 单日 {pct_chg:+.2f}% → 触发 crisis regime\n{reason}"
    return send_alert(msg, level="crisis")


def notify_jp_guidance_opportunity(ticker: str, name: str, rsi: float,
                                     pct_5d: float, guidance: dict) -> dict:
    """JP 股极端超卖 + 業績予想上修 双重共振 → 高价值 catalyst 提示。

    guidance: {direction, magnitude, guidance_note, revision_reason, ...}
    只在两个条件同时满足时触发（调用方保证）:
      - RSI < 35 (极端超卖)
      - guidance.direction == "上修"
    """
    mag = guidance.get("magnitude", "?")
    note = (guidance.get("guidance_note") or "")[:100]
    reason = (guidance.get("revision_reason") or "")[:100]
    reaction = guidance.get("market_reaction", "?")
    msg = (
        f"**🇯🇵 JP CATALYST — {ticker} ({name})**\n"
        f"技术: RSI **{rsi:.0f}** 极端超卖 · 5d {pct_5d:+.1f}%\n"
        f"业绩指引: **上修 {mag}** · 市場反応 {reaction}\n"
        f"指引: {note}\n"
        f"理由: {reason}\n"
        f"→ 技术 + 基本面双重共振，可关注反弹机会（人工判断）"
    )
    return send_alert(msg, level="jp_catalyst")


def notify_watchdog(event: str, old_pid=None, new_pid=None) -> dict:
    if event == "dead_restart":
        msg = f"orchestrator 死亡（旧 PID {old_pid}），已自动重启 → 新 PID {new_pid}"
        return send_alert(msg, level="warning")
    elif event == "no_lock_start":
        msg = f"orchestrator 未在跑，watchdog 已启动 → PID {new_pid}"
        return send_alert(msg, level="recovery")
    elif event == "launch_failed":
        msg = f"orchestrator 启动失败：{old_pid}"   # 借用字段传错误信息
        return send_alert(msg, level="error")
    return {"sent": [], "skipped": ["unknown_event"]}


if __name__ == "__main__":
    # 命令行测试：python notifications.py
    print("测试推送...")
    r = send_alert("notifications.py 测试消息 — 如果收到说明配置正确", level="info", dedup=False)
    print(json.dumps(r, indent=2, ensure_ascii=False))
