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


def audit_latest_signal_files(signals_dir: Path, max_age_hours: float = 36.0) -> dict:
    now = datetime.now(timezone.utc).astimezone()
    files = []
    critical = 0
    warning = 0
    for path in sorted(Path(signals_dir).glob("*_latest.json")):
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
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "status": "critical" if critical else ("warning" if warning else "ok"),
        "critical_files": critical,
        "warning_files": warning,
        "checked_files": len(files),
        "files": files,
    }

