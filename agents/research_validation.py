"""Purged walk-forward validation for fixed, explainable strategy rules."""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable


@dataclass(frozen=True)
class WalkForwardSplit:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def purged_walk_forward_splits(
    n_samples: int,
    *,
    train_size: int = 160,
    test_size: int = 40,
    purge: int = 7,
    embargo: int = 3,
    anchored: bool = True,
) -> list[WalkForwardSplit]:
    """Create chronological train/test folds with purge and embargo gaps."""
    if min(n_samples, train_size, test_size) <= 0:
        return []
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be non-negative")
    splits: list[WalkForwardSplit] = []
    train_end = train_size
    while True:
        test_start = train_end + purge
        test_end = min(n_samples, test_start + test_size)
        if test_end - test_start < max(10, test_size // 2):
            break
        train_start = 0 if anchored else max(0, train_end - train_size)
        splits.append(WalkForwardSplit(train_start, train_end, test_start, test_end))
        train_end += test_size + embargo
        if train_end + purge >= n_samples:
            break
    return splits


def _stats(trades: list[dict], hold: int) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0.0, "avg_ret": 0.0, "sharpe": 0.0}
    returns = [float(x["ret"]) for x in trades]
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / max(1, len(returns) - 1)
    std = math.sqrt(variance)
    return {
        "n": len(trades),
        "win_rate": sum(1 for x in trades if x["win"]) / len(trades) * 100,
        "avg_ret": mean,
        "sharpe": mean / std * math.sqrt(252 / max(1, hold)) if std > 0 else 0.0,
    }


# F06 准入 (2026-09-30): 旧门槛 "≥5 笔 + 胜率≥52%" 不足以区分运气与规则有效.
MIN_OOS_TRADES = 20          # 独立 (不重叠) 样本数
WILSON_Z = 1.645             # 单侧 95%
MIN_FOLD_STABILITY = 0.5


def _wilson_lower(wins: float, n: int, z: float = WILSON_Z) -> float:
    """胜率的 Wilson 置信下界 (0~1)."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def _admission(aggregate: dict, stability: float) -> tuple[bool, dict]:
    """返回 (是否通过, 明细). 所有条件必须同时满足."""
    n = int(aggregate.get("n", 0))
    wins = round(aggregate.get("win_rate", 0.0) / 100.0 * n)
    lower = _wilson_lower(wins, n) * 100
    checks = {
        "n": n >= MIN_OOS_TRADES,
        "win_rate_lower_bound": lower > 50.0,
        "avg_ret": aggregate.get("avg_ret", 0.0) > 0,
        "fold_stability": stability >= MIN_FOLD_STABILITY,
    }
    info = {
        "min_trades": MIN_OOS_TRADES,
        "win_rate_lower_bound": round(lower, 1),
        "failed": [k for k, ok in checks.items() if not ok],
    }
    return all(checks.values()), info


def evaluate_rule_walk_forward(
    rule: dict,
    rows,
    check_rule: Callable,
    *,
    train_size: int | None = None,
    test_size: int = 40,
    embargo: int = 3,
) -> dict:
    """Evaluate a fixed rule only on purged chronological OOS folds."""
    n = len(rows)
    hold = max(1, int(rule.get("hold", 1)))
    if train_size is None:
        train_size = min(160, max(60, n // 2))
    splits = purged_walk_forward_splits(
        n,
        train_size=train_size,
        test_size=min(test_size, max(20, n // 5)),
        purge=hold,
        embargo=max(embargo, hold // 2),
    )
    action = rule.get("action") or ""
    is_bull = action in {"WATCH_BUY", "BUY", "WATCH_BUY_PROBE", "ADD", "PROBE"}
    # F06 fix (2026-09-19, audit): REDUCE/EXIT/SELL 是多头减仓, 不等于开空.
    # 之前 avg_ret 用原始 raw return, REDUCE 后价格跌 → ret<0 → 平均 avg_ret 负,
    # 但方向 100% 命中 (win_rate 高), 出现 "胜率 100%, 收益负" 的矛盾.
    # Fix: reduce 动作用 effective_ret = -ret (避损收益), 与 win 定义对齐.
    from trading_contracts import REDUCE_ACTIONS, SELL_ACTIONS   # R07 单一来源
    is_reduce = action in (REDUCE_ACTIONS | SELL_ACTIONS)
    folds = []
    all_oos: list[dict] = []
    for split in splits:
        trades = []
        # F06 (2026-09-30): 标签 (i+hold 的价格) 必须仍在本测试段内, 不能读到 test_end 之后.
        last_entry = min(split.test_end, n) - hold
        # F06: 持有期内不再开新样本 → 样本互不重叠, n 是独立样本数.
        next_allowed = split.test_start
        for i in range(split.test_start, max(split.test_start, last_entry)):
            if i < next_allowed:
                continue
            row = rows.iloc[i] if hasattr(rows, "iloc") else rows[i]
            if not check_rule(row, rule):
                continue
            next_allowed = i + hold
            future = rows.iloc[i + hold] if hasattr(rows, "iloc") else rows[i + hold]
            entry = float(row["close"])
            exit_price = float(future["close"])
            raw_ret = (exit_price - entry) / entry * 100
            # 方向判定: bull → ret > 0 是命中; reduce → ret < 0 是命中 (成功预警下跌)
            if is_bull:
                win = raw_ret > 0
                effective_ret = raw_ret
            elif is_reduce:
                win = raw_ret < 0
                effective_ret = -raw_ret   # avoided loss = negated raw return
            else:
                # HOLD/CAUTION/未知 action: 只跟踪方向对错的 raw movement
                win = raw_ret > 0
                effective_ret = raw_ret
            date_value = row.get("time_key") if hasattr(row, "get") else None
            trades.append({
                "index":         i,
                "date":          str(getattr(date_value, "date", lambda: date_value)()),
                "ret":           effective_ret,   # 用 effective 让 win 与 avg 方向一致
                "raw_ret":       raw_ret,          # 保留原始价格变化供审计
                "win":           win,
                "action_class":  "bull" if is_bull else ("reduce" if is_reduce else "neutral"),
            })
        fold_stats = _stats(trades, hold)
        fold_stats.update({
            "train": [split.train_start, split.train_end],
            "test": [split.test_start, split.test_end],
        })
        folds.append(fold_stats)
        all_oos.extend(trades)
    aggregate = _stats(all_oos, hold)
    positive_folds = sum(1 for fold in folds if fold["n"] > 0 and fold["win_rate"] >= 50)
    active_folds = sum(1 for fold in folds if fold["n"] > 0)
    stability = positive_folds / active_folds if active_folds else 0.0
    passed, admission = _admission(aggregate, stability)
    return {
        "method": "purged_walk_forward",
        "purge_days": hold,
        "embargo_days": max(embargo, hold // 2),
        "fold_count": len(folds),
        "active_fold_count": active_folds,
        "oos_n": aggregate["n"],
        "oos_win_rate": round(aggregate["win_rate"], 1),
        "oos_avg_ret": round(aggregate["avg_ret"], 3),
        "oos_sharpe": round(aggregate["sharpe"], 2),
        "fold_stability": round(stability, 3),
        "passed": passed,
        "admission": admission,
        "sampling": "non_overlapping",
        # 规则参数固定, 训练段不参与拟合, 只用于 purge/embargo 定位 (如实标注)
        "train_used_for_fitting": False,
        "folds": folds,
    }


# EVO-20261028-1：以下接口仅消费离线历史规则重放材料，不导入交易模块。
# 协议须由研究协调者在开发前持久化并取得登记凭据；构造字典不等于完成登记。
OVERSOLD_SPEC = {
    "proposal": "EVO-20261028-1",
    "train": ["2022-01-01", "2024-12-31"],
    "historical_oos": ["2025-01-01", "2026-09-30"],
    "rsi": "已完成且事件时可得的日线，Wilder RSI(14)<=30",
    "merge": "同标的相邻事件间隔<=5个交易日时合并，保留首个事件",
    "entry": "触发后下一可交易分钟的开盘",
    "primary": "入场所在交易日起第三个交易日收盘，减同期板块收益",
    "descriptive_days": [1, 5],
    "matching": "同标的、同开盘后分钟，按前20个日收益波动率最近邻，无放回",
    "matching_order": "超卖事件按时间和事件编号排序，距离相同按对照事件编号排序",
    "minimum": {"total": 60, "oversold": 20, "control": 20},
    "inference": "配对样本均值差，标的及UTC交易周双向聚类，双侧95%区间",
    "pass": "两期均值差>0且区间下限>0；任一期排除比例>10%不通过",
    "execution": "统计收益不证明可执行卖出改进，不授权任何交易改动",
}


def oversold_digest(value) -> str:
    """可复核的规范 JSON 摘要；拒绝 NaN，避免不同环境产生不同协议。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _study_time(value) -> datetime:
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("研究时间必须带时区")
    return value.astimezone(timezone.utc)


def make_oversold_protocol(
    *, registered_at: str, development_started_at: str,
    registration_receipt: str, data_version: str, data_sha256: str,
    code_version: str, calendar_version: str, rule_versions: list[str],
    historical_oos_used: bool, prospective_end: str | None = None,
) -> dict:
    """构造待归档协议，不写文件、不补造登记时间或登记凭据。

    data_sha256 固定完整输入快照。前瞻研究应先登记数据源/基线快照，
    再以 baseline_sha256 追踪后续只追加的数据，不能事后重选历史样本。
    登记凭据及历史规则全量重放的真实性仍须外部审计。
    """
    registered = _study_time(registered_at)
    if registered >= _study_time(development_started_at):
        raise ValueError("缺少开发前登记")
    if not isinstance(historical_oos_used, bool):
        raise ValueError("必须明确声明历史样本外是否用于调参")
    if not all((registration_receipt, data_version, code_version,
                calendar_version, rule_versions)):
        raise ValueError("登记凭据、数据、代码、日历和规则版本不能为空")
    if len(data_sha256) != 64 or any(c not in "0123456789abcdef" for c in data_sha256):
        raise ValueError("数据摘要必须为 SHA256")
    oos = list(OVERSOLD_SPEC["historical_oos"])
    if historical_oos_used:
        # 使用登记后完整日期，排除登记当天已经观察到的行情。
        start = (registered.date() + timedelta(days=1)).isoformat()
        if prospective_end is None or date.fromisoformat(prospective_end) <= date.fromisoformat(start):
            raise ValueError("前瞻期终点必须随协议预先登记")
        oos = [start, prospective_end]
    protocol = {
        "spec": json.loads(json.dumps(OVERSOLD_SPEC, ensure_ascii=False)),
        "registered_at": registered_at,
        "development_started_at": development_started_at,
        "registration_receipt": registration_receipt,
        "data_version": data_version, "data_sha256": data_sha256,
        "code_version": code_version, "calendar_version": calendar_version,
        "rule_versions": sorted(set(rule_versions)),
        "historical_oos_used": historical_oos_used, "oos": oos,
    }
    protocol["protocol_sha256"] = oversold_digest(protocol)
    return protocol


def _daily_state(rows: list[dict], trigger: datetime) -> tuple[float, float]:
    """只使用触发前已完成且当时已发布的日线，初始化后按 Wilder 平滑。"""
    usable = []
    for row in rows:
        close_ts = _study_time(row["close_ts"])
        available = _study_time(row["available_at"])
        if available < close_ts:
            raise ValueError("日线可得时间早于收盘")
        if close_ts <= trigger and available <= trigger:
            close = float(row["close"])
            if not math.isfinite(close) or close <= 0:
                raise ValueError("日线价格无效")
            usable.append((close_ts, close))
    usable.sort()
    if len(usable) < 21 or len({t for t, _ in usable}) != len(usable):
        raise ValueError("日线不足或重复")
    closes = [p for _, p in usable]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(x, 0) for x in changes[:14]) / 14
    loss = sum(max(-x, 0) for x in changes[:14]) / 14
    for change in changes[14:]:
        gain = (gain * 13 + max(change, 0)) / 14
        loss = (loss * 13 + max(-change, 0)) / 14
    rsi = 100 - 100 / (1 + gain / loss) if loss else (100.0 if gain else 50.0)
    returns = [b / a - 1 for a, b in zip(closes[-21:], closes[-20:])]
    mean = sum(returns) / 20
    vol = math.sqrt(sum((x - mean) ** 2 for x in returns) / 19)
    return rsi, vol


def _study_calendar(rows: list[dict]) -> list[dict]:
    sessions = []
    for row in rows:
        start, end = _study_time(row["open"]), _study_time(row["close"])
        day = date.fromisoformat(row["date"])
        if start >= end or (end - start).total_seconds() % 60:
            raise ValueError("交易日历分钟边界无效")
        if start.second or start.microsecond or end.second or end.microsecond:
            raise ValueError("交易日历不是整分钟")
        sessions.append({"date": day.isoformat(), "open": start, "close": end})
    if not sessions or any(
        a["date"] >= b["date"] or a["close"] >= b["open"]
        for a, b in zip(sessions, sessions[1:])
    ):
        raise ValueError("交易日历重复或未排序")
    return sessions


def _study_entry(trigger: datetime, sessions: list[dict]) -> tuple[int, datetime]:
    """分钟时间戳表示该分钟开盘；严格晚于触发时刻。"""
    for index, session in enumerate(sessions):
        if trigger < session["open"]:
            return index, session["open"]
        if session["open"] <= trigger < session["close"]:
            minute = trigger.replace(second=0, microsecond=0) + timedelta(minutes=1)
            if minute < session["close"]:
                return index, minute
    raise ValueError("日历缺少下一可交易分钟")


def _event_outcome(row: dict, trigger: datetime, sessions: list[dict],
                   index: int, entry: datetime) -> dict:
    """验证主窗口完整分钟路径；第一、第五日仅描述，不参与择优。"""
    if row.get("minute_reliable") is not True or not row.get("sector_benchmark"):
        raise ValueError("缺少可靠分钟行情或板块基准")
    rsi, vol = _daily_state(row["daily"], trigger)
    bars = {}
    for bar in row["minutes"]:
        ts = _study_time(bar["ts"])
        if ts in bars:
            raise ValueError("分钟行情重复")
        bars[ts] = bar
    entry_bar = bars.get(entry)
    if entry_bar is None:
        raise ValueError("缺少下一可交易分钟")
    price = float(entry_bar["open"])
    benchmark = float(entry_bar["benchmark_open"])
    if not all(math.isfinite(x) and x > 0 for x in (price, benchmark)):
        raise ValueError("入场价格无效")
    paths, returns, maes = [], {}, {}
    worst = 0.0
    for day in range(1, 6):
        if index + day > len(sessions):
            if day <= 3:
                raise ValueError("主窗口日历不足")
            break
        session = sessions[index + day - 1]
        minute = entry if day == 1 else session["open"]
        day_path = []
        complete = True
        while minute < session["close"]:
            bar = bars.get(minute)
            try:
                if bar is None:
                    raise ValueError
                opening, low, closing, sector_open, sector_close = (
                    float(bar[k]) for k in
                    ("open", "low", "close", "benchmark_open", "benchmark_close")
                )
                if (not all(math.isfinite(x) and x > 0 for x in
                            (opening, low, closing, sector_open, sector_close))
                        or low > min(opening, closing)):
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError):
                complete = False
                break
            worst = min(worst, (low / price - 1) * 100)
            day_path.append({
                "ts": minute.isoformat(), "open": opening, "low": low,
                "close": closing, "benchmark_open": sector_open,
                "benchmark_close": sector_close,
            })
            minute += timedelta(minutes=1)
        if not complete or not day_path:
            if day <= 3:
                raise ValueError("主窗口分钟路径缺失或价格无效")
            break
        paths.extend(day_path)
        if day in (1, 3, 5):
            last = day_path[-1]
            raw = (last["close"] / price - 1) * 100
            sector = (last["benchmark_close"] / benchmark - 1) * 100
            returns[str(day)] = {
                "raw_pct": raw, "sector_pct": sector, "excess_pct": raw - sector,
            }
            maes[str(day)] = worst
    return {
        "event_id": row["event_id"], "ticker": row["ticker"],
        "trigger_ts": trigger.isoformat(), "entry_ts": entry.isoformat(),
        "week": (trigger.date() - timedelta(days=trigger.weekday())).isoformat(),
        "minute_of_session": int((entry - sessions[index]["open"]).total_seconds() // 60),
        "rsi14": rsi, "oversold": rsi <= 30, "volatility20": vol,
        "returns": returns, "mae_pct": maes, "path": paths,
        "sector_benchmark": row["sector_benchmark"],
    }


def _cluster_interval(pairs: list[tuple[dict, dict]]) -> dict:
    """双向聚类夹心方差；负方差或聚类不足时不生成可通过的区间。"""
    n = len(pairs)
    if n < 2:
        return {"difference_pct": None, "ci95": None}
    mean_a = sum(a["returns"]["3"]["excess_pct"] for a, _ in pairs) / n
    mean_b = sum(b["returns"]["3"]["excess_pct"] for _, b in pairs) / n
    observations = []
    for a, b in pairs:
        observations.extend([
            (a, (a["returns"]["3"]["excess_pct"] - mean_a) / n),
            (b, -(b["returns"]["3"]["excess_pct"] - mean_b) / n),
        ])

    def component(keys):
        groups = defaultdict(float)
        for row, influence in observations:
            groups[tuple(row[k] for k in keys)] += influence
        count = len(groups)
        if count < 2:
            return None, count
        return count / (count - 1) * sum(x * x for x in groups.values()), count

    ticker, nt = component(("ticker",))
    week, nw = component(("week",))
    intersection, _ = component(("ticker", "week"))
    result = {"difference_pct": mean_a - mean_b, "ci95": None,
              "ticker_clusters": nt, "week_clusters": nw}
    if None in (ticker, week, intersection):
        return result
    variance = ticker + week - intersection
    if variance < 0 or not math.isfinite(variance):
        return result
    # Student t 双侧95%分位数向上取整；超过30自由度仍使用30的保守值。
    criticals = (
        12.707, 4.303, 3.183, 2.777, 2.571, 2.447, 2.365, 2.307,
        2.263, 2.229, 2.201, 2.179, 2.161, 2.145, 2.132, 2.120,
        2.110, 2.101, 2.094, 2.086, 2.080, 2.074, 2.069, 2.064,
        2.060, 2.056, 2.052, 2.049, 2.046, 2.043,
    )
    margin = criticals[min(nt - 1, nw - 1, 30) - 1] * math.sqrt(variance)
    result["ci95"] = [mean_a - mean_b - margin, mean_a - mean_b + margin]
    return result


def _study_period(inventory, events, sessions, bounds) -> dict:
    selected = [r for r in inventory if bounds[0] <= r["event_date"] <= bounds[1]]
    excluded, merged, candidates = [], [], []
    last_session = {}
    prepared = []
    for item in selected:
        row = events[item["event_id"]]
        try:
            trigger = _study_time(row["trigger_ts"])
            index, entry = _study_entry(trigger, sessions)
            event_session = next(
                (i for i, s in enumerate(sessions) if s["date"] == item["event_date"]), None)
            if event_session is None:
                raise ValueError("事件日期不在交易日历")
            # 正常交易时段内的触发必须与重放清单的交易日一致。
            containing = next(
                (s for s in sessions if s["open"] <= trigger < s["close"]), None)
            if containing is None or containing["date"] != item["event_date"]:
                raise ValueError("触发时间不在登记的交易时段")
            prepared.append((trigger, item["event_id"], event_session, index, entry))
        except (KeyError, TypeError, ValueError, StopIteration, OverflowError) as exc:
            excluded.append({"event_id": item["event_id"], "reason": str(exc)})
    for trigger, eid, event_session, index, entry in sorted(prepared):
        row = events[eid]
        previous = last_session.get(row["ticker"])
        last_session[row["ticker"]] = event_session
        if previous is not None and event_session - previous <= 5:
            merged.append(eid)
            continue
        try:
            outcome = _event_outcome(row, trigger, sessions, index, entry)
            # 不读取预注册期终点之后的标签，边界事件明确列为排除。
            if sessions[index + 2]["date"] > bounds[1]:
                raise ValueError("第三日标签越过研究期边界")
            if index + 4 < len(sessions) and sessions[index + 4]["date"] > bounds[1]:
                outcome["returns"].pop("5", None)
                outcome["mae_pct"].pop("5", None)
                end = sessions[index + 2]["close"]
                outcome["path"] = [
                    b for b in outcome["path"] if _study_time(b["ts"]) < end
                ]
            candidates.append(outcome)
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            excluded.append({"event_id": eid, "reason": str(exc)})
    treated = [r for r in candidates if r["oversold"]]
    controls = [r for r in candidates if not r["oversold"]]
    unused = {r["event_id"]: r for r in controls}
    pairs = []
    unmatched = []
    for row in sorted(treated, key=lambda r: (r["trigger_ts"], r["event_id"])):
        eligible = [
            r for r in unused.values()
            if r["ticker"] == row["ticker"]
            and r["minute_of_session"] == row["minute_of_session"]
        ]
        if not eligible:
            unmatched.append(row["event_id"])
            continue
        control = min(eligible, key=lambda r: (
            abs(r["volatility20"] - row["volatility20"]), r["event_id"]))
        unused.pop(control["event_id"])
        pairs.append((row, control))
    interval = _cluster_interval(pairs)
    # 分母排除重复合并事件；时间不明的原始事件仍占分母并计入排除。
    independent = len(selected) - len(merged)
    exclusion_rate = len(excluded) / independent if independent else 0.0
    enough = (len(candidates) >= 60 and len(treated) >= 20
              and len(controls) >= 20 and len(pairs) >= 20)
    ci = interval["ci95"]
    passed = bool(enough and exclusion_rate <= 0.10
                  and interval["difference_pct"] > 0 and ci is not None and ci[0] > 0)

    def summary(rows):
        output = {"n": len(rows)}
        for day in ("1", "3", "5"):
            known = [r for r in rows if day in r["returns"]]
            output[day] = {
                "n": len(known),
                "mean_excess_pct": (
                    sum(r["returns"][day]["excess_pct"] for r in known) / len(known)
                    if known else None),
                "positive_raw_rate": (
                    sum(r["returns"][day]["raw_pct"] > 0 for r in known) / len(known)
                    if known else None),
                "mean_mae_pct": (
                    sum(r["mae_pct"][day] for r in known) / len(known) if known else None),
            }
        return output

    return {
        "bounds": bounds, "eligible": len(selected), "independent": independent,
        "merged_event_ids": merged, "excluded": excluded,
        "exclusion_rate": exclusion_rate, "unmatched_oversold": unmatched,
        "oversold": summary(treated), "control": summary(controls),
        "matched_pairs": [
            {"oversold": a["event_id"], "control": b["event_id"],
             "volatility_distance": abs(a["volatility20"] - b["volatility20"])}
            for a, b in pairs
        ],
        "primary": interval, "events": candidates, "passed": passed,
        "status": "通过" if passed else ("证据不足" if not enough else "未通过"),
    }


def evaluate_oversold_study(protocol: dict, dataset: dict, *, code_version: str) -> dict:
    """离线事件研究，无写盘、网络、订单或决策回传。

    dataset 契约：
      data_version/calendar_version、sessions=[{date,open,close}]；
      inventory 是全部合格止损事件清单，每项含
      event_id/event_date/ticker/rule_version；
      events 与清单一一对应，另含 trigger_ts/daily/minutes/
      minute_reliable/sector_benchmark；
      replay 含 complete=True、source_sha256、rule_versions，证明上游按当时
      规则版本全量重放。本接口核对完整性，不替代上游历史规则重放器。
    前瞻数据另含 baseline_sha256，并提供同版本来源及全量事件清单。
    """
    result = {
        "proposal": "EVO-20261028-1", "passed": False, "status": "证据不足",
        "research_only": True, "trading_authorized": False,
        "execution_improvement_verified": False, "errors": [], "periods": {},
        "limitations": [
            "登记凭据、来源快照及历史规则重放须外部审计",
            "均值差通过不代表高概率反弹或卖在底部",
            "最近邻波动匹配不消除全部混杂，波动距离逐对披露",
            "价格路径不含可执行报价、费用或再入场成本，不能证明止损错误",
        ],
    }
    try:
        body = {k: v for k, v in protocol.items() if k != "protocol_sha256"}
        expected = make_oversold_protocol(
            **{k: protocol[k] for k in (
                "registered_at", "development_started_at", "registration_receipt",
                "data_version", "data_sha256", "code_version", "calendar_version",
                "rule_versions", "historical_oos_used")},
            prospective_end=protocol["oos"][1] if protocol["historical_oos_used"] else None,
        )
        if protocol != expected or oversold_digest(body) != protocol["protocol_sha256"]:
            raise ValueError("协议被改动或固定检验定义不一致")
        if code_version != protocol["code_version"]:
            raise ValueError("执行代码版本与登记版本不一致")
        if (dataset["data_version"] != protocol["data_version"]
                or dataset["calendar_version"] != protocol["calendar_version"]):
            raise ValueError("数据或交易日历版本不一致")
        digest = oversold_digest(dataset)
        if protocol["historical_oos_used"]:
            if dataset.get("baseline_sha256") != protocol["data_sha256"]:
                raise ValueError("前瞻数据缺少登记基线")
        elif digest != protocol["data_sha256"]:
            raise ValueError("历史数据快照摘要不一致")
        replay = dataset["replay"]
        if (replay.get("complete") is not True or not replay.get("source_sha256")
                or sorted(replay["rule_versions"]) != protocol["rule_versions"]):
            raise ValueError("缺少全部合格止损事件的历史规则重放凭据")
        inventory = dataset["inventory"]
        events = dataset["events"]
        ids = [str(r["event_id"]) for r in inventory]
        event_ids = [str(r["event_id"]) for r in events]
        if (len(set(ids)) != len(ids) or len(set(event_ids)) != len(event_ids)
                or set(ids) != set(event_ids)):
            raise ValueError("重放清单重复或事件覆盖不完整")
        by_id = {str(r["event_id"]): r for r in events}
        for item in inventory:
            if not isinstance(item["event_id"], str):
                raise ValueError("事件编号须为字符串")
            date.fromisoformat(item["event_date"])
            row = by_id[item["event_id"]]
            if (item["rule_version"] not in protocol["rule_versions"]
                    or any(row.get(k) != item[k] for k in
                           ("event_id", "ticker", "rule_version", "event_date"))
                    or not item["ticker"]):
                raise ValueError("事件与当时规则清单不一致")
        sessions = _study_calendar(dataset["sessions"])
        periods = {
            "train": _study_period(inventory, by_id, sessions, OVERSOLD_SPEC["train"]),
            "oos": _study_period(inventory, by_id, sessions, protocol["oos"]),
        }
        result.update({
            "protocol_sha256": protocol["protocol_sha256"],
            "data_sha256": digest, "code_version": code_version, "periods": periods,
            "passed": all(p["passed"] for p in periods.values()),
        })
        result["status"] = (
            "通过" if result["passed"] else
            ("证据不足" if any(p["status"] == "证据不足" for p in periods.values())
             else "未通过")
        )
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        result["errors"].append(str(exc))
    return result

