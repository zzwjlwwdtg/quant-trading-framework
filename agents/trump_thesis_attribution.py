"""trump_thesis_attribution.py — 关联 Trump 帖子和 cut_prob 变化.

思路: 对每个显著 Trump 帖子 (magnitude=large/extreme 或 FED_ATTACK/TARIFF),
找它 post_time 前后的 thesis_history 快照, 计算 cut_prob delta。
这能回答: "哪些 Trump 帖子实际推高/压低了降息概率?"

用法:
  from trump_thesis_attribution import analyze_attribution
  result = analyze_attribution()   # returns {top_movers, ...}

局限:
  · thesis_history 只覆盖 180 天 backfill (~2026-02-26 起)
  · thesis_history 采样频率 12h, 帖子到 snapshot 有时间差
  · 只是相关性, 不是因果 (同期可能有其它宏观事件)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TRUMP_CACHE = Path(__file__).parent / "signals" / "trump_cache"


def _load_trump_posts(min_magnitude: str = "medium") -> list[dict]:
    """加载所有 trump_cache 里的显著 items. 按 post_time 排序."""
    mag_order = {"small": 1, "medium": 2, "large": 3, "extreme": 4}
    threshold = mag_order.get(min_magnitude, 2)
    key_events = {"TARIFF", "FED_ATTACK", "SANCTION", "GEOPOLITICS", "DEAL"}
    posts = []
    if not _TRUMP_CACHE.exists():
        return posts
    for f in _TRUMP_CACHE.glob("parsed_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data.get("items", []):
                mag = item.get("magnitude", "small")
                evt = item.get("event_type", "")
                pt = item.get("post_time", "")
                if not pt:
                    continue
                is_key_event = evt in key_events
                if mag_order.get(mag, 0) >= threshold or is_key_event:
                    posts.append({
                        "post_time": pt,
                        "event_type": evt,
                        "magnitude": mag,
                        "direction": item.get("direction", "neutral"),
                        "confidence": item.get("confidence", 0),
                        "tickers": item.get("tickers_affected", []),
                        "preview": (item.get("preview") or "")[:120],
                        "post_id": item.get("post_id", ""),
                    })
        except Exception:
            continue
    posts.sort(key=lambda p: p["post_time"])
    # 去重 (同一 post_id 可能被多个 parse batch 抓到)
    seen = set()
    unique = []
    for p in posts:
        if p["post_id"] not in seen:
            seen.add(p["post_id"])
            unique.append(p)
    return unique


def _load_thesis_snapshots() -> list[dict]:
    """按 ts 排序的 thesis snapshot 列表."""
    from thesis_history import HIST_PATH
    if not HIST_PATH.exists():
        return []
    rows = []
    with HIST_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                rows.append(row)
            except Exception:
                continue
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows


def _find_nearest_snapshot(target_dt: datetime, snapshots: list[dict], direction: str = "after") -> dict | None:
    """direction: 'before' = 找 target 之前最近一条 / 'after' = 之后最近."""
    for row in snapshots if direction == "after" else reversed(snapshots):
        try:
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            if direction == "after" and ts >= target_dt:
                return row
            if direction == "before" and ts <= target_dt:
                return row
        except Exception:
            continue
    return None


def analyze_attribution(min_magnitude: str = "medium",
                        window_hours: int = 48) -> dict:
    """对每个显著 Trump 帖子, 计算前后 cut_prob delta.
    window_hours: post 后多久内的 snapshot 算作"reaction"."""
    posts = _load_trump_posts(min_magnitude=min_magnitude)
    snapshots = _load_thesis_snapshots()
    if not posts or not snapshots:
        return {"error": "无 trump 帖子或 thesis 历史", "posts_count": len(posts), "snapshots_count": len(snapshots)}
    attributed = []
    for post in posts:
        try:
            pt = datetime.fromisoformat(post["post_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        before = _find_nearest_snapshot(pt, snapshots, "before")
        after_cutoff = pt + timedelta(hours=window_hours)
        # 找 post 后 window_hours 内最靠后的 snapshot
        after = None
        for row in snapshots:
            try:
                ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
                if pt <= ts <= after_cutoff:
                    after = row
            except Exception:
                continue
        if not before or not after:
            continue
        cp_before = before.get("cut_probability_pct")
        cp_after = after.get("cut_probability_pct")
        if cp_before is None or cp_after is None:
            continue
        delta = cp_after - cp_before
        attributed.append({
            "post_time": post["post_time"],
            "event_type": post["event_type"],
            "magnitude": post["magnitude"],
            "direction": post["direction"],
            "tickers": post["tickers"],
            "preview": post["preview"],
            "cut_prob_before": cp_before,
            "cut_prob_after": cp_after,
            "delta": round(delta, 1),
            "before_ts": before.get("ts"),
            "after_ts": after.get("ts"),
        })
    # 按 |delta| 排序，找 top movers
    attributed.sort(key=lambda x: abs(x["delta"]), reverse=True)
    # 按 event_type 分组统计
    by_event = {}
    for a in attributed:
        et = a["event_type"]
        if et not in by_event:
            by_event[et] = {"count": 0, "avg_delta": 0.0, "sum_delta": 0.0}
        by_event[et]["count"] += 1
        by_event[et]["sum_delta"] += a["delta"]
    for et in by_event:
        c = by_event[et]["count"]
        by_event[et]["avg_delta"] = round(by_event[et]["sum_delta"] / c, 2) if c else 0
        by_event[et]["sum_delta"] = round(by_event[et]["sum_delta"], 1)
    return {
        "posts_analyzed": len(attributed),
        "posts_total": len(posts),
        "snapshots_used": len(snapshots),
        "window_hours": window_hours,
        "top_movers": attributed[:15],  # 影响最大的 15 条
        "by_event_type": by_event,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    r = analyze_attribution()
    print(json.dumps({
        "posts_analyzed": r.get("posts_analyzed"),
        "by_event_type": r.get("by_event_type"),
        "top_5_movers": [
            {"date": m["post_time"][:10], "event": m["event_type"],
             "mag": m["magnitude"], "delta_pp": m["delta"], "preview": m["preview"][:80]}
            for m in (r.get("top_movers") or [])[:5]
        ],
    }, ensure_ascii=False, indent=2))
