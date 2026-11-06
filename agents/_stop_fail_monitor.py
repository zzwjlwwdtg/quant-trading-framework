"""STOP 拒单和保护配置的离线只读监控；不查询券商，不影响交易。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
JST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")
FAIL = re.compile(r"\[trader\] STOP (FAIL|EXC)\s+(\S+)(?:\s+@\s+([\d.]+))?:\s*(.*)$")
ARMED = re.compile(r"\[trader\] (\S+) broker stop unavailable; armed software stop at \$([\d.]+)")


def instant(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def number(value):
    try:
        n = float(value)
        return n if not isinstance(value, bool) and math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def fresh(ts, now, max_age_seconds):
    dt = instant(ts)
    return dt is not None and 0 <= (now - dt).total_seconds() <= max_age_seconds


def parse_events(log_files, *, start, end):
    """固定起止区间，保留坏行分母；轮转/重复日志按事件内容去重。"""
    start, end = instant(start), instant(end)
    if start is None or end is None or start > end:
        raise ValueError("回放起止时间必须带时区且有序")
    events, armed, bad, errors = {}, [], [], []
    risk_monitor_evidence = None
    total, candidates, parsed = 0, 0, 0
    for file in sorted(map(Path, log_files)):
        try:
            with file.open(encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, 1):
                    total += 1
                    ref = f"{file.name}:{lineno}"
                    candidate = "[trader]" in line and ("STOP FAIL" in line or "STOP EXC" in line)
                    stamp = STAMP.match(line)
                    try:
                        ts = datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).astimezone(UTC) if stamp else None
                    except ValueError:
                        ts = None
                    if ts is not None and not start <= ts <= end:
                        continue
                    if ts is not None and "[risk-monitor]" in line:
                        record = {"ts": ts.isoformat(), "source": ref, "message": line.strip(),
                                  "proves_current_polling_health": False}
                        if risk_monitor_evidence is None or record["ts"] > risk_monitor_evidence["ts"]:
                            risk_monitor_evidence = record
                    if candidate:
                        candidates += 1
                        match = FAIL.search(line)
                        stop = number(match[3]) if match and match[3] else None
                        if ts is None or match is None or (match[1] == "FAIL" and (stop is None or stop <= 0)):
                            bad.append({"source": ref, "reason": "拒单行缺有效时间、标的或止损价"})
                            continue
                        parsed += 1
                        key = (ts.isoformat(), match[1], match[2], stop, match[4].strip())
                        event_id = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]
                        if event_id in events:
                            events[event_id]["sources"].append(ref)
                            continue
                        events[event_id] = {
                            "event_id": event_id, "ts": ts.isoformat(), "kind": match[1],
                            "ticker": match[2], "stop_price": stop, "message": match[4].strip(),
                            "sources": [ref], "software_armed_at_event": False,
                        }
                    match = ARMED.search(line)
                    if match and ts is not None:
                        armed.append((ts.isoformat(), match[1], number(match[2]), ref))
        except OSError as exc:
            errors.append({"source": file.name, "error": type(exc).__name__})
    # 同秒同标的同价的配置日志只证明配置，不证明轮询有效或仓位仍在。
    for event in events.values():
        matches = [r for r in armed if r[0] == event["ts"] and r[1] == event["ticker"] and (event["stop_price"] is None or r[2] == event["stop_price"])]
        event["software_armed_at_event"] = bool(matches)
        event["armed_sources"] = [r[3] for r in matches]
    return {
        "events": sorted(events.values(), key=lambda row: (row["ts"], row["event_id"])),
        "coverage": {"total_lines": total, "candidate_lines": candidates, "parsed_lines": parsed,
                     "unique_events": len(events), "parse_rate": parsed / candidates if candidates else None},
        "unparsed": bad, "source_errors": errors,
        "risk_monitor_evidence": risk_monitor_evidence,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
    }


def protection_rows(state, positions, *, state_observed_at, now, max_age_seconds=300):
    """配置状态与保护有效性分开。没有带时间的持仓证据不推断当前敞口。"""
    now = instant(now)
    if now is None or number(max_age_seconds) is None or max_age_seconds <= 0:
        raise ValueError("需要当前UTC时间和正的展示新鲜度窗口")
    if not isinstance(state, dict) or not isinstance(positions, dict):
        return []
    source_time = positions.get("observed_at")
    position_fresh = positions.get("moomoo_ok") is True and fresh(source_time, now, max_age_seconds)
    state_fresh = fresh(state_observed_at, now, max_age_seconds)
    grouped = {}
    for position in positions.get("positions") or []:
        if isinstance(position, dict) and position.get("ticker"):
            grouped.setdefault(str(position["ticker"]), []).append(position)
    tickers = set(grouped) | {str(t) for t in state if not str(t).startswith("__")}
    result = []
    for ticker in sorted(tickers):
        config = state.get(ticker)
        config = config if isinstance(config, dict) else {}
        mode = config.get("protective_stop_mode")
        rows = grouped.get(ticker, [])
        qty = number(rows[0].get("qty")) if len(rows) == 1 else None
        authoritative = (position_fresh and len(rows) == 1
                         and rows[0].get("source") in ("moomoo_live", "moomoo_manual")
                         and qty is not None and qty >= 0)
        reasons, status = [], "unknown"
        if not authoritative:
            reasons.append("持仓缺失、过期、重复或无带时间的券商来源")
        elif qty == 0:
            status = "flat"
        elif not state_fresh:
            reasons.append("保护配置快照过期或时间未知")
        elif mode == "software_triggered":
            status = "software_triggered"
        elif mode in ("software", "broker") and (number(config.get("protective_stop_price")) or 0) > 0:
            if mode != "broker" or config.get("protective_stop_order_id"):
                status = mode
            else:
                reasons.append("缺券商保护单编号")
        elif mode == "unprotected":
            status = "unprotected"
        else:
            reasons.append("缺可核实的保护配置，不能据此断言裸仓")
        result.append({
            "ticker": ticker, "status": status, "configured_mode": mode,
            "position_qty": qty if authoritative else None, "position_observed_at": source_time,
            "state_observed_at": state_observed_at, "stop_price": number(config.get("protective_stop_price")),
            "configuration_updated_at": config.get("protective_stop_updated_utc"),
            "protection_effectiveness": "unknown", "unknown_reasons": reasons,
        })
    return result


def event_markouts(event, observations):
    """仅接受精确历史时点的实测价；不插值，不使用当前价或模型价。"""
    triggered = instant(event.get("ts"))
    out = {}
    for minutes in (1, 5, 15):
        target = triggered + timedelta(minutes=minutes) if triggered else None
        usable = [r for r in observations if isinstance(r, dict) and r.get("ticker") == event.get("ticker")
                  and instant(r.get("ts")) == target and r.get("kind") == "observed"
                  and r.get("source") and (number(r.get("price")) or 0) > 0
                  and instant(r.get("available_ts")) is not None and instant(r["available_ts"]) <= target]
        # STOP FAIL 行没有当时可执行市场价，不能用规则止损线充当基准。
        baseline = event.get("market_observation") or {}
        base = number(baseline.get("price"))
        valid_base = (base is not None and base > 0 and baseline.get("kind") == "observed"
                      and baseline.get("source") and instant(baseline.get("ts")) == triggered
                      and instant(baseline.get("available_ts")) is not None
                      and instant(baseline["available_ts"]) <= triggered)
        known = len(usable) == 1 and valid_base
        out[str(minutes)] = {"return_pct": (number(usable[0]["price"]) / base - 1) * 100 if known else None,
                             "target_ts": target.isoformat() if target else None,
                             "source": usable[0]["source"] if known else None,
                             "missing_reason": None if known else "缺当时实测基准价或唯一的对应历史行情"}
    return out


def build_report(log_files, *, start, end, state=None, positions=None, state_observed_at=None, observations=()):
    report = parse_events(log_files, start=start, end=end)
    for event in report["events"]:
        event["markouts"] = event_markouts(event, observations)
    report["protection"] = protection_rows(state or {}, positions or {}, state_observed_at=state_observed_at, now=end)
    report["configuration_counts"] = dict(Counter(r["status"] for r in report["protection"]))
    report["markout_coverage"] = {str(m): {"known": sum(e["markouts"][str(m)]["return_pct"] is not None for e in report["events"]), "total": len(report["events"])} for m in (1, 5, 15)}
    report.update(proposal="EVO-20261105-1", read_only=True, generated_at=end,
                  note="拒单不等于无保护；保护配置不等于保护有效。无证据均保留未知。")
    return report


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def report_from_files(*, logs_dir, state_path, positions_path, now=None, days=30):
    """本地文件入口；不联网、不写盘。缓存文件mtime不能替代券商查询时间。"""
    now = now or datetime.now(UTC)
    end, start = now.isoformat(), (now - timedelta(days=days)).isoformat()
    paths = []
    for day in range(days + 1):
        filename = f"run_{(now.astimezone(JST) - timedelta(days=day)):%Y%m%d}.log"
        path = Path(logs_dir) / filename
        if path.exists():
            paths.append(path)
    try:
        state_time = datetime.fromtimestamp(Path(state_path).stat().st_mtime, UTC).isoformat()
    except OSError:
        state_time = None
    result = build_report(paths, start=start, end=end, state=read_json(state_path), positions=read_json(positions_path), state_observed_at=state_time)
    result["log_files"] = [p.name for p in paths]
    result["missing_log_files"] = days + 1 - len(paths)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, default=HERE / "logs")
    parser.add_argument("--state", type=Path, default=HERE / "trader_state.json")
    parser.add_argument("--positions", type=Path, default=HERE / ".webui_cache" / "positions_live.json")
    parser.add_argument("--output", type=Path, help="显式新报告路径；拒绝覆盖任何已有文件")
    args = parser.parse_args()
    report = report_from_files(logs_dir=args.logs_dir, state_path=args.state, positions_path=args.positions)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        # 唯一允许的生产写入目标；隔离报告写入交给调用者捕获stdout。
        if args.output.resolve() != (HERE / "signals" / "stop_fail_events.json").resolve():
            parser.error("输出仅允许本副本 signals/stop_fail_events.json")
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    else:
        print(encoded)


if __name__ == "__main__":
    main()
