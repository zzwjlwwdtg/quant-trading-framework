"""Public, versioned market observation framework. No models, accounts or orders.

Mature yfinance handles data collection; this module only orchestrates fixed ETF
proxies and transparent research rules. It is NOT a validated trading strategy.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import SIGNALS_DIR

JST = timezone(timedelta(hours=9))
METHOD = "KURUMI_PUBLIC_MARKET_V1"
CACHE = Path(SIGNALS_DIR) / "public_market_outlook.json"
UNIVERSES = {
    "CN": {"zone": "Asia/Shanghai", "close_hour": 15, "benchmark": "510300.SS",
           "indices": {"510300.SS": "沪深300ETF代理", "510500.SS": "中证500ETF代理", "159915.SZ": "创业板ETF代理"},
           "sectors": {"512480.SS": "半导体", "515880.SS": "通信设备", "515790.SS": "光伏",
                       "512170.SS": "医疗", "515180.SS": "红利", "512800.SS": "银行"}},
    "US": {"zone": "America/New_York", "close_hour": 16, "benchmark": "SPY",
           "indices": {"SPY": "标普500ETF代理", "QQQ": "纳指100ETF代理", "IWM": "罗素2000ETF代理"},
           "sectors": {"SMH": "半导体", "XLI": "工业", "XLE": "能源", "XLF": "金融", "GLD": "黄金", "TLT": "长期美债"}},
}
LIMITATIONS = ["已完成日线，不是实时盘面；日期是报价日期，不是抓取日期。",
              "参与度仅为3个宽基/风格ETF代理，不是全市场涨跌家数；ETF量比不是资金净流入。",
              "方向为有限ETF池的观察候选；规则尚未验证预测收益，不代表买入信号或概率。",
              "A股启用行情库修复，重建日线数量另标；涉及重建的21日窗口不计算量比，不能冒充交易所正式日线。"]


def history(symbol: str):
    from importlib.metadata import version
    import yfinance as yf
    if version("yfinance") != "1.2.0":
        raise RuntimeError("Unreviewed public data component version")
    # Use the pinned component's finer-interval reconstruction, not local interpolation.
    return yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=True,
                                     repair=symbol.endswith((".SS", ".SZ")), timeout=8)


def observe(frame, now: datetime, region: str) -> dict | None:
    """Conservatively discard today's unfinished bar, bad rows and old sessions."""
    import pandas as pd
    spec = UNIVERSES[region]
    local = now.astimezone(ZoneInfo(spec["zone"]))
    if frame is None or frame.empty or "Close" not in frame:
        return None
    frame = frame.copy()
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_convert(ZoneInfo(spec["zone"]))
    dates = [x.date() for x in frame.index]
    mask = [d < local.date() or d == local.date() and (local.hour, local.minute) >= (spec["close_hour"], 15) for d in dates]
    frame = frame.loc[mask].sort_index()
    if frame.index.has_duplicates or len(frame) < 21:
        return None
    closes = pd.to_numeric(frame["Close"], errors="coerce")
    if not all(math.isfinite(float(x)) and x > 0 for x in closes.tail(21)):
        return None
    asof = frame.index[-1].date()
    # No invented Chinese/US holiday calendar. Long closures are conservatively unavailable.
    if not 0 <= (local.date() - asof).days <= 4:
        return None
    ma = float(closes.tail(20).mean())
    repaired = int(frame["Repaired?"].tail(21).fillna(True).astype(bool).sum()) if "Repaired?" in frame else 0
    vol_ratio = None
    # Reconstructed volume can differ materially from an eventual official daily bar.
    if "Volume" in frame and repaired == 0:
        volumes = pd.to_numeric(frame["Volume"], errors="coerce").tail(21)
        if all(math.isfinite(float(x)) and x >= 0 for x in volumes) and volumes.iloc[:-1].mean() > 0:
            vol_ratio = round(float(volumes.iloc[-1] / volumes.iloc[:-1].mean()), 2)
    window = "|".join(x.date().isoformat() for x in frame.index[-21:])
    return {"as_of": asof.isoformat(), "window_key": hashlib.sha256(window.encode()).hexdigest()[:24], "pct_5d": round((float(closes.iloc[-1] / closes.iloc[-6]) - 1) * 100, 2),
            "pct_20d": round((float(closes.iloc[-1] / closes.iloc[-21]) - 1) * 100, 2),
            "above_ma20": float(closes.iloc[-1]) > ma, "volume_ratio": vol_ratio,
            "repaired_bars_21d": repaired}


def empty(region: str) -> dict:
    return {"version": 1, "method": METHOD, "scope": "CLOSED_SESSION_ETF_PROXY_NOT_LIVE", "region": region,
            "status": "UNAVAILABLE", "generated_at": None, "expires_at": None, "as_of": None, "snapshot_id": None,
            "freshness": "UNKNOWN",
            "regime": "INSUFFICIENT", "benchmark": UNIVERSES[region]["benchmark"],
            "participation": {"above_ma20": 0, "observed": 0, "universe": 3}, "indices": [], "directions": [],
            "coverage": {"observed": 0, "universe": 9}, "limitations": LIMITATIONS,
            "source": "Yahoo Finance / yfinance 1.2.0", "source_policy": "FREE_PUBLIC_ONLY"}


def build(region: str, frames: dict, now: datetime) -> dict:
    import pandas as pd
    spec, result = UNIVERSES[region], empty(region)
    values = {}
    for symbol in list(spec["indices"]) + list(spec["sectors"]):
        try:
            row = observe(frames.get(symbol), now, region)
        except (ValueError, TypeError, KeyError):
            row = None
        if row:
            values[symbol] = row
    benchmark = values.get(spec["benchmark"])
    asof = benchmark["as_of"] if benchmark else None
    # Relative strength must use the same completed session, never mismatched market dates.
    aligned = {s: row for s, row in values.items() if asof and row["as_of"] == asof and row["window_key"] == benchmark["window_key"]}
    indices = [{"symbol": s, "name": name, **aligned[s]} for s, name in spec["indices"].items() if s in aligned]
    up = sum(row["above_ma20"] and row["pct_5d"] > 0 and row["pct_20d"] > 0 for row in indices)
    down = sum(not row["above_ma20"] and row["pct_5d"] < 0 and row["pct_20d"] < 0 for row in indices)
    regime = "INSUFFICIENT" if len(indices) < 3 else "RISK_ON" if up >= 2 else "RISK_OFF" if down >= 2 else "MIXED"
    candidates = []
    if benchmark and len(indices) == 3:
        for symbol, name in spec["sectors"].items():
            row = aligned.get(symbol)
            if not row:
                continue
            excess = round(row["pct_20d"] - benchmark["pct_20d"], 2)
            if not row["above_ma20"] or row["pct_5d"] <= 0 or excess <= 2:
                continue
            candidates.append({"symbol": symbol, "name": name, **row, "excess_20d_pp": excess,
                               "status": "WATCH_ONLY", "price_evidence": "ABOVE_MA20_POSITIVE_5D_RS20_GT_2PP",
                               "confirmation": "相对基准强度保持、20日线未失守，再核对成交量与公开催化；缺催化只算价格线索。",
                               "invalidation": "任一入选条件不再满足（跌回20日线、5日不再上涨、20日相对强度不超过2百分点）即撤销本轮候选。",
                               "catalyst_verified": False, "url": f"https://finance.yahoo.com/quote/{symbol}/history/"})
    candidates.sort(key=lambda row: row["excess_20d_pp"], reverse=True)
    local = now.astimezone(ZoneInfo(spec["zone"]))
    reference = pd.Timestamp(local.date())
    if local.weekday() >= 5 or (local.hour, local.minute) < (spec["close_hour"], 15):
        reference -= pd.offsets.BDay(1)
    # Weekday sanity check is conservative, NOT an exchange holiday calendar.
    fresh = "RECENT_CLOSED_SESSION" if asof == reference.date().isoformat() else "LAGGED_BACKGROUND" if asof else "UNKNOWN"
    result.update(status="OK" if len(aligned) == 9 and fresh == "RECENT_CLOSED_SESSION" else "PARTIAL" if aligned else "UNAVAILABLE", generated_at=now.isoformat(),
                  expires_at=(now + timedelta(minutes=35)).isoformat(), as_of=asof, regime=regime,
                  freshness=fresh,
                  participation={"above_ma20": sum(row["above_ma20"] for row in indices), "observed": len(indices), "universe": 3},
                  indices=indices, directions=candidates[:2] if fresh == "RECENT_CLOSED_SESSION" else [], coverage={"observed": len(aligned), "universe": 9})
    # Stable observation ID (not a future-price prediction). Same data cannot become a new claim.
    frozen = {k: result[k] for k in ("method", "region", "as_of", "freshness", "regime", "indices", "directions", "coverage")}
    result["snapshot_id"] = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()[:24]
    return result


def read(region: str = "CN", *, cache: Path = CACHE, now: datetime | None = None) -> dict:
    if region not in UNIVERSES:
        raise ValueError("region must be CN or US")
    now = now or datetime.now(JST)
    try:
        if cache.stat().st_size > 2_000_000:
            return empty(region)
        raw = json.loads(cache.read_text(encoding="utf-8"))[region]
        if raw["method"] != METHOD or raw["version"] != 1 or raw["region"] != region:
            return empty(region)
        generated, expires = datetime.fromisoformat(raw["generated_at"]), datetime.fromisoformat(raw["expires_at"])
        if generated.tzinfo is None or expires.tzinfo is None or generated > now + timedelta(seconds=5) or not timedelta(0) < expires - generated <= timedelta(minutes=35):
            return empty(region)
        if now >= expires:
            result = empty(region)
            result.update(status="STALE", generated_at=raw["generated_at"], expires_at=raw["expires_at"])
            return result
        # Fixed producer schema, no arbitrary cached fields or exception details leave this endpoint.
        result = {k: raw[k] for k in empty(region)}
        observation_keys = ("symbol", "name", "as_of", "pct_5d", "pct_20d", "above_ma20", "volume_ratio", "repaired_bars_21d")
        result["indices"] = [{k: row[k] for k in observation_keys} for row in raw["indices"][:3]]
        direction_keys = (*observation_keys, "excess_20d_pp", "status", "catalyst_verified", "url")
        result["directions"] = [{k: row[k] for k in direction_keys} for row in raw["directions"][:2]]
        result["participation"] = {k: raw["participation"][k] for k in ("above_ma20", "observed", "universe")}
        result["coverage"] = {k: raw["coverage"][k] for k in ("observed", "universe")}
        return result
    except (OSError, KeyError, ValueError, TypeError):
        return empty(region)


def refresh(*, cache: Path = CACHE, load=history, now: datetime | None = None) -> dict:
    cache.parent.mkdir(parents=True, exist_ok=True)
    lock = cache.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {"status": "BUSY"}  # Never steal a lock on an assumed timeout.
    try:
        symbols = [s for spec in UNIVERSES.values() for s in (*spec["indices"], *spec["sectors"])]
        def safe(symbol):
            try:
                return symbol, load(symbol)
            except Exception:
                return symbol, None
        with ThreadPoolExecutor(max_workers=4) as pool:
            frames = dict(pool.map(safe, symbols))
        registered = now or datetime.now(JST)  # Timestamp after collection, never backdated.
        data = {region: build(region, frames, registered) for region in UNIVERSES}
        previous = {}
        try:
            previous = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(previous, dict):
            previous = {}
        journal = cache.with_suffix(".history.jsonl")
        with journal.open("a", encoding="utf-8") as out:
            for region, record in data.items():
                if record["as_of"] and (previous.get(region) or {}).get("snapshot_id") != record["snapshot_id"]:
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
        temp = cache.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(cache)
        return {region: {"status": row["status"], "as_of": row["as_of"], "regime": row["regime"], "directions": len(row["directions"])} for region, row in data.items()}
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) == 2 else "read"
    if command == "tick":
        recent = read("CN")
        generated = recent.get("generated_at")
        result = {"status": "RECENT"} if generated and datetime.now(JST) - datetime.fromisoformat(generated) < timedelta(minutes=30) else refresh()
    elif command == "refresh":
        result = refresh()
    elif command == "read":
        result = {r: read(r) for r in UNIVERSES}
    else:
        raise SystemExit("read | refresh | tick")
    print(json.dumps(result, ensure_ascii=False))
