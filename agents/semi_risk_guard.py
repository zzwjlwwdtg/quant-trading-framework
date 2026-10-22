"""半导体风险开关 (2026-10-03, 用户确认).

看多半导体的核心前提: "高利率只伤估值, 不伤盈利". 下面任一条件触发, 说明前提
可能被证伪 → 半导体 (thesis_config.semi_risk_guard.tickers) 的买入门槛自动提高
到 min_confidence (canonical 10 分制; 5 分制下即满分 5/5 才允许买入).

  earnings_revisions_down  大市值龙头的盈利预期 30 天内转为下调
                           (yfinance eps_trend: 今年 + 明年 EPS 一致预期, 市值加权)
  yield_5p5_real_rising    10 年期名义收益率 > 5.5% 且 10 年期实际利率 (TIPS) 60 日历日上行
  hy_spread_widening       高收益债利差 (ICE BofA HY OAS) 60 日历日走阔 ≥ 75bp

运行: 每天美股收盘后由 WebUI 看门狗白名单任务 (_semi_risk_guard.bat) 调用,
结果写 signals/semi_risk_guard.json. 读取方 thesis_config.semi_risk_guard_state().

数据降级 (2026-10-22): 任一条件缺数据时, 该条件沿用上一次的触发状态, 已开启的
开关不会因为数据源超时/缺数而自动解除; 承接的触发集合写入 carried. 已开启的
条件只有在重新有数据且未触发时才会被清除. 数据降级持续 STICKY_MAX_DAYS 天 →
每天告警一次要求人工复核 (不会自动解除). 只改买入门槛, 不卖出, 不改仓位.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

AGENTS = Path(__file__).resolve().parent
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

STATE_PATH = AGENTS / "signals" / "semi_risk_guard.json"
STALE_DAYS = 4
STICKY_MAX_DAYS = 14
# 标普 500 权重最大的公司 (盈利预期的代理; 市值加权)
MEGA_CAPS = ("NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSLA",
             "BRK-B", "JPM", "LLY", "WMT", "V", "ORCL", "XOM")
DEFAULT_CONDITIONS = {
    "earnings_revisions_down": {"metric": "mega_eps_revision_30d_pct", "op": "<", "threshold": -0.5},
    "yield_5p5_real_rising": {"metric": "dgs10_pct", "op": ">", "threshold": 5.5,
                              "and": {"metric": "dfii10_60d_delta_bps", "op": ">", "threshold": 0}},
    "hy_spread_widening": {"metric": "hy_oas_60d_delta_bps", "op": ">=", "threshold": 75},
}


def _cmp(actual, op, threshold) -> bool:
    a, t = float(actual), float(threshold)
    return {"<": a < t, "<=": a <= t, ">": a > t, ">=": a >= t}[op]


def evaluate(metrics: dict, conditions: dict | None = None) -> dict:
    """→ {triggered: [id], unknown: [id], active: bool, details}. 缺数据的条件记 unknown."""
    conditions = conditions or DEFAULT_CONDITIONS
    triggered, unknown, details = [], [], {}
    for cid, c in conditions.items():
        parts = [c] + ([c["and"]] if c.get("and") else [])
        vals = [metrics.get(p["metric"]) for p in parts]
        if any(v is None for v in vals):
            unknown.append(cid)
            details[cid] = {"status": "unknown"}
            continue
        hit = all(_cmp(v, p["op"], p["threshold"]) for v, p in zip(vals, parts))
        details[cid] = {"status": "triggered" if hit else "ok",
                        "values": {p["metric"]: v for v, p in zip(vals, parts)}}
        if hit:
            triggered.append(cid)
    return {"triggered": triggered, "unknown": unknown, "active": bool(triggered),
            "details": details}


def mega_eps_revision(trends: dict[str, dict], caps: dict[str, float]) -> float | None:
    """市值加权的 30 天 EPS 预期修正 (%). trends[tk] = {"cur": [今年, 明年], "ago30": [...]}"""
    num = den = 0.0
    for tk, t in trends.items():
        cap = caps.get(tk)
        revs = [(c / a - 1) * 100 for c, a in zip(t.get("cur", []), t.get("ago30", []))
                if c is not None and a is not None and a > 0]
        if not cap or not revs:
            continue
        num += cap * sum(revs) / len(revs)
        den += cap
    return round(num / den, 3) if den else None


# ---------------- 联网部分 (Windows 上运行) ----------------
def _fred_series(sid: str, limit: int = 120) -> list[tuple[str, float]]:
    import urllib.request
    from config import FRED_API_KEY
    if not FRED_API_KEY:
        return []
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
           f"&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit={limit}")
    with urllib.request.urlopen(url, timeout=20) as r:
        obs = json.loads(r.read()).get("observations", [])
    return [(o["date"], float(o["value"])) for o in obs if o.get("value") not in ("", ".", None)]


def _delta_bps(series: list[tuple[str, float]], days: int = 60) -> float | None:
    """最新值 - 最近一个不晚于 (最新日 - days) 的观测. 60 日历日, 不是 60 个观测."""
    if not series:
        return None
    try:
        latest = date.fromisoformat(str(series[0][0])[:10])
    except Exception:
        return None
    cutoff = latest - timedelta(days=days)
    for d, v in series[1:]:
        try:
            dd = date.fromisoformat(str(d)[:10])
        except Exception:
            continue
        if dd <= cutoff:
            return round((series[0][1] - v) * 100, 1)
    return None


def fetch_metrics() -> tuple[dict, dict]:
    metrics, status = {}, {}
    try:
        d10 = _fred_series("DGS10", 10)
        metrics["dgs10_pct"] = d10[0][1] if d10 else None
        status["DGS10"] = d10[0][0] if d10 else "unavailable"
    except Exception as e:
        status["DGS10"] = f"error: {e}"
    for sid, key in (("DFII10", "dfii10_60d_delta_bps"), ("BAMLH0A0HYM2", "hy_oas_60d_delta_bps")):
        try:
            s = _fred_series(sid, 120)
            metrics[key] = _delta_bps(s)
            if sid == "BAMLH0A0HYM2" and s:
                metrics["hy_oas_pct"] = s[0][1]
            status[sid] = s[0][0] if s else "unavailable"
        except Exception as e:
            status[sid] = f"error: {e}"
    try:
        import yfinance as yf
        trends, caps = {}, {}
        for tk in MEGA_CAPS:
            try:
                t = yf.Ticker(tk)
                et = t.get_eps_trend()
                rows = [r for r in ("0y", "+1y") if r in et.index]
                trends[tk] = {"cur": [float(et.loc[r, "current"]) for r in rows],
                              "ago30": [float(et.loc[r, "30daysAgo"]) for r in rows]}
                caps[tk] = float(t.fast_info["marketCap"])
            except Exception:
                continue
            time.sleep(0.5)
        metrics["mega_eps_revision_30d_pct"] = mega_eps_revision(trends, caps)
        status["eps_trend"] = f"{len(trends)}/{len(MEGA_CAPS)} tickers"
    except Exception as e:
        status["eps_trend"] = f"error: {e}"
    return metrics, status


# ---------------- 状态读写 / 降级承接 ----------------
def _last_good_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.stem}_last_good{state_path.suffix}")


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _ts(value) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(value))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _sticky_days(since, now: datetime) -> float | None:
    d = _ts(since)
    return round((now - d).total_seconds() / 86400, 1) if d else None


def _dd(day: float | None) -> str:
    return f"{day:.1f}d" if day is not None else "?"


def _notify_daily(event: str, msg: str, level: str) -> None:
    """持久化按 ET 日去重; notifications 不可用时退回普通 send_alert."""
    try:
        from notifications import send_alert, _alert_sent_today, _record_alert_sent
        if _alert_sent_today(event, level):
            return
        send_alert(msg, level=level, dedup=True)
        _record_alert_sent(event, level)
    except Exception:
        try:
            from notifications import send_alert
            send_alert(msg, level=level, dedup=True)
        except Exception as e:
            print(f"[notify] {e}")


def run(state_path: Path = STATE_PATH, fetch=fetch_metrics, notify=True) -> dict:
    from thesis_config import semi_risk_guard_config
    cfg = semi_risk_guard_config() or {}
    now = datetime.now(timezone.utc)
    prev = _read_state(state_path)
    if state_path.exists() and not prev:
        prev = _read_state(_last_good_path(state_path))
    prev_active = bool(prev.get("active"))
    prev_trig = [str(x) for x in (prev.get("triggered") or [])]

    metrics, status = fetch()
    ev = evaluate(metrics, cfg.get("conditions") or None)
    unknown = [str(x) for x in ev["unknown"]]
    carried = [cid for cid in unknown if cid in prev_trig]
    # 旧状态没有可识别的触发 id (或已是 sentinel) 时, 用 sentinel 持续承接;
    # sentinel 本身也必须可承接, 否则第二天缺数据会被误判为解除.
    if prev_active and unknown and not carried and (not prev_trig or "prior_active" in prev_trig):
        carried = ["prior_active"]
    triggered = sorted(set(ev["triggered"]) | set(carried))
    active = bool(triggered)

    sticky_since = prev.get("sticky_since") if carried else None
    if carried and not sticky_since:
        sticky_since = prev.get("ts")
    sticky_days = _sticky_days(sticky_since, now) if carried else None

    state = {"ts": now.isoformat(timespec="seconds"),
             "metrics": metrics, "data_status": status,
             "triggered": triggered, "unknown": unknown, "active": active,
             "observed_triggered": ev["triggered"], "details": ev["details"],
             "data_degraded": bool(unknown), "carried": carried,
             "carried_from": prev.get("ts") if carried else None,
             "sticky_since": sticky_since, "sticky_days": sticky_days,
             "sticky_max_days": STICKY_MAX_DAYS}
    _write_state(state_path, state)
    if not unknown:
        _write_state(_last_good_path(state_path), state)
    print(json.dumps(state, ensure_ascii=False))

    if notify:
        changed = prev_active != active or set(prev_trig) != set(triggered)
        min_conf = cfg.get("min_confidence")
        if carried:
            msg = (f"半导体风险开关 维持开启 (数据降级 {len(unknown)} 项): "
                   f"触发 {', '.join(triggered) or '无'}"
                   + (f" → 半导体买入门槛保持 min_confidence={min_conf}" if min_conf else ""))
            _notify_daily("semi_guard_carried", msg, "warning")
        elif prev and changed:
            try:
                from notifications import send_alert
                msg = (f"半导体风险开关 {'开启' if active else '解除'}: "
                       f"触发 {', '.join(triggered) or '无'}"
                       + (f" → 半导体买入门槛提高到 min_confidence={min_conf}"
                          if active and min_conf else ""))
                send_alert(msg, level="warning" if active else "recovery", dedup=True)
            except Exception as e:
                print(f"[notify] {e}")
        elif unknown:
            _notify_daily("semi_guard_data_incomplete",
                          f"半导体风险开关数据不完整 ({', '.join(unknown)}); "
                          "未触发条件保持不限制", "warning")
        if sticky_days is not None and sticky_days >= STICKY_MAX_DAYS:
            _notify_daily("semi_guard_manual_review",
                          f"半导体风险开关已因数据降级维持开启 {_dd(sticky_days)} "
                          f"(阈值 {STICKY_MAX_DAYS}d); 请人工复核数据源与持仓限制",
                          "warning")
    return state


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    # 后台无控制台运行时 stdout 可能丢失 → 异常写进自己的日志
    try:
        run()
    except Exception:
        import traceback
        log = AGENTS / "logs" / "semi_risk_guard_py.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"===== {datetime.now(timezone.utc).isoformat()} =====\n{traceback.format_exc()}\n")
        raise SystemExit(1)
