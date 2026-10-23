"""看门狗每 ~5 分钟调用一次: 到点汇报 + 重大数据公布前 30 分钟提醒 + 公布后补发实际值."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import brief as B
from . import schedule as S
from .push import push

CACHE = S.STATE_DIR / "calendar_cache.json"
LOG = S.STATE_DIR / "push_log.jsonl"
CACHE_TTL_MIN = 60
RESULT_FOLLOWUP_MIN = 120      # 公布后 2 小时内, 拿到公布值就补发一次


def _log(kind: str, title: str, res: dict) -> None:
    S.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": S.now_jst().isoformat(timespec="seconds"), "kind": kind, "title": title,
                            "sent": res.get("sent"), "skipped": res.get("skipped")}, ensure_ascii=False) + "\n")


def _ser(ev: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in ev.items()}


def _de(ev: dict) -> dict:
    ev = dict(ev)
    for k in ("time_jst", "time_bjt"):
        if isinstance(ev.get(k), str):
            try:
                ev[k] = datetime.fromisoformat(ev[k])
            except ValueError:
                ev[k] = None
    return ev


def load_events(now: datetime, state: dict, force: bool = False) -> list[dict]:
    cached = []
    try:
        c = json.loads(CACHE.read_text(encoding="utf-8"))
        age = (now - datetime.fromisoformat(c["ts"])).total_seconds() / 60
        cached = [_de(e) for e in c.get("events", [])]
    except Exception:
        age = 1e9
    reminded = set(state.get("reminded", []))
    waiting_result = any(e["id"] in reminded and not B._val(e.get("pubVal")) and e.get("time_jst")
                         and now - timedelta(minutes=RESULT_FOLLOWUP_MIN) <= e["time_jst"] <= now for e in cached)
    if not force and cached and age < CACHE_TTL_MIN and not waiting_result:
        return cached
    today_bjt = now.astimezone(B.BJT).date()
    evs, errs = B.important_events(today_bjt - timedelta(days=1), 3)
    if not evs and cached:
        return cached        # 拉取失败时沿用缓存
    S.STATE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"ts": now.isoformat(), "events": [_ser(e) for e in evs], "errors": errs},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    return evs


def run_tick(now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or S.now_jst()
    state = S.load_state()
    done = {"slots": [], "reminders": [], "results": []}

    for slot in S.due_slots(now, state):
        b = B.build_brief(slot, now=now)
        res = {"sent": [], "skipped": ["dry_run"]} if dry_run else push(b["title"], b["markdown"], slot=slot)
        _log(f"slot:{slot}", b["title"], res)
        S.mark_slot_done(state, slot, now)
        done["slots"].append({"slot": slot, "push": res})

    events = load_events(now, state)
    due = S.due_reminders(events, now, state)
    by_time: dict = {}
    for ev in due:
        by_time.setdefault(ev["time_jst"], []).append(ev)
    for t, evs in sorted(by_time.items()):
        mins = int(round((t - now).total_seconds() / 60))
        title = f"⏰ {mins} 分钟后公布（{t.strftime('%H:%M')} JST）"
        body = "\n".join(B.fmt_event(e, with_result=False) for e in evs)
        res = {"sent": [], "skipped": ["dry_run"]} if dry_run else push(title, body, slot="reminder",
                                                                          data={"events": [_ser(e) for e in evs]})
        _log("reminder", title, res)
        S.mark_reminded(state, [e["id"] for e in evs])
        done["reminders"].append({"time": t.isoformat(), "n": len(evs), "push": res})

    reminded, sent_res = set(state.get("reminded", [])), set(state.get("results_sent", []))
    fresh = [e for e in events if e["id"] in reminded and e["id"] not in sent_res and B._val(e.get("pubVal"))
             and e.get("time_jst") and now - timedelta(minutes=RESULT_FOLLOWUP_MIN) <= e["time_jst"] <= now]
    if fresh:
        title = "📊 重大数据已公布"
        body = "\n".join(B.fmt_event(e) for e in fresh)
        res = {"sent": [], "skipped": ["dry_run"]} if dry_run else push(title, body, slot="result",
                                                                          data={"events": [_ser(e) for e in fresh]})
        _log("result", title, res)
        lst = state.setdefault("results_sent", [])
        lst.extend(e["id"] for e in fresh)
        del lst[:-500]
        done["results"] = [e["id"] for e in fresh]

    state["last_tick"] = now.isoformat(timespec="seconds")
    S.save_state(state)
    return done
