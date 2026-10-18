"""Point-in-time data validation and fail-closed order quality gates."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def _num(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _parse_time(value, now: datetime) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00").replace(" ET", "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Existing market snapshots use local wall-clock timestamps.
        return parsed.replace(tzinfo=now.astimezone().tzinfo)
    return parsed


def assess_market_snapshot(
    market: dict,
    *,
    now: datetime | None = None,
    max_age_hours: float = 36.0,
) -> dict:
    now = now or datetime.now(timezone.utc).astimezone()
    issues: list[dict] = []
    price = _num(market.get("price") or market.get("last_price"))
    if price is None or price <= 0:
        issues.append({"level": "critical", "code": "invalid_price", "message": "价格缺失或非正数"})

    # F03 fix (2026-09-19, audit): 缺失/未来时间戳应阻止新开仓 (order_data_gate
    # 会豁免减仓路径, 不误伤保护性退出).
    observed = _parse_time(market.get("ts") or market.get("quote_ts"), now)
    age_hours = None
    if observed is None:
        # 之前是 warning → BUY 放行. 现改为 critical: 无法验证价格是否可用就不能新开风险.
        issues.append({"level": "critical", "code": "missing_timestamp",
                        "message": "缺少可验证的行情时间戳"})
    else:
        raw_age = (now - observed.astimezone(now.tzinfo)).total_seconds() / 3600.0
        # 未来时间戳 (2099 年等): 之前 max(0, ...) 截为 0 → 假装新鲜. 现改 critical.
        # 保留 3 分钟 (0.05h) 容差应对时钟微偏.
        if raw_age < -0.05:
            issues.append({"level": "critical", "code": "future_timestamp",
                            "message": f"时间戳来自未来 {abs(raw_age):.1f} 小时后, 不可信"})
            age_hours = 0.0   # 显示用, 仍报告 issue
        else:
            age_hours = max(0.0, raw_age)
            if age_hours > max_age_hours:
                issues.append({"level": "critical", "code": "stale_price",
                                "message": f"行情已过期 {age_hours:.1f} 小时"})

    bid = _num(market.get("bid") or market.get("bid_price"))
    ask = _num(market.get("ask") or market.get("ask_price"))
    if bid is not None and ask is not None:
        if bid <= 0 or ask <= 0 or bid > ask:
            issues.append({"level": "critical", "code": "crossed_quote", "message": "买卖盘报价无效"})
        elif price and (price < bid * 0.97 or price > ask * 1.03):
            issues.append({"level": "warning", "code": "price_quote_mismatch", "message": "最新价明显偏离买卖盘"})
    else:
        issues.append({"level": "warning", "code": "missing_quote", "message": "缺少 bid/ask，成交成本使用保守估计"})

    rsi = _num(market.get("rsi_14"))
    if rsi is not None and not 0 <= rsi <= 100:
        issues.append({"level": "critical", "code": "invalid_rsi", "message": "RSI 超出 0-100"})
    required_indicators = ("rsi_14", "ma20", "ma50", "vol_ratio")
    missing = [key for key in required_indicators if market.get(key) is None]
    if missing:
        issues.append({"level": "warning", "code": "indicator_coverage", "message": "指标缺失: " + ", ".join(missing)})

    status = "critical" if any(x["level"] == "critical" for x in issues) else ("warning" if issues else "ok")
    return {
        "status": status,
        "allow_new_risk": status != "critical",
        "age_hours": age_hours,
        "issues": issues,
        "coverage_pct": (len(required_indicators) - len(missing)) / len(required_indicators) * 100,
    }


def order_data_gate(market: dict, side: str, *, now: datetime | None = None) -> dict:
    result = assess_market_snapshot(market, now=now)
    reducing = str(side).upper().startswith("SELL")
    # Never block a risk-reducing exit solely because quote metadata is stale.
    result["allow_order"] = bool(reducing or result["allow_new_risk"])
    result["risk_reducing_override"] = bool(reducing and not result["allow_new_risk"])
    return result


class PointInTimeStore:
    """Append-only observation store that prevents future-dated data leakage."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(
        self,
        *,
        source: str,
        payload: dict,
        observed_at: datetime | None = None,
        effective_at: datetime | None = None,
    ) -> dict:
        observed = observed_at or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        effective = effective_at or observed
        if effective.tzinfo is None:
            effective = effective.replace(tzinfo=timezone.utc)
        if effective > observed:
            raise ValueError("effective_at cannot be later than observed_at")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record = {
            "observed_at": observed.astimezone(timezone.utc).isoformat(),
            "effective_at": effective.astimezone(timezone.utc).isoformat(),
            "source": source,
            "payload_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "payload": payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def as_of(self, when: datetime, source: str | None = None) -> dict | None:
        if not self.path.exists():
            return None
        target = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        best = None
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if source and record.get("source") != source:
                        continue
                    observed = datetime.fromisoformat(record["observed_at"])
                    effective = datetime.fromisoformat(record["effective_at"])
                    if observed <= target and effective <= target:
                        if best is None or observed > datetime.fromisoformat(best["observed_at"]):
                            best = record
                except Exception:
                    continue
        return best


def price_divergence_rate(
    trade_log_path: Path | str,
    *,
    threshold: float = 0.006,
    since: str | None = None,
    include_dry_run: bool = False,
    top_k: int = 5,
) -> dict:
    """EVO-20261013-2 (2026-10-13): 读 trade_log.jsonl, 统计三端价差超阈值的比例.

    用于影子期判断 PRICE_DIVERGENCE 频率, 以及 dashboard 展示整体口径一致性.

    参数:
      trade_log_path   trade_log.jsonl 路径
      threshold        |divergence_pct| ≥ 阈值 视为 flagged (默认 0.6%)
      since            ISO 时间戳, 只统计 >= since 的条目; None = 全量
      include_dry_run  默认跳过 dry_run 记录
      top_k            按 |divergence_pct| 排前几条样本返回

    返回: {schema_version, generated_at, threshold, total, with_snapshot,
            flagged, rate, by_ticker, top_samples}.
    old/legacy 条目无 divergence_pct 字段 → 不计入 with_snapshot, 但计入 total,
    让 "snapshot 覆盖率" 可见.
    """
    now = datetime.now(timezone.utc).astimezone()
    path = Path(trade_log_path)
    total = 0
    with_snap = 0
    flagged = 0
    by_ticker_total: dict[str, int] = {}
    by_ticker_flag:  dict[str, int] = {}
    samples: list[dict] = []
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not include_dry_run and row.get("dry_run"):
                    continue
                if since and str(row.get("ts", "")) < since:
                    continue
                total += 1
                ticker = str(row.get("ticker") or "")
                by_ticker_total[ticker] = by_ticker_total.get(ticker, 0) + 1
                div = row.get("divergence_pct")
                if div is None:
                    continue
                try:
                    divf = float(div)
                except (TypeError, ValueError):
                    continue
                with_snap += 1
                if abs(divf) >= threshold:
                    flagged += 1
                    by_ticker_flag[ticker] = by_ticker_flag.get(ticker, 0) + 1
                    samples.append({
                        "ts": row.get("ts"),
                        "ticker": ticker,
                        "side": row.get("side"),
                        "ref_price": row.get("ref_price"),
                        "live_quote": row.get("live_quote"),
                        "divergence_pct": divf,
                        "divergence_flag": row.get("divergence_flag") or "PRICE_DIVERGENCE",
                    })
    rate = (flagged / with_snap) if with_snap else None
    samples.sort(key=lambda s: abs(float(s["divergence_pct"])), reverse=True)
    by_ticker = sorted(
        ({"ticker": tk, "total": by_ticker_total.get(tk, 0),
          "flagged": by_ticker_flag.get(tk, 0)}
         for tk in set(by_ticker_total) | set(by_ticker_flag)),
        key=lambda d: (-d["flagged"], -d["total"], d["ticker"]),
    )
    return {
        "schema_version":    1,
        "generated_at":      now.isoformat(),
        "threshold":         threshold,
        "since":             since,
        "total":             total,
        "with_snapshot":     with_snap,
        "snapshot_coverage": (with_snap / total) if total else None,
        "flagged":           flagged,
        "rate":              rate,
        "by_ticker":         by_ticker,
        "top_samples":       samples[:top_k],
    }


def _ticker_from_signal_filename(name: str) -> str:
    """`US.NVDA_latest.json` → `US.NVDA`; `6488.TW_latest.json` → `6488.TW`."""
    stem = name
    if stem.endswith("_latest.json"):
        stem = stem[: -len("_latest.json")]
    return stem


def audit_latest_signal_files(signals_dir: Path, max_age_hours: float = 36.0) -> dict:
    """审计 signals/*_latest.json 的新鲜度 / 字段完整度.

    EVO-20261014-3 (2026-10-19): retired 符号 (退市 / 长期 404 的 proxy) 的
    stale snapshot 不再计入 critical_files, 单独放到 retired_files, 让
    critical_files 回归到真正需要修的问题.
    """
    now = datetime.now(timezone.utc).astimezone()

    # 读 retired list — 不可用时降级为空集, 行为向前兼容
    try:
        from _data_source_health import list_retired
        retired_set = set(list_retired())
    except Exception:
        retired_set = set()

    files: list[dict] = []
    critical = 0
    warning = 0
    retired_count = 0
    for path in sorted(Path(signals_dir).glob("*_latest.json")):
        ticker = _ticker_from_signal_filename(path.name)
        if ticker in retired_set:
            files.append({
                "file": path.name,
                "status": "retired",
                "allow_new_risk": False,
                "issues": [{
                    "level": "info", "code": "retired",
                    "message": f"{ticker} 已 retired (退市/长期 404), 不计入 critical",
                }],
            })
            retired_count += 1
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            quality = assess_market_snapshot(data.get("market") or {}, now=now, max_age_hours=max_age_hours)
        except Exception as exc:
            quality = {
                "status": "critical",
                "allow_new_risk": False,
                "issues": [{"level": "critical", "code": "invalid_json", "message": str(exc)}],
            }
        if quality["status"] == "critical":
            critical += 1
        elif quality["status"] == "warning":
            warning += 1
        files.append({"file": path.name, **quality})
    return {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "status": "critical" if critical else ("warning" if warning else "ok"),
        "critical_files": critical,
        "warning_files": warning,
        "retired_files": retired_count,
        "checked_files": len(files),
        "files": files,
    }

