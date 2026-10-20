"""Bounded, public single-symbol lookup. No broker, AI, orders or persistent state."""
from __future__ import annotations

import copy
import math
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import version
from zoneinfo import ZoneInfo

METHOD = "KURUMI_STOCK_DAILY_V1"
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-stock")
_lock = threading.Lock()
_cache: OrderedDict = OrderedDict()
_jobs = {}


def symbol(value: str) -> str:
    value = str(value).strip().upper()
    if re.fullmatch(r"(?:60[0135]\d{3}|688\d{3}|51\d{4}|56\d{4})", value):
        return value + ".SS"
    if re.fullmatch(r"(?:00[0123]\d{3}|30[01]\d{3}|15\d{4})", value):
        return value + ".SZ"
    if re.fullmatch(r"\d{6}\.(?:SH|SS|SZ)", value):
        raw, exchange = value.split(".")
        result = symbol(raw)
        if result != raw + (".SS" if exchange in ("SH", "SS") else ".SZ"):
            raise ValueError("invalid symbol/exchange")
        return result
    if value.startswith("US."):
        value = value[3:]
    if re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", value):
        return value.replace(".", "-")
    raise ValueError("unsupported symbol")


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def empty(ticker, status="UNAVAILABLE"):
    return {"version": 1, "method": METHOD, "scope": "PUBLIC_SINGLE_STOCK_DELAYED",
            "symbol": ticker, "status": status, "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "Yahoo Finance / yfinance 1.2.0", "source_policy": "FREE_PUBLIC_ONLY",
            "source_url": f"https://finance.yahoo.com/quote/{ticker}/", "quote": None, "daily": None}


def divergence(frame):
    """Two right-hand CLOSED bars confirm each pivot; never use future/unclosed bars."""
    result = {"state": "INSUFFICIENT", "first_date": None, "second_date": None,
              "confirmed_on": None, "first_high": None, "second_high": None,
              "first_macd": None, "second_macd": None}
    if len(frame) < 60:
        return result
    frame = frame.tail(60)
    peaks = []
    for i in range(2, len(frame) - 2):
        value = float(frame["high"].iloc[i])
        neighbors = frame["high"].iloc[i-2:i+3]
        if value == float(neighbors.max()) and sum(neighbors == value) == 1:
            if not peaks or i - peaks[-1] >= 5:
                peaks.append(i)
    result["state"] = "NOT_DETECTED"
    if len(peaks) < 2 or len(frame) - 1 - peaks[-1] > 10:
        return result
    a, b = peaks[-2:]
    high1, high2 = (float(frame["high"].iloc[i]) for i in (a, b))
    dif1, dif2 = (number(frame["macd_dif"].iloc[i]) for i in (a, b))
    if dif1 is None or dif2 is None:
        result["state"] = "INSUFFICIENT"
        return result
    if high2 >= high1 * 1.005 and dif2 < dif1 - max(abs(dif1) * 0.05, 1e-8):
        result.update(state="DETECTED", first_date=frame.index[a].date().isoformat(),
                      second_date=frame.index[b].date().isoformat(), confirmed_on=frame.index[b+2].date().isoformat(),
                      first_high=high1, second_high=high2,
                      first_macd=dif1, second_macd=dif2)
    return result


def analyze(ticker, daily, intraday, metadata, now=None):
    import numpy as np
    import pandas as pd
    from backtest_engine import add_indicators
    now = now or datetime.now(timezone.utc)
    cn = ticker.endswith((".SS", ".SZ"))
    zone = ZoneInfo("Asia/Shanghai" if cn else "America/New_York")
    local = now.astimezone(zone)
    result = empty(ticker)
    result["generated_at"] = now.isoformat()
    if daily is None or daily.empty or daily.index.tz is None or daily.index.has_duplicates:
        return result
    raw = daily.copy().sort_index()
    raw.index = raw.index.tz_convert(zone)
    if any(raw.index.date > local.date()):
        return result
    # Corporate-action session: don't invent a raw-close-based percent change.
    quote = None
    if intraday is not None and not intraday.empty and intraday.index.tz is not None and not intraday.index.has_duplicates:
        intra = intraday.sort_index().copy()
        intra.index = intra.index.tz_convert(zone)
        # Yahoo labels minutes at their START. Never quote the forming/future minute.
        intra = intra.loc[intra.index + pd.Timedelta(minutes=1) <= now]
        if intra.empty:
            intra = None
    else:
        intra = None
    if intra is not None:
        last = intra.index[-1]
        price = number(intra["Close"].iloc[-1])
        prior = raw.loc[raw.index.date < last.date()]
        prev = number(prior["Close"].iloc[-1]) if len(prior) else None
        today_action = raw.loc[raw.index.date == last.date()]
        action = any(number(v) not in (None, 0) for col in ("Stock Splits", "Dividends") if col in today_action for v in today_action[col])
        if price and price > 0 and prev and prev > 0 and last.date() <= local.date():
            volumes = pd.to_numeric(intra.loc[intra.index.date == last.date(), "Volume"], errors="coerce")
            volume = int(volumes.sum()) if len(volumes) and np.isfinite(volumes).all() and (volumes >= 0).all() else None
            quote = {"price": round(price, 6), "previous_close": None if action else round(prev, 6),
                     "change_pct": None if action else round((price / prev - 1) * 100, 4),
                     "as_of": (last + pd.Timedelta(minutes=1)).isoformat(), "session_date": last.date().isoformat(),
                     "volume": volume, "volume_unit": "SHARES", "volume_basis": "REGULAR_SESSION_1M_SUM_NOT_FULL_DAY",
                     "today": last.date() == local.date(), "age_seconds": max(0, int((now-(last+pd.Timedelta(minutes=1)).to_pydatetime()).total_seconds())),
                     "currency": "CNY" if cn else "USD", "delayed": True}
    # Only completed regular sessions. This intentionally does not guess holiday calendars.
    close_hour = 15 if cn else 16
    complete = [d < local.date() or d == local.date() and (local.hour, local.minute) >= (close_hour, 15) for d in raw.index.date]
    closed = raw.loc[complete].copy()
    if len(closed) >= 60 and 0 <= (local.date()-closed.index[-1].date()).days <= 7:
        cols = ["Open", "High", "Low", "Close", "Volume"]
        good = closed[cols].apply(pd.to_numeric, errors="coerce")
        if not np.isfinite(good.tail(60)).all().all() or not (good[["Open", "High", "Low", "Close"]].tail(60) > 0).all().all() or not (good.Volume.tail(60) >= 0).all():
            return result
        if not ((good.High >= good[["Open", "Close", "Low"]].max(axis=1)) & (good.Low <= good[["Open", "Close", "High"]].min(axis=1))).tail(60).all():
            return result
        # Reuse yfinance's mature corporate-action adjustment, and the project's existing pure indicator path.
        import yfinance as yf
        adjusted = yf.utils.auto_adjust(closed) if "Adj Close" in closed else closed
        if not np.isfinite(adjusted[["Open", "High", "Low", "Close"]].tail(60)).all().all() or not (adjusted[["Open", "High", "Low", "Close"]].tail(60) > 0).all().all():
            return result
        adjusted = adjusted.rename(columns={"Open":"open", "High":"high", "Low":"low", "Close":"close", "Volume":"volume"})
        calculated = add_indicators(adjusted)
        row, prev = calculated.iloc[-1], calculated.iloc[-2]
        repaired = int(closed["Repaired?"].tail(60).fillna(True).astype(bool).sum()) if "Repaired?" in closed else 0
        mean = float(closed.Volume.iloc[-21:-1].mean())
        ratio = None if repaired or mean <= 0 else round(float(closed.Volume.iloc[-1]) / mean, 4)
        div = divergence(calculated) if not repaired else {**divergence(calculated.iloc[:0]), "state":"INSUFFICIENT"}
        prior_high = float(calculated.high.iloc[-21:-1].max())
        breakout = bool(row.close > prior_high)
        above = bool(row.close > row.ma20 > row.ma50)
        macd = bool(row.macd_dif > row.macd_dea)
        volume_ok = ratio is not None and ratio >= 1.5
        eligible = above and macd and breakout and volume_ok and div["state"] == "NOT_DETECTED" and not repaired
        daily_volume = int(closed.Volume.iloc[-1])
        result["daily"] = {"as_of": closed.index[-1].date().isoformat(), "bars": len(closed),
            "adjustment": "YFINANCE_AUTO_ADJUST", "repaired_bars_60": repaired,
            "volume": daily_volume, "volume_unit":"SHARES", "mean_volume_20_prior": round(mean, 4),
            "volume_ratio": ratio, "volume_state": "UNAVAILABLE" if ratio is None else "EXPANDED" if volume_ok else "CONTRACTED" if ratio < 0.8 else "NORMAL",
            "rsi14_sma": number(row.rsi_14), "ma20": number(row.ma20), "ma50": number(row.ma50),
            "adjusted_close": number(row.close), "prior_high20": prior_high,
            "macd_dif": number(row.macd_dif), "macd_dea": number(row.macd_dea),
            "macd_cross": "GOLDEN" if prev.macd_dif <= prev.macd_dea and row.macd_dif > row.macd_dea else "DEATH" if prev.macd_dif >= prev.macd_dea and row.macd_dif < row.macd_dea else "NONE",
            "top_divergence": div, "buy_signal": "WATCH_ONLY" if eligible else "NONE",
            "checks": {"trend": above, "macd": macd, "breakout20": breakout, "volume": volume_ok},
            "not_a_trade_order": True}
    result["quote"] = quote
    result["status"] = "PARTIAL" if quote or result["daily"] else "UNAVAILABLE"
    return result


def collect(ticker):
    import yfinance as yf
    if version("yfinance") != "1.2.0":
        return empty(ticker)
    daily = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=False,
                                     repair=ticker.endswith((".SS", ".SZ")), timeout=5)
    try:
        intraday = yf.Ticker(ticker).history(period="1d", interval="1m", auto_adjust=False, prepost=False, timeout=5)
    except Exception:
        intraday = None  # A quote outage must not discard valid completed daily history.
    return analyze(ticker, daily, intraday, {})


def read(value):
    ticker = symbol(value)
    with _lock:
        # Reap ALL completed jobs; otherwise querying two symbols once starves new codes.
        for key, job in list(_jobs.items()):
            if not job.done():
                continue
            try:
                data = job.result()
            except Exception:
                data = empty(key)
            _cache[key] = (time.monotonic(), data)
            _cache.move_to_end(key)
            del _jobs[key]
        cached = _cache.get(ticker)
        age = time.monotonic()-cached[0] if cached else None
        ttl = 60 if cached and cached[1]["status"] != "UNAVAILABLE" else 30
        if (not cached or age >= ttl) and ticker not in _jobs and len(_jobs) < 2:
            _jobs[ticker] = _pool.submit(collect, ticker)
        if cached:
            _cache.move_to_end(ticker)
            result = copy.deepcopy(cached[1])
        else:
            result = empty(ticker, "PENDING" if ticker in _jobs else "BUSY")
        while len(_cache) > 32:
            _cache.popitem(last=False)
        result["cache"] = {"age_seconds": round(age, 3) if age is not None else None,
                           "stale": age is not None and age >= ttl, "refreshing": ticker in _jobs}
        return result
