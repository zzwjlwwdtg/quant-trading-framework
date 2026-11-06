"""Public A-share industry index snapshots and close-to-close streaks."""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

import requests

from .sources import BJT, JST, UA

SOURCE_URL = "https://quote.eastmoney.com/center/boardlist.html#industry_board"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
DAILY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SECTOR_SLOTS = ("early", "midday")
MAX_ROWS = 3


def _get(url: str, params: dict) -> dict:
    response = requests.get(url, params=params, timeout=(3, 5),
                            headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
    response.raise_for_status()
    data = response.json().get("data")
    if not isinstance(data, dict):
        raise ValueError("industry index data missing")
    return data


def _number(raw) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("nonfinite market value")
    return value


def _quotes(descending: bool, now: datetime, slot: str) -> tuple[int, list[dict]]:
    data = _get(QUOTE_URL, {
        "pn": 1, "pz": 10, "po": int(descending), "np": 1, "fltt": 2,
        "fid": "f3", "fs": "m:90 t:2 f:!50", "fields": "f2,f3,f12,f14,f18,f124",
    })
    total = int(data["total"])
    rows = data["diff"]
    if total < MAX_ROWS or not isinstance(rows, list) or len(rows) != min(10, total):
        raise ValueError("incomplete industry index ranking")
    quotes = []
    for row in rows:
        code, name = str(row["f12"]), str(row["f14"]).strip()
        latest, previous, change = (_number(row[key]) for key in ("f2", "f18", "f3"))
        asof = datetime.fromtimestamp(_number(row["f124"]), tz=BJT)
        if not re.fullmatch(r"BK\d{4,6}", code) or not re.fullmatch(r"[\w·（）()、 -]{1,32}", name):
            raise ValueError("unexpected industry index identifier")
        if min(latest, previous) <= 0 or abs((latest / previous - 1) * 100 - change) > 0.02:
            raise ValueError("industry index price and percentage disagree")
        if asof.date() != now.date() or asof > now + timedelta(seconds=10):
            raise ValueError("industry quotes are not from this session")
        if slot == "midday":
            if not time(11, 30) <= asof.time() < time(13):
                raise ValueError("source quotes are outside the midday interval")
        elif asof.time() < time(9, 30) or now - asof > timedelta(minutes=10):
            raise ValueError("early-session industry quotes are stale")
        quotes.append({"code": code, "name": name, "last": latest, "previous": previous,
                       "change_pct": change, "asof": asof})
    changes = [row["change_pct"] for row in quotes]
    if changes != sorted(changes, reverse=descending) or len({row["code"] for row in quotes}) != len(quotes):
        raise ValueError("industry quotes are not ranked")
    return total, quotes


def _daily(secid: str, today: date) -> dict[date, float]:
    # Request only completed sessions. Some feeds also expose a provisional daily bar.
    data = _get(DAILY_URL, {
        "secid": secid, "klt": 101, "fqt": 0, "lmt": 120,
        "end": (today - timedelta(days=1)).strftime("%Y%m%d"),
        "fields1": "f1,f2,f3", "fields2": "f51,f52,f53",
    })
    if str(data["code"]) != secid.split(".")[1] or not isinstance(data["klines"], list):
        raise ValueError("unexpected industry index history")
    closes = {}
    for bar in data["klines"]:
        day, _, close = bar.split(",")
        session = date.fromisoformat(day)
        price = _number(close)
        if session >= today or price <= 0 or session in closes:
            raise ValueError("invalid daily close")
        closes[session] = price
    return closes


def streak(quote: dict, closes: dict[date, float], sessions: list[date]) -> dict | None:
    if not sessions or sessions[-1] not in closes or not math.isclose(
            closes[sessions[-1]], quote["previous"], rel_tol=0, abs_tol=0.011):
        return None
    if quote["change_pct"] == 0:
        return {"days": 0, "cumulative_pct": 0.0, "partial": False}
    direction = 1 if quote["change_pct"] > 0 else -1
    days, base = 1, closes[sessions[-1]]
    for day in reversed(sessions[:-1]):
        close = closes.get(day)
        if close is None:
            break
        if (base - close) * direction <= 0:
            return {"days": days, "cumulative_pct": (quote["last"] / base - 1) * 100, "partial": False}
        base = close
        days += 1
    return {"days": days, "cumulative_pct": (quote["last"] / base - 1) * 100, "partial": True}


def build(now: datetime, slot: str) -> dict:
    now = now.astimezone(BJT)
    if slot not in SECTOR_SLOTS:
        raise ValueError("unsupported industry snapshot slot")
    clock = now.time()
    if now.weekday() >= 5 or not (time(9, 30) <= clock < time(11, 30) if slot == "early"
                                 else time(11, 30) <= clock < time(13)):
        return {"status": "UNAVAILABLE", "reason": "outside_session"}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            top, bottom = list(pool.map(lambda order: _quotes(order, now, slot), (True, False)))
            if top[0] != bottom[0] or top[1][0]["change_pct"] < bottom[1][0]["change_pct"]:
                raise ValueError("industry rankings disagree")
            snapshots = [row["asof"] for _, quotes in (top, bottom) for row in quotes]
            if max(snapshots) - min(snapshots) > timedelta(minutes=2):
                raise ValueError("industry rankings are out of sync")
            up = [row for row in top[1] if row["change_pct"] > 0][:MAX_ROWS]
            down = [row for row in bottom[1] if row["change_pct"] < 0][:MAX_ROWS]

            def history(secid: str) -> dict[date, float]:
                try:
                    return _daily(secid, now.date())
                except (requests.RequestException, AttributeError, ValueError, KeyError, TypeError):
                    return {}

            rows = up + down
            daily = list(pool.map(history, ["1.000001", *(f"90.{row['code']}" for row in rows)]))
        sessions = sorted(daily[0])
        for row, closes in zip(rows, daily[1:], strict=True):
            row["streak"] = streak(row, closes, sessions)
            row["asof"] = row["asof"].astimezone(JST).isoformat(timespec="seconds")
    except (requests.RequestException, AttributeError, ValueError, KeyError, TypeError, IndexError, OverflowError):
        return {"status": "UNAVAILABLE", "reason": "quote_snapshot_unverified"}
    return {"status": "READY", "slot": slot, "source": SOURCE_URL, "universe_size": top[0],
            "asof": min(snapshots).astimezone(JST).isoformat(timespec="seconds"),
            "latest_asof": max(snapshots).astimezone(JST).isoformat(timespec="seconds"),
            "up": up, "down": down}


def render(data: dict) -> list[str]:
    if data["status"] != "READY":
        return ["板块行情暂缺，等本时段的有效快照。"]
    earliest, latest = (datetime.fromisoformat(data[k]).strftime("%H:%M:%S") for k in ("asof", "latest_asof"))
    stamp = earliest if earliest == latest else f"{earliest} 至 {latest}"
    timing = "上午收盘为日本时间 12:30。" if data["slot"] == "midday" else ""
    lines = [f"{timing}行情源更新时间为日本时间 {stamp}，覆盖东方财富 {data['universe_size']} 个行业板块。"]
    for key, heading, direction in (("up", "领涨板块", "涨"), ("down", "领跌板块", "跌")):
        lines += ["", f"### {heading}"]
        if not data[key]:
            lines.append("本时点暂无上涨板块。" if key == "up" else "本时点暂无下跌板块。")
        for row in data[key]:
            item = f"- {row['name']}  当日 {row['change_pct']:+.2f}%"
            run = row["streak"]
            if run is None:
                item += "｜连涨跌与累计待核实"
            elif run["partial"]:
                item += f"｜至少连{direction} {run['days']} 日｜已覆盖累计 {run['cumulative_pct']:+.2f}%"
            else:
                item += f"｜连{direction} {run['days']} 日｜区间累计 {run['cumulative_pct']:+.2f}%"
            lines.append(item)
    lines += ["", "### 统计口径",
              "连续天数按交易日计算，计入本次盘中快照。累计以本段起点前一交易日收盘价为基准。",
              f"来源 [东方财富行业行情]({data['source']})"]
    return lines
