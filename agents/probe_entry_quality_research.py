"""EVO-20261020-1：独立的预注册回测，不接入任何交易路径。

输入为离线整理的历史触发事件，禁止用当前 AI / regime / 行情补历史。
每条事件包含：
  event_id、ticker（US. 前缀）、trigger、ts（带时区）、
  ref_price、live_price、quote_source、ai_gate_status、regime_today、
  forward_prices（1h / EOD / T+1 EOD 的历史价格）。

forward_prices 必须由离线回放按交易所日历生成：1h 为触发后一小时；
EOD 为触发所属交易日收盘，T+1 EOD 为下一交易日收盘（含半日市）。
每条事件的 evidence 字段记录原始触发、快照和远期价格的来源。
本模块不推断附件中未提供的历史日志结构，也不联网补价。
"""

import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median


PROPOSAL_ID = "EVO-20261020-1"
HORIZONS = ("1h", "EOD", "T+1 EOD")
CHANGE_DESCRIPTION = (
    "【用户批准范围限定 2026-10-29: 仅研究】"
    "本次只实现预注册回测/影子记录: 在历史 sim_active_probe 事件上"
    "评估 entry_quality_gate 的过滤效果并写入 signals/backtest_verdicts/; "
    "不得修改 strategy_engine/decision_agent/paper_trader 的下单行为。"
    "回测通过后另提交易改动提案。"
)


def _digest(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _positive(value):
    if isinstance(value, bool):
        raise ValueError("价格和阈值不能是布尔值")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("价格和阈值必须为有限正数")
    return number


def _timestamp(value):
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return ts


def preregister(path, *, as_of, gap_calibration, universe, evidence_manifest,
                event_ids, sessions, registered_at):
    """读取结果前独占写入预注册；已有文件不能覆盖。

    gap_calibration 必须来自先前预注册的训练期校准结论，不能在这里调参。
    evidence_manifest 列出完整历史事件、行情和交易日历文件的 SHA256；
    universe 为系统历史交易过的全部 US 标的，不使用当前交易池代替。
    本函数不读取这些源文件，其真实性和完整性由离线数据整理者核对。
    """
    cutoff = date.fromisoformat(as_of)
    if cutoff < date(2026, 7, 1):
        raise ValueError("样本外截止日不能早于 2026-07-01")
    calibration = dict(gap_calibration)
    gap = _positive(calibration["gap_max"])
    if gap >= 1:
        raise ValueError("gap_max 使用比例单位，必须小于 1")
    if (calibration.get("verdict") != "pass"
            or not calibration.get("preregistration_sha256")
            or not calibration.get("verdict_sha256")
            or date.fromisoformat(calibration["sample_end"]) > date(2026, 6, 30)):
        raise ValueError("缺少训练期预注册校准证据，禁止手工设定 GAP_MAX")
    tickers = sorted(set(universe))
    if not tickers or any(not t.startswith("US.") for t in tickers):
        raise ValueError("必须提供全部历史 US 标的")
    if not evidence_manifest:
        raise ValueError("必须预先固定历史证据清单")
    for digest in [*evidence_manifest.values(), calibration.get("preregistration_sha256"), calibration.get("verdict_sha256")]:
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("证据清单必须使用 SHA256")
    documents = {}
    try:
        for label in ("preregistration", "verdict"):
            raw = Path(calibration[f"{label}_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != calibration[f"{label}_sha256"]:
                raise ValueError("校准证据文件摘要不符")
            documents[label] = json.loads(raw)
        registration, verdict = documents["preregistration"], documents["verdict"]
        if (verdict["verdict"] != "pass" or verdict["gap_max"] != gap
                or verdict["sample_end"] != calibration["sample_end"]
                or verdict["preregistration_sha256"] != calibration["preregistration_sha256"]
                or registration["train_end"] != calibration["sample_end"]
                or _timestamp(registration["registered_at"]).date() > date.fromisoformat(registration["train_start"])):
            raise ValueError("校准结论或事前注册区间不符")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"需要真实训练期预注册与校准结论文件: {exc}") from None
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    registered = _timestamp(registered_at)
    if cutoff > registered.astimezone(et).date():
        raise ValueError("研究截止日不能晚于注册日")
    if not event_ids or any(not isinstance(i, str) or not i for i in event_ids) or len(set(event_ids)) != len(event_ids):
        raise ValueError("必须固定完整且不重复的历史事件清单")
    if not sessions or any(_timestamp(t).astimezone(et).date().isoformat() != day for day, t in sessions.items()):
        raise ValueError("需要明确带时区且交易日一致的完整收盘日历")
    plan = {
        "proposal_id": PROPOSAL_ID,
        "change": {"description": CHANGE_DESCRIPTION},
        "scope": "research_only",
        "train": ["2024-01-01", "2026-06-30"],
        "oos": ["2026-07-01", as_of],
        "horizons": list(HORIZONS),
        "gap_calibration": calibration,
        "calibration_documents": documents,
        "skip_ai_statuses": ["HOLD", "SELL"],
        "skip_regimes": ["risk_off"],
        "universe": tickers,
        "evidence_manifest": dict(evidence_manifest),
        "registered_at": registered_at,
        "event_ids": sorted(event_ids),
        "sessions": dict(sorted(sessions.items())),
        "protocol": {
            "return": "历史远期价格 / 触发时 live_price - 1；比例单位",
            "portfolio": "每事件等额预算，skip 留现金且收益为零，分母不变",
            "significance": "按 ET 交易日聚合 skip 收益均值后做单侧精确符号检验；零值保留为非负",
            "acceptance": "三个期限分别在两期满足 skip median<0 且 p<0.1；两期组合均值均不下降",
            "missing": "缺失、重复、未成熟或无法分类的事件均阻止通过，不选择性删除",
            "deployment": "即使通过也不得上线，另提交易改动提案",
        },
    }
    plan["plan_sha256"] = _digest(plan)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return plan


def _gate(event, plan):
    reasons = []
    ref = _positive(event["ref_price"])
    live = _positive(event["live_price"])
    if not isinstance(event["quote_source"], str) or event["quote_source"] in ("", "fallback_daily"):
        raise ValueError("历史实时报价不可用")
    status = event["ai_gate_status"]
    regime = event["regime_today"]
    if status not in ("BUY", "ADD", "WATCH_BUY", "PROBE", "HOLD", "SELL", "REDUCE"):
        raise ValueError("缺少历史 AI Gate 状态")
    if regime not in ("risk_on", "neutral", "risk_off", "crisis"):
        raise ValueError("缺少历史系统级 regime")
    if abs(ref - live) / ref >= plan["gap_calibration"]["gap_max"]:
        reasons.append("ref_live_gap")
    if status in plan["skip_ai_statuses"]:
        reasons.append("ai_gate")
    if regime in plan["skip_regimes"]:
        reasons.append("risk_off")
    return reasons


def _stats(values):
    return {
        "n": len(values),
        "avg_return": mean(values) if values else None,
        "median_return": median(values) if values else None,
        "win_rate": sum(v > 0 for v in values) / len(values) if values else None,
    }


def _negative_sign_p(daily_values):
    """负收益的单侧精确符号检验；按日聚合避免同日多标的虚增样本。"""
    n = len(daily_values)
    if not n:
        return None
    negative = sum(v < 0 for v in daily_values)
    return sum(math.comb(n, k) for k in range(negative, n + 1)) / (2 ** n)


def evaluate(preregistration_path, events, *, observed_at, evidence_manifest):
    """回放标准化历史事件，返回研究结论及逐事件影子记录。

    observed_at 必须晚于全部远期价格的实际观测时刻。
    forward_prices 的每个期限为 {price, ts}；ET session_date 为交易日标签。
    交易日历定位由离线整理层负责，必须包含在预注册证据清单中。
    """
    from zoneinfo import ZoneInfo

    plan = json.loads(Path(preregistration_path).read_text(encoding="utf-8"))
    signed = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if _digest(signed) != plan.get("plan_sha256"):
        raise ValueError("预注册内容已变化")
    if (plan["proposal_id"] != PROPOSAL_ID
            or plan["scope"] != "research_only"
            or tuple(plan["horizons"]) != HORIZONS):
        raise ValueError("预注册方案不匹配")
    if evidence_manifest != plan["evidence_manifest"]:
        raise ValueError("证据文件已变化，必须保留原预注册数据")
    observed = _timestamp(observed_at)
    if observed < _timestamp(plan["registered_at"]):
        raise ValueError("回放时刻不得早于本次研究注册")
    et = ZoneInfo("America/New_York")
    rows, errors, seen = [], [], set()
    source_events = list(events)
    closes = sorted(_timestamp(t) for t in plan["sessions"].values())
    for index, event in enumerate(source_events):
        try:
            event_id = event["event_id"]
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("事件 ID 不可为空")
            if event_id in seen:
                raise ValueError("重复事件 ID")
            seen.add(event_id)
            if event_id not in plan["event_ids"]:
                raise ValueError("事件不在冻结清单")
            if event["trigger"] not in (
                    "sim_active_probe", "sim_active_probe_after_claude_veto"):
                raise ValueError("不是本提案的历史触发事件")
            if event["ticker"] not in plan["universe"]:
                raise ValueError("标的不在预注册历史全集")
            ts = _timestamp(event["ts"])
            session = date.fromisoformat(event["session_date"])
            if session != ts.astimezone(et).date():
                raise ValueError("触发时间与 ET 交易日不一致")
            day = session.isoformat()
            if plan["train"][0] <= day <= plan["train"][1]:
                period = "train"
            elif plan["oos"][0] <= day <= plan["oos"][1]:
                period = "oos"
            else:
                raise ValueError("事件超出预注册区间")
            if not event.get("evidence"):
                raise ValueError("缺少事件来源")
            snapshot = _timestamp(event["snapshot_ts"])
            if snapshot > ts:
                raise ValueError("历史快照晚于触发时刻")
            eod = _timestamp(plan["sessions"][day])
            following = [t for t in closes if t > eod]
            if ts >= eod or not following:
                raise ValueError("缺下一交易日或事件不在该交易日收盘前")
            targets = {"EOD": eod, "T+1 EOD": following[0]}
            reasons = _gate(event, plan)
            returns = {}
            for horizon in HORIZONS:
                point = event["forward_prices"][horizon]
                end = _timestamp(point["ts"])
                if end < ts or end > observed:
                    raise ValueError("远期价格未成熟或时间倒置")
                if horizon == "1h" and (end - ts).total_seconds() != 3600:
                    raise ValueError("1h 价格时点不正确")
                if horizon in targets and end != targets[horizon]:
                    raise ValueError("收盘价时点与冻结交易日历不符")
                if end.astimezone(et).date().isoformat() > plan["oos"][1]:
                    raise ValueError("远期价格超出冻结样本截止日")
                returns[horizon] = _positive(point["price"]) / _positive(event["live_price"]) - 1
            rows.append({
                "event_id": event_id, "ticker": event["ticker"],
                "session_date": day, "period": period,
                "gate": "skip" if reasons else "pass",
                "reasons": reasons, "returns": returns,
                "evidence": event["evidence"],
            })
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            errors.append({"index": index, "reason": str(exc)})

    missing_ids = sorted(set(plan["event_ids"]) - seen)
    if missing_ids:
        errors.append({"missing_event_ids": missing_ids})

    results, checks = {}, []
    for period in ("train", "oos"):
        subset = [r for r in rows if r["period"] == period]
        results[period] = {}
        for horizon in HORIZONS:
            skipped = [r for r in subset if r["gate"] == "skip"]
            passed = [r for r in subset if r["gate"] == "pass"]
            values = [r["returns"][horizon] for r in skipped]
            days = {}
            for row in skipped:
                days.setdefault(row["session_date"], []).append(row["returns"][horizon])
            p_value = _negative_sign_p([mean(v) for v in days.values()])
            baseline = [r["returns"][horizon] for r in subset]
            filtered = [
                0.0 if r["gate"] == "skip" else r["returns"][horizon]
                for r in subset
            ]
            a = bool(values and median(values) < 0 and p_value < 0.1)
            b = bool(baseline and mean(filtered) >= mean(baseline))
            checks.extend((a, b))
            results[period][horizon] = {
                "pass": _stats([r["returns"][horizon] for r in passed]),
                "skip": _stats(values),
                "skip_daily_n": len(days), "skip_negative_p": p_value,
                "baseline": _stats(baseline),
                "filtered_cash_zero": _stats(filtered),
                "criterion_a": a, "criterion_b": b,
            }
    sufficient = not errors and all(
        results[p][h]["skip"]["n"] and results[p][h]["pass"]["n"]
        for p in results for h in HORIZONS
    )
    return {
        "proposal_id": PROPOSAL_ID,
        "change": plan["change"],
        "scope": "research_only",
        "preregistration_sha256": _digest(plan),
        "events_sha256": _digest(source_events) if not errors else None,
        "observed_at": observed_at,
        "evidence_manifest": evidence_manifest,
        "status": ("pass" if all(checks) else "reject") if sufficient else "insufficient",
        "deployment_allowed": False,
        "results": results, "shadow_records": rows, "errors": errors,
        "limitations": [
            "输入为离线标准化事件；完整性、历史快照来源和交易日历定位须核对证据清单。",
            "等额事件预算的现金零收益反事实，不代表含费用、动态仓位和资金再分配的真实 NAV。",
            "日期聚合符号检验仍不能消除跨日相关性；不得据此自动上线。",
            "本次注册只冻结后续分析口径，历史结果已经发生，不能追溯声称事前注册。",
        ],
    }


def write_verdict(report, verdict_dir):
    """由离线调用者显式写入 signals/backtest_verdicts/，禁止覆盖旧结论。

    测试必须传临时目录；没有默认 live 路径，导入模块不会创建文件。
    """
    directory = Path(verdict_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{PROPOSAL_ID}-{_digest(report)}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return path
