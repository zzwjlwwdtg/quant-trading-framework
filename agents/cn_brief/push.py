"""推送渠道 (可插拔). 生成和推送分开: 机器人没定之前, 报告照样写文件.

渠道 (都在 secrets.local.json 或环境变量里配置, 不配就跳过):
  - Discord:  DISCORD_WEBHOOK_URL            (沿用 notifications 的配置)
  - Telegram: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  - 通用:     CN_BRIEF_WEBHOOK_URL (不配则用 BOT_WEBHOOK_URL)  POST JSON {"source","type":"brief","slot","title","markdown","data"}
              (给以后选定的第三方机器人库用: 它起一个本地 HTTP 接收端即可)
单独给本模块用的 Discord/Telegram 可以另配 CN_BRIEF_DISCORD_WEBHOOK_URL / CN_BRIEF_TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DISCORD_LIMIT = 1900
TELEGRAM_LIMIT = 3900


def _cfg(key: str, default: str = "") -> str:
    try:
        from config import _cfg as c
        return c(key, default) or default
    except Exception:
        import os
        return os.environ.get(key, default)


def chunk(text: str, limit: int) -> list[str]:
    """按行切分, 每段不超过 limit (单行超长时硬切)."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        parts.append(cur)
    return parts


def push(title: str, markdown: str, slot: str = "", data: dict | None = None) -> dict:
    result = {"sent": [], "skipped": []}
    text = f"**{title}**\n{markdown}" if title else markdown
    try:
        from notifications import _send_discord, _send_telegram
    except Exception as e:
        _send_discord = _send_telegram = None
        result["skipped"].append(f"notifications_import: {e}")

    dc = _cfg("CN_BRIEF_DISCORD_WEBHOOK_URL") or _cfg("DISCORD_WEBHOOK_URL")
    if dc and _send_discord:
        ok = all(_send_discord(dc, part) for part in chunk(text, DISCORD_LIMIT))
        (result["sent"] if ok else result["skipped"]).append("discord" if ok else "discord_failed")
    else:
        result["skipped"].append("discord_no_config")

    tg_token = _cfg("TELEGRAM_BOT_TOKEN")
    tg_chat = _cfg("CN_BRIEF_TELEGRAM_CHAT_ID") or _cfg("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat and _send_telegram:
        ok = all(_send_telegram(tg_token, tg_chat, part) for part in chunk(text, TELEGRAM_LIMIT))
        (result["sent"] if ok else result["skipped"]).append("telegram" if ok else "telegram_failed")
    else:
        result["skipped"].append("telegram_no_config")

    hook = _cfg("CN_BRIEF_WEBHOOK_URL") or _cfg("BOT_WEBHOOK_URL")   # 2026-10-19: 没单独配就推给统一的机器人地址
    if hook:
        try:
            import requests
            r = requests.post(hook, json={"source": "fsi-skills", "type": "brief", "slot": slot, "title": title,
                                          "markdown": markdown, "data": data or {}}, timeout=15)
            (result["sent"] if r.ok else result["skipped"]).append(
                "webhook" if r.ok else f"webhook_http_{r.status_code}")
        except Exception as e:
            result["skipped"].append(f"webhook_error: {str(e)[:120]}")
    else:
        result["skipped"].append("webhook_no_config")
    return result
