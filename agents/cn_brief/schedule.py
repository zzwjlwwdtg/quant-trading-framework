"""时段调度与提醒去重 (日本时间). 看门狗约每 5 分钟调用一次 tick."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
STATE_DIR = Path(__file__).resolve().parents[1] / "signals" / "cn_brief"
STATE_PATH = STATE_DIR / "state.json"

# 固定汇报时段 (用户 2026-10-19 选定): 盘前 9:00 / 收盘后 16:30 / 晚间 21:00 (日本时间)
SLOTS = {
    "morning": (9, 0),
    "close": (16, 30),
    "evening": (21, 0),
}
SLOT_GRACE_MIN = 40          # 到点后 40 分钟内补发 (电脑睡眠/看门狗延迟), 超过就跳过当天
REMIND_BEFORE_MIN = 30       # 重大数据公布前 30 分钟提醒
REMIND_WINDOW_MIN = (20, 36) # 看门狗 5 分钟一跳, 给一个窗口, 每个事件只提醒一次


def now_jst() -> datetime:
    return datetime.now(JST)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def slot_runs_on(slot: str, now: datetime) -> bool:
    """morning/close 只在工作日 (A股交易日由研报数量再判断); evening 只在工作日 (美国数据多在工作日)."""
    return now.weekday() < 5


def due_slots(now: datetime, state: dict) -> list[str]:
    """返回现在应该跑、且今天还没跑过的时段."""
    out = []
    today = now.date().isoformat()
    done = state.get("slots_done", {})
    for slot, (h, m) in SLOTS.items():
        if not slot_runs_on(slot, now):
            continue
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now < start + timedelta(minutes=SLOT_GRACE_MIN) and done.get(slot) != today:
            out.append(slot)
    return out


def mark_slot_done(state: dict, slot: str, now: datetime) -> None:
    state.setdefault("slots_done", {})[slot] = now.date().isoformat()


def due_reminders(events: list[dict], now: datetime, state: dict) -> list[dict]:
    """events: [{"id", "time_jst" (datetime), "important": bool, ...}] → 现在该提醒、还没提醒过的."""
    sent = set(state.get("reminded", []))
    lo, hi = REMIND_WINDOW_MIN
    out = []
    for ev in events:
        t = ev.get("time_jst")
        if not ev.get("important") or not isinstance(t, datetime):
            continue
        mins = (t - now).total_seconds() / 60
        if lo <= mins <= hi and ev["id"] not in sent:
            out.append(ev)
    return out


def mark_reminded(state: dict, ids: list[str]) -> None:
    lst = state.setdefault("reminded", [])
    lst.extend(i for i in ids if i not in lst)
    del lst[:-500]            # 只留最近 500 条
