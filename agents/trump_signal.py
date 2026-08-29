"""
trump_signal.py — Trump Truth Social 发言 → 结构化交易信号

数据源（按优先级）：
  1. ix.cnn.io/data/truth-social/truth_archive.csv  （实时，约 5 分钟延迟）
  2. F:/trump-code/data/trump_posts_all.json        （静态全量备份）

按用户规则（feedback_news_pipeline.md）：发言原文不能直接 keyword 匹配，必须
先经 Claude CLI 拆成 JSON：

  {
    "event_type": "TARIFF" | "DEAL" | "FED" | "GEOPOLITICAL" | "MARKET"
                | "DOMESTIC_POLICY" | "MEDIA_ATTACK" | "NOISE",
    "direction":  "bullish" | "bearish" | "neutral",     # 对美股
    "magnitude":  "small" | "medium" | "large" | "extreme",
    "tickers_affected": ["SPY", "TQQQ", "SOXL", "GLD", "NVDA"],
    "confidence": 0-10,
    "post_id":    "...",
    "post_time":  "ISO8601",
    "is_overnight": bool,           # trump-code 发现：盘后/夜盘发布特殊处理
    "verbatim_evidence": "..."      # 推文原文片段（≤200 字符）
  }

聚合输出 (`get_trump_signal()`)：
  {
    "ts": now,
    "lookback_hours": 24,
    "posts_count": N,
    "active_signals": [...],            # 上述 item 列表
    "aggregate_direction": "bullish/bearish/neutral",
    "aggregate_magnitude": "...",
    "tariff_alert": bool,               # TARIFF 类信号触发
    "deal_signal": bool,                # DEAL 类（trump-code 数据示 deal 偏多）
    "silence_signal": bool,             # 24h 0 posts → 历史 80% bullish
    "fallback": bool,                   # CLI 失败时 True
    "source": "..."
  }

CLI 测试入口:
  python trump_signal.py             # 拉 24h 推文 + CLI 解析 + 聚合
  python trump_signal.py --hours 48  # 改 lookback
"""
from __future__ import annotations

import csv
import hashlib
import re
import io
import json
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import SIGNALS_DIR


# ── 数据源 ────────────────────────────────────────────────────────────────
LIVE_CSV_URL = "https://ix.cnn.io/data/truth-social/truth_archive.csv"
STATIC_JSON = Path("F:/trump-code/data/trump_posts_all.json")
CACHE_DIR = Path(SIGNALS_DIR) / "trump_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LIVE_CACHE_TTL_MIN = 5         # 5min cache for live feed
CLI_CACHE_TTL_HOURS = 6        # CLI 解析 6h cache

_EVENT_TYPES = {"TARIFF", "DEAL", "FED", "GEOPOLITICAL", "MARKET",
                "DOMESTIC_POLICY", "MEDIA_ATTACK", "NOISE"}
_DIRECTIONS  = {"bullish", "bearish", "neutral"}
_MAGNITUDES  = {"small", "medium", "large", "extreme"}


# ── 抓取 ──────────────────────────────────────────────────────────────────
def _live_cache_path() -> Path:
    return CACHE_DIR / "truth_live.csv"


def fetch_live_posts(timeout: int = 15) -> list[dict]:
    """从 CNN 实时 archive 拉最新一批。5min cache 减少请求。"""
    p = _live_cache_path()
    if p.exists():
        age_min = (datetime.now().timestamp() - p.stat().st_mtime) / 60
        if age_min < LIVE_CACHE_TTL_MIN:
            text = p.read_text(encoding="utf-8", errors="replace")
            return _parse_csv(text)
    try:
        req = urllib.request.Request(LIVE_CSV_URL,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", errors="replace")
        p.write_text(text, encoding="utf-8")
        return _parse_csv(text)
    except Exception:
        # fallback: 用旧 cache（即使过期）
        if p.exists():
            return _parse_csv(p.read_text(encoding="utf-8", errors="replace"))
        return []


def _parse_csv(text: str) -> list[dict]:
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            content = (row.get("content") or "").strip()
            if not content:
                continue   # 跳过空 content（往往是转发的纯图片/视频）
            rows.append({
                "id":        row.get("id", ""),
                "created_at": row.get("created_at", ""),
                "content":   content,
                "url":       row.get("url", ""),
            })
    except Exception:
        pass
    return rows


def fetch_static_posts(hours: int = 24) -> list[dict]:
    """从 trump-code 静态备份读取最近 N 小时。"""
    if not STATIC_JSON.exists():
        return []
    try:
        data = json.loads(STATIC_JSON.read_text(encoding="utf-8"))
        posts = data.get("posts", [])
    except Exception:
        return []
    return _filter_by_age(posts, hours)


NITTER_MIRRORS = [
    "https://nitter.net",       # 2026-08 主镜像，最稳定（19+ items 长期在线）
    "https://xcancel.com",      # fallback，item 少但存活
]


def fetch_x_posts(hours: int = 24, timeout: int = 8) -> list[dict]:
    """从 Nitter RSS 拉 @realDonaldTrump 最近 N 小时 X (Twitter) 推文。

    多镜像 fallback，全挂或全空 → 返 []（不报错，让主流程降级到 Truth Social only）。
    输出格式对齐 Truth Social：{id, content, created_at, source: "x"}
    """
    from xml.etree import ElementTree as ET
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    for base in NITTER_MIRRORS:
        url = f"{base}/realDonaldTrump/rss"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                xml_text = r.read().decode("utf-8", errors="replace")
            root = ET.fromstring(xml_text)
            posts = []
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link")  or "").strip()
                pub_s = (item.findtext("pubDate") or "").strip()
                try:
                    # RFC 822: "Sun, 03 Aug 2026 12:34:56 GMT"
                    pub_dt = datetime.strptime(pub_s, "%a, %d %b %Y %H:%M:%S %Z")
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    try:
                        pub_dt = datetime.strptime(pub_s, "%a, %d %b %Y %H:%M:%S %z")
                    except Exception:
                        continue
                if pub_dt < cutoff:
                    continue
                # 提 tweet id
                m = re.search(r"/status/(\d+)", link)
                post_id = m.group(1) if m else str(hash(title))[:12]
                posts.append({
                    "id":         f"x_{post_id}",
                    "content":    title,
                    "created_at": pub_dt.isoformat(),
                    "url":        link,
                    "source":     "x_nitter",
                })
            if posts:
                return posts
        except Exception:
            continue
    return []


def _dedup_by_content(posts: list[dict]) -> list[dict]:
    """按 content 前 200 字符 sha1 去重，保留最早出现的（通常 Truth Social 早于 X 转发）。"""
    seen = set()
    out = []
    for p in sorted(posts, key=lambda x: x.get("created_at", "")):
        content = (p.get("content") or "").strip()[:200]
        if not content:
            continue
        h = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(p)
    return out


def fetch_recent_posts(hours: int = 24) -> tuple[list[dict], str]:
    """优先 live Truth Social + X (Nitter) 合并去重，失败 fallback 静态。"""
    truth = _filter_by_age(fetch_live_posts(), hours)
    x_posts = fetch_x_posts(hours)   # 免费 Nitter，多镜像 fallback
    if truth or x_posts:
        merged = _dedup_by_content(truth + x_posts)
        label = f"cnn_live+x_nitter" if x_posts else "cnn_live"
        if not truth: label = "x_nitter_only"
        return merged, label
    static = fetch_static_posts(hours)
    if static:
        return static, "trump-code/static"
    return [], "none"


def _filter_by_age(posts: list[dict], hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for p in posts:
        ts = p.get("created_at", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= cutoff:
            out.append({**p, "_dt": dt})
    out.sort(key=lambda x: x["_dt"], reverse=True)
    return out


# ── Claude CLI 解析 ───────────────────────────────────────────────────────
_PROMPT = """你是 Trump Truth Social 推文 → 美股交易信号 的解析器。

请把每条推文严格分类（event_type 枚举）：
- TARIFF       : 关税相关（threats / agreement / 取消 / 调整）
- DEAL         : 重大商业 / 贸易协议 / 投资承诺
- FED          : 联储 / 利率 / Fed Chair (Powell/Warsh) / 货币政策评论
- GEOPOLITICAL : 地缘冲突 / 制裁 / 战争 / 外交
- MARKET       : 直接喊单（"buy stocks now"）/ 谈具体行业 / 谈具体公司
- DOMESTIC_POLICY : 国内政策 / 财政 / 监管 / 移民等不直接动行业
- MEDIA_ATTACK : 攻击媒体 / 个人 / 政敌（通常对市场无影响）
- NOISE        : 节日问候 / 个人感想 / 转发名人言论等

每条推文必须返回（严格按 schema）：
- event_type
- direction: 对美股影响。bullish/bearish/neutral
  关键经验（trump-code 历史回测）：
    - **关税威胁 (TARIFF threat) ≠ bearish**：实际历史里 TARIFF→SHORT 错 70%
      （往往随后撤回 / 谈判，反弹），所以纯 tariff threat 应标 neutral 或 small bearish
    - **关税 RELIEF（取消/暂停）= 强 bullish**：盘前 RELIEF 信号是最强的买入
    - **DEAL 信号一般 bullish**（贸易协议 / 投资承诺）
    - **late-night tariff tweets = 反指标**（62% 错），可标 magnitude=small
- magnitude: small / medium / large / extreme
- tickers_affected: 从 [SPY, QQQ, TQQQ, SOXL, NVDA, GLD] 选受影响标的，最多 6 个
    钢铁/铝/汽车 = SPY；半导体相关 = SOXL, NVDA, QQQ；通胀避险 = GLD
- confidence: 0-10
- is_overnight: 推文是否发布在美东时间盘后 (16:00-9:30 ET)
- verbatim_evidence: 推文里支持判断的关键句（≤200 字符）

**输出规则**：
1. 严格 JSON 单根对象 `{{"items":[...]}}`
2. 不要 markdown / 不要 ```json``` 围栏 / 不要前后解释
3. items 长度严格等于输入推文数，按相同顺序

今日日期（UTC）: {today}
待解析推文（JSON 数组，最新在前）：
{posts_json}
"""


def _cli_cache_path(key: str) -> Path:
    return CACHE_DIR / f"parsed_{key}.json"


def _cli_cache_read(key: str) -> dict | None:
    p = _cli_cache_path(key)
    if not p.exists():
        return None
    age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
    if age_h > CLI_CACHE_TTL_HOURS:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cli_cache_write(key: str, data: dict) -> None:
    try:
        _cli_cache_path(key).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _normalize_item(raw: dict, post: dict) -> dict:
    et = (raw.get("event_type") or "NOISE").upper().strip()
    if et not in _EVENT_TYPES:
        et = "NOISE"
    direction = (raw.get("direction") or "neutral").lower().strip()
    if direction not in _DIRECTIONS:
        direction = "neutral"
    magnitude = (raw.get("magnitude") or "small").lower().strip()
    if magnitude not in _MAGNITUDES:
        magnitude = "small"
    tickers = raw.get("tickers_affected") or []
    if not isinstance(tickers, list):
        tickers = []
    tickers = [str(t).upper().strip() for t in tickers][:6]
    try:
        conf = int(raw.get("confidence", 0))
    except Exception:
        conf = 0
    conf = max(0, min(10, conf))
    return {
        "event_type": et,
        "direction": direction,
        "magnitude": magnitude,
        "tickers_affected": tickers,
        "confidence": conf,
        "is_overnight": bool(raw.get("is_overnight", False)),
        "post_id": post.get("id", ""),
        "post_time": post.get("created_at", ""),
        "preview": (post.get("content") or "")[:120],
        "verbatim_evidence": (raw.get("verbatim_evidence") or "")[:240],
    }


def analyze_posts(posts: list[dict], *, cli_timeout: int = 90,
                   max_posts_per_call: int = 5) -> dict:
    """调统一 AI CLI 把推文拆成结构化信号。

    超过 max_posts_per_call 时分批调用（避免 prompt 过大或 stdin 限制）。
    """
    if not posts:
        return {"items": [], "fallback": False, "n_posts": 0}

    # 分批：超过限制时切片调用，最后合并 items
    if len(posts) > max_posts_per_call:
        all_items: list[dict] = []
        statuses: list[str] = []
        providers: set[str] = set()
        any_ok = False
        for i in range(0, len(posts), max_posts_per_call):
            chunk = posts[i:i + max_posts_per_call]
            sub = analyze_posts(chunk, cli_timeout=cli_timeout,
                                max_posts_per_call=max_posts_per_call)
            all_items.extend(sub.get("items", []))
            if sub.get("cli_status"):
                statuses.append(sub["cli_status"])
            if sub.get("provider"):
                providers.add(str(sub["provider"]))
            if not sub.get("fallback"):
                any_ok = True
        return {
            "items": all_items, "fallback": (not any_ok),
            "n_posts": len(posts),
            "provider": next(iter(providers)) if len(providers) == 1 else "Mixed",
            "ts": datetime.now().isoformat(),
            "cli_status": " | ".join(statuses)[:300] if statuses else "",
        }

    compact = [
        {"id": p.get("id", ""),
         "created_at": p.get("created_at", ""),
         "content": (p.get("content") or "")[:600]}
        for p in posts
    ]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    posts_json = json.dumps(compact, ensure_ascii=False, indent=2)

    text = json.dumps([compact, today], sort_keys=True, ensure_ascii=False)
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    cached = _cli_cache_read(key)
    if cached:
        return {**cached, "cached": True}

    prompt = _PROMPT.format(today=today, posts_json=posts_json)

    from ai_prompt import query_ai_cli
    out, status, provider, _ = query_ai_cli(prompt, timeout=cli_timeout)

    if not out:
        return {"items": [], "fallback": True, "n_posts": len(posts),
                "cli_status": status}

    from news_analyzer import _extract_json_from_cli_output
    parsed = _extract_json_from_cli_output(out)
    if not parsed or not isinstance(parsed.get("items"), list):
        return {"items": [], "fallback": True, "n_posts": len(posts),
                "cli_status": f"parse_failed: {out[:200]}"}

    items = [_normalize_item(it, p) for it, p in zip(parsed["items"], posts)
             if isinstance(it, dict)]
    result = {"items": items, "fallback": False, "n_posts": len(posts),
              "provider": provider, "ts": datetime.now().isoformat()}
    _cli_cache_write(key, result)
    return result


# ── 聚合 ──────────────────────────────────────────────────────────────────
_MAG_WEIGHT = {"small": 1, "medium": 2, "large": 3, "extreme": 5}


def _aggregate(items: list[dict]) -> tuple[str, str, int]:
    """加权聚合方向：返回 (direction, magnitude, score)。"""
    if not items:
        return "neutral", "small", 0
    score = 0
    for it in items:
        w = _MAG_WEIGHT.get(it["magnitude"], 1) * (it["confidence"] / 10.0)
        if it["direction"] == "bullish":
            score += w
        elif it["direction"] == "bearish":
            score -= w
    if abs(score) < 1.0:
        direction = "neutral"
    elif score > 0:
        direction = "bullish"
    else:
        direction = "bearish"
    mag_score = abs(score)
    if mag_score >= 5:
        mag = "extreme"
    elif mag_score >= 3:
        mag = "large"
    elif mag_score >= 1.5:
        mag = "medium"
    else:
        mag = "small"
    return direction, mag, int(score * 10)


# events_watch 层用的快速 cache：每 30min 一次完整聚合，避免每个 15min cycle 都触发 CLI
_AGG_CACHE_PATH = CACHE_DIR / "aggregate_latest.json"
_AGG_CACHE_TTL_MIN = 30


def get_trump_signal_cached(hours: int = 24) -> dict:
    """events_watch 调用入口：30min cache。其它直接调用 get_trump_signal()。"""
    if _AGG_CACHE_PATH.exists():
        age_min = (datetime.now().timestamp() - _AGG_CACHE_PATH.stat().st_mtime) / 60
        if age_min < _AGG_CACHE_TTL_MIN:
            try:
                return json.loads(_AGG_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
    sig = get_trump_signal(hours=hours)
    try:
        _AGG_CACHE_PATH.write_text(json.dumps(sig, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except Exception:
        pass
    return sig


def get_trump_signal(hours: int = 24) -> dict:
    """主入口：抓推文 → CLI 解析 → 聚合。"""
    posts, source = fetch_recent_posts(hours=hours)
    parsed = analyze_posts(posts)
    items = parsed.get("items", [])
    direction, magnitude, score = _aggregate(items)

    # 单一类型筛
    tariff = [it for it in items
              if it["event_type"] == "TARIFF" and it["confidence"] >= 6]
    deal = [it for it in items
            if it["event_type"] == "DEAL" and it["confidence"] >= 6]

    # silence signal: 24h 0 posts → 历史 80% bullish (trump-code 发现)
    silence = (len(posts) == 0)
    if silence:
        # 用 silence 信号 override neutral
        direction = "bullish"
        magnitude = "small"

    return {
        "ts": datetime.now().isoformat(),
        "lookback_hours": hours,
        "posts_count": len(posts),
        "active_signals": items,
        "aggregate_direction": direction,
        "aggregate_magnitude": magnitude,
        "aggregate_score": score,
        "tariff_alert": bool(tariff),
        "tariff_count": len(tariff),
        "deal_signal": bool(deal),
        "silence_signal": silence,
        "fallback": parsed.get("fallback", False),
        "cli_status": parsed.get("cli_status", ""),
        "source": source,
        "provider": parsed.get("provider", ""),
    }


# ── 显示工具 ──────────────────────────────────────────────────────────────
def format_trump_banner(sig: dict) -> list[str]:
    import os as _os
    tech_only = _os.environ.get("TECHNICAL_ONLY", "1") != "0"
    label = "Trump Signal [参考]" if tech_only else "Trump Signal"
    W = 76
    lines = ["+" + "=" * (W - 2) + "+",
             f"|  {label}  |  {sig['ts'][:16]} (lookback {sig['lookback_hours']}h)".ljust(W - 1) + "|",
             "+" + "=" * (W - 2) + "+"]
    if sig.get("fallback"):
        lines.append("  ⚠ CLI fallback：本次未解析，可能调用失败")
        lines.append("=" * W)
        return lines
    posts_n = sig["posts_count"]
    agg_d = sig["aggregate_direction"]
    agg_m = sig["aggregate_magnitude"]
    lines.append(f"  推文数 {posts_n}    聚合方向 {agg_d}    幅度 {agg_m}    score {sig['aggregate_score']:+d}")
    if sig["silence_signal"]:
        lines.append("  ✓ Silence signal (0 posts in window) — 历史 80% bullish")
    if sig["tariff_alert"]:
        lines.append(f"  ⚠ TARIFF alert × {sig['tariff_count']} — trump-code 历史: 关税威胁 ≠ 直接 bearish")
    if sig["deal_signal"]:
        lines.append("  ✓ DEAL signal — 历史 bullish 倾向")
    # 列出 medium+ 信号
    for it in sig["active_signals"]:
        if _MAG_WEIGHT.get(it["magnitude"], 1) >= 2:
            tag = {"bullish": "▲", "bearish": "▼", "neutral": "·"}.get(it["direction"], "·")
            tickers = "/".join(it["tickers_affected"][:3]) if it["tickers_affected"] else "-"
            lines.append(f"    {tag} {it['event_type']:<14} {it['magnitude']:<7} conf={it['confidence']:<2} {tickers}")
            ev = it["verbatim_evidence"][:100]
            if ev:
                lines.append(f"        \"{ev}\"")
    lines.append("=" * W)
    return lines


def _cli_main():
    hours = 24
    if "--hours" in sys.argv:
        try:
            hours = int(sys.argv[sys.argv.index("--hours") + 1])
        except Exception:
            pass
    sig = get_trump_signal(hours=hours)
    for line in format_trump_banner(sig):
        print(line)
    print()
    print("[full JSON]")
    print(json.dumps(sig, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    _cli_main()
