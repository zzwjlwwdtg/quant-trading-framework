"""Immutable public hypotheses and forward price-proxy evaluation, never trading.

Reuses backtest_engine.load_history (adjusted yfinance OHLC). First SPY-observed
regular open strictly after registration -> fifth paired session's completed close.
No claim of independent observations, causal validation, fills or net trading P&L.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import SIGNALS_DIR

DB = Path(SIGNALS_DIR) / "research_forecasts.sqlite3"
PROXIES = {"QQQ", "SMH", "XLE", "GLD", "TLT", "XLI", "XLF", "IWM"}
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
METHOD = "NEXT_OBSERVED_SPY_OPEN_TO_5TH_PAIRED_CLOSE_EXCESS_0.25PP_V1"
CAUTION = "前向样本，非历史挖掘；主题ETF代理、复权日线、未计成本。条件/因果未自动验证，样本可能相关，不能证明可交易优势。"


@contextmanager
def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS outcomes(id TEXT PRIMARY KEY REFERENCES forecasts(id),
        evaluated_at TEXT NOT NULL, body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS review_attempts(id TEXT PRIMARY KEY REFERENCES forecasts(id), checked_at TEXT NOT NULL);
      CREATE TRIGGER IF NOT EXISTS forecasts_immutable BEFORE UPDATE ON forecasts
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_FORECAST'); END;
      CREATE TRIGGER IF NOT EXISTS outcomes_immutable BEFORE UPDATE ON outcomes
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_OUTCOME'); END;
      CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_FORECAST'); END;
      CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_OUTCOME'); END;
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def register(note, *, now, path=None):
    """Server timestamp only; stable evidence/proxy key prevents rewriting a later opinion."""
    hypothesis = note.get("hypothesis") or {}
    proxy, stance = hypothesis.get("proxy"), hypothesis.get("stance")
    if (proxy not in PROXIES or proxy not in note.get("themes", []) or not note.get("evidence")
            or stance not in {"OUTPERFORM", "UNDERPERFORM"}
            or any(not isinstance(hypothesis.get(k), str) or not hypothesis[k].strip()
                   or len(hypothesis[k]) > 180 for k in ("thesis", "condition", "invalidation"))):
        return None
    if now.tzinfo is None or datetime.fromisoformat(note["published_at"]) > now:
        return None
    identifier = hashlib.sha256((METHOD + note["id"] + proxy).encode()).hexdigest()[:24]
    body = {"id": identifier, "created_at": now.astimezone(UTC).isoformat(),
            "evidence_id": note["id"], "source_url": note["url"], "published_at": note["published_at"],
            "source_title": note.get("title", "")[:200], "evidence_quote": note["evidence"][:200],
            "institution": note.get("institution", "原文未注明")[:80],
            "proxy": proxy, "benchmark": "SPY", "horizon_sessions": 5, "stance": stance,
            "thesis": hypothesis["thesis"], "condition": hypothesis["condition"],
            "invalidation": hypothesis["invalidation"], "confidence": "LOW_TO_MODERATE_UNCALIBRATED",
            "method": METHOD, "broadcast_status": "NOT_AUTOMATICALLY_SENT",
            "scope": "CONDITIONAL_ETF_PROXY_NOT_REPORT_AUTHOR_TARGET"}
    with _connect(path or DB) as db:
        # BEGIN IMMEDIATE serializes the daily cap and evidence dedupe across refreshes.
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT body FROM forecasts WHERE id=?", (identifier,)).fetchone()
        if previous:
            return json.loads(previous[0])
        day = now.astimezone(UTC).date().isoformat()
        if db.execute("SELECT COUNT(*) FROM forecasts WHERE created_at LIKE ?", (day + "%",)).fetchone()[0] >= 2:
            return None
        db.execute("INSERT INTO forecasts VALUES(?,?,?)", (identifier, body["created_at"], json.dumps(body, ensure_ascii=False)))
    return body


def evaluate(forecast, prices, *, now):
    """Pure scorer. Benchmark determines sessions; missing ticker dates never shift entry/exit."""
    created = datetime.fromisoformat(forecast["created_at"])
    if created.tzinfo is None or now.tzinfo is None or created > now:
        return None
    proxy = prices.get(forecast["proxy"], {})
    benchmark = prices.get("SPY", {})
    sessions = sorted(day for day in benchmark
                      if datetime.combine(datetime.fromisoformat(day).date(), time(9, 30), NY) > created)
    if len(sessions) < 5:
        return None
    days = sessions[:5]
    exit_time = datetime.combine(datetime.fromisoformat(days[-1]).date(), time(16), NY)
    # Conservatively wait until normal close +15min, including on early-close days.
    if now < exit_time + timedelta(minutes=15):
        return None
    try:
        for day in days:
            for rows in (proxy, benchmark):
                if any(not math.isfinite(float(rows[day][k])) or float(rows[day][k]) <= 0
                       for k in ("open", "close")):
                    return None
        ret = (proxy[days[-1]]["close"] / proxy[days[0]]["open"] - 1) * 100
        bench = (benchmark[days[-1]]["close"] / benchmark[days[0]]["open"] - 1) * 100
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    excess = round(ret - bench, 8)
    if not all(math.isfinite(x) for x in (ret, bench, excess)):
        return None
    signed = excess if forecast["stance"] == "OUTPERFORM" else -excess
    outcome = "FLAT" if abs(excess) < 0.25 else "HIT" if signed >= 0.25 else "MISS"
    return {"status": outcome, "entry_session": days[0], "exit_session": days[-1],
            "return_pct": round(ret, 4), "benchmark_return_pct": round(bench, 4),
            "entry_price": proxy[days[0]]["open"], "exit_price": proxy[days[-1]]["close"],
            "benchmark_entry_price": benchmark[days[0]]["open"], "benchmark_exit_price": benchmark[days[-1]]["close"],
            "price_source": "YFINANCE_ADJUSTED_DAILY_VIA_EXISTING_BACKTEST_LOADER",
            "excess_pp": round(excess, 4), "evaluated_at": now.astimezone(UTC).isoformat(),
            "condition_outcome": "NOT_EVALUATED", "metric": "DIRECTION_PRICE_PROXY_ONLY"}


def _prices(symbol):
    from backtest_engine import load_history
    frame = load_history(symbol, days=350)
    return {d.strftime("%Y-%m-%d"): {"open": float(row["open"]), "close": float(row["close"])}
            for d, row in frame.iterrows()}


def review_due(*, path=None, now=None, load_prices=None):
    """At most three records and four public price downloads; no AI or account access."""
    path, now = Path(path or DB), (now or datetime.now(UTC)).astimezone(UTC)
    if not path.exists():
        return {"evaluated": 0, "status": "NO_FORECASTS"}
    with _connect(path) as db:
        rows = db.execute("SELECT f.body FROM forecasts f LEFT JOIN outcomes o ON f.id=o.id "
                          "LEFT JOIN review_attempts a ON f.id=a.id "
                          "WHERE o.id IS NULL AND (a.checked_at IS NULL OR a.checked_at<=?) "
                          "ORDER BY COALESCE(a.checked_at,''),f.created_at LIMIT 3",
                          ((now - timedelta(hours=6)).isoformat(),)).fetchall()
        cache, completed = {}, 0
        for (raw,) in rows:
            row = json.loads(raw)
            # Five sessions cannot mature within 4 calendar days; avoid needless downloads.
            if now - datetime.fromisoformat(row["created_at"]) < timedelta(days=4):
                continue
            db.execute("INSERT INTO review_attempts VALUES(?,?) ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at",
                       (row["id"], now.isoformat()))
            if now - datetime.fromisoformat(row["created_at"]) > timedelta(days=60):
                result = {"status": "UNRESOLVED", "evaluated_at": now.isoformat(),
                          "condition_outcome": "NOT_EVALUATED", "metric": "DATA_NOT_AVAILABLE_WITHIN_60_DAYS"}
            else:
                try:
                    for symbol in ("SPY", row["proxy"]):
                        if symbol not in cache:
                            cache[symbol] = (load_prices or _prices)(symbol)
                    result = evaluate(row, cache, now=now)
                except Exception:
                    result = None  # Missing/failing data is never a win or loss.
            if result:
                db.execute("INSERT OR IGNORE INTO outcomes VALUES(?,?,?)",
                           (row["id"], result["evaluated_at"], json.dumps(result)))
                completed += 1
    return {"status": "OK", "evaluated": completed}


def read(*, path=None, now=None):
    """Read-only API. No file creation, outcome calculation, network, model or cursor write."""
    path, now = Path(path or DB), now or datetime.now(UTC)
    base = {"version": 1, "scope": "PUBLIC_FORECAST_REVIEW", "queried_at": now.isoformat(),
            "method": METHOD, "warning": CAUTION}
    if not path.exists():
        return {**base, "status": "NO_FORECASTS", "counts": {}, "recent": []}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            counts = {"total": db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]}
            for status, count in db.execute("SELECT json_extract(body,'$.status'),COUNT(*) FROM outcomes GROUP BY 1"):
                counts[status.lower()] = count
            counts["pending"] = counts["total"] - sum(counts.get(k, 0) for k in ("hit", "miss", "flat", "unresolved"))
            rows = db.execute("SELECT f.body,o.body FROM forecasts f LEFT JOIN outcomes o ON f.id=o.id "
                              "ORDER BY f.created_at DESC,f.id LIMIT 8").fetchall()
            recent = []
            for raw, outcome in rows:
                row = json.loads(raw)
                result = json.loads(outcome) if outcome else {"status": "PENDING"}
                recent.append({**row, "outcome": result})
        return {**base, "status": "OK", "counts": counts, "recent": recent}
    except (OSError, sqlite3.Error, ValueError):
        return {**base, "status": "UNAVAILABLE", "counts": {}, "recent": []}
