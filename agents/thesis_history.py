"""thesis_history.py — 时序追踪 Fed 降息概率 + 各节点状态.

每日采样 bond_monitor 快照 + 规则版 thesis_check，追加到 jsonl。
用途:
  · Dashboard 显示 90d 降息概率演化 mini chart
  · AI 评估时读最近 4-8 周历史，说"趋势在向哪走"
  · 支持 backfill (从 FRED 拉 180d 历史数据一次性 seed)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HIST_PATH = Path(__file__).parent / "signals" / "thesis_history.jsonl"
MIN_APPEND_HOURS = 12  # 至少 12h 才追加一次 (避免同日多次运行灌水)


def _read_last_ts() -> Optional[str]:
    if not HIST_PATH.exists():
        return None
    try:
        with HIST_PATH.open("r", encoding="utf-8") as f:
            last = None
            for line in f:
                if line.strip():
                    last = line
        if last:
            return json.loads(last).get("ts")
    except Exception:
        pass
    return None


def _snapshot_row(bond_data: dict) -> dict:
    """从 bond_monitor 快照抽出 thesis 相关精简字段."""
    mc = bond_data.get("macro_context", {}) or {}
    yields = bond_data.get("yields", {}) or {}
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        # 核心利率
        "y10": (yields.get("10y") or {}).get("value"),
        "y30": (yields.get("30y") or {}).get("value"),
        "tips": (yields.get("tips_10y") or {}).get("value"),
        # Fed 降息驱动 (通胀/就业)
        "cpi_yoy": mc.get("cpi_yoy_pct"),
        "core_cpi_yoy": mc.get("core_cpi_yoy_pct"),
        "unrate": mc.get("unemployment_pct"),
        # 金融市场状态
        "vix": mc.get("vix"),
        "ig_bps": mc.get("cdx_ig_bps"),
        "hy_bps": mc.get("cdx_hy_bps"),
        "nfci": (mc.get("nfci") or {}).get("value"),
        # 美元/油
        "dxy": mc.get("dxy"),
        "oil_uso": mc.get("oil_uso"),
        "oil_pct_20d": mc.get("oil_pct_20d"),
        # 复合
        "erp": mc.get("erp_vs_tips_pct"),
        "bbi": mc.get("bbi_score"),
        "stablecoin_bn": mc.get("stablecoin_total_bn"),
    }
    row["cut_probability_pct"] = _estimate_cut_probability(row)
    return row


def _estimate_cut_probability(row: dict) -> int:
    """规则版降息概率估算 — 跟 bond_ai_interpret._fallback_from_rules 逻辑保持一致."""
    score = 50
    cpi = row.get("cpi_yoy"); unrate = row.get("unrate")
    dxy = row.get("dxy"); oil = row.get("oil_pct_20d")
    hy = row.get("hy_bps"); vix = row.get("vix"); y10 = row.get("y10")
    # 支持降息
    if cpi is not None:
        if cpi < 2.5: score += 15
        elif cpi < 3: score += 10
        elif cpi >= 3.5: score -= 15
    if unrate is not None:
        if unrate >= 4.5: score += 8
        if unrate <= 4.0: score -= 10
    if dxy is not None and dxy < 100: score += 5
    if oil is not None and oil <= -5: score += 5
    if hy is not None:
        if hy > 400: score += 10
        if hy < 300: score -= 5
    if vix is not None:
        if vix > 22: score += 5
        if vix < 18: score -= 5
    if y10 is not None and y10 > 4.7: score -= 3
    return max(5, min(score, 95))


def append_snapshot(bond_data: dict, force: bool = False) -> bool:
    """追加一条 snapshot; 距上次 <MIN_APPEND_HOURS 且 force=False 则跳过。返回是否写入."""
    if not force:
        last_ts = _read_last_ts()
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if elapsed < MIN_APPEND_HOURS:
                    return False
            except Exception:
                pass
    row = _snapshot_row(bond_data)
    HIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HIST_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def load_history(days: int = 180) -> list[dict]:
    """读近 N 天 snapshot."""
    if not HIST_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    try:
        with HIST_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
                    if ts >= cutoff:
                        rows.append(row)
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def get_trend_summary(days: int = 30) -> dict:
    """给 AI prompt 用: 近 N 天 cut probability 趋势 + 关键指标变化."""
    rows = load_history(days=days)
    if not rows:
        return {"n_samples": 0, "note": "无历史数据"}
    first = rows[0]; latest = rows[-1]
    def _delta(k):
        v_a = first.get(k); v_b = latest.get(k)
        if v_a is None or v_b is None:
            return None
        return round(v_b - v_a, 2)
    return {
        "n_samples": len(rows),
        "period_days": days,
        "cut_prob_start": first.get("cut_probability_pct"),
        "cut_prob_latest": latest.get("cut_probability_pct"),
        "cut_prob_delta": _delta("cut_probability_pct"),
        "y10_delta_bps": _delta("y10") * 100 if _delta("y10") is not None else None,
        "cpi_yoy_delta_pp": _delta("cpi_yoy"),
        "unrate_delta_pp": _delta("unrate"),
        "vix_delta": _delta("vix"),
        "hy_delta_bps": _delta("hy_bps"),
        "dxy_delta": _delta("dxy"),
    }


if __name__ == "__main__":
    from bond_monitor import get_bond_monitor
    d = get_bond_monitor()
    wrote = append_snapshot(d, force=True)
    print(f"appended: {wrote}")
    print(f"trend 30d: {json.dumps(get_trend_summary(30), ensure_ascii=False, indent=2)}")
    print(f"total history: {len(load_history(365))} rows")
