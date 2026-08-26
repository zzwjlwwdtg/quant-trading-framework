"""snapshot_generator.py — 把 webui 快照冻结成 docs/*.json + docs/index.html.

用于 GitHub Pages 部署 → 别人只能查看不能操作。

流程：
  1. 遍历所有 "公开" endpoint（跳过 owner-only 私人 endpoint）
  2. 对每张 signal / JP 卡的 per-ticker endpoint 迭代 watch list
  3. 把 JSON response 存到 docs/data/*.json（文件名 encode URL 参数）
  4. 从 agents/dashboard.html 生成 docs/index.html：
     · 注入 window.STATIC_SNAPSHOT_MODE = true
     · body class="static-snapshot"（隐藏 owner-only 面板，显示 banner/footer）
     · 注入快照时间戳

跑法：
  python snapshot_generator.py                   # 快照 + 写 docs/
  python snapshot_generator.py --only signals    # 只快照单个 endpoint（debug）
  python snapshot_generator.py --skip-tickers    # 跳 per-ticker（快速全局刷新）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR   = Path(__file__).parent
REPO_ROOT  = BASE_DIR.parent
DOCS_DIR   = REPO_ROOT / "docs"
DATA_DIR   = DOCS_DIR / "data"
DASH_HTML  = BASE_DIR / "dashboard.html"
LAST_RUN_FILE = BASE_DIR / ".snapshot_last_run"
OFF_HOURS_INTERVAL_SEC = 2 * 3600 - 300   # 2h - 5min buffer, 防 cron 抖动错过

WEBUI_HOST = "127.0.0.1"
WEBUI_PORT = 8080
BASE_URL   = f"http://{WEBUI_HOST}:{WEBUI_PORT}"

# 公开 endpoints（无参数）—— 全部快照
GLOBAL_ENDPOINTS = [
    "/api/health",
    "/api/signals",
    "/api/sectors",
    "/api/oil",
    "/api/banners",       # trump + gold sentiment banners (公开衍生信号)
    "/api/hmm",           # market regime detection (bull_low_vol/crisis/…) 公开市场信号
    "/api/bond_monitor",  # 美债 yields + TIPS + GLD 20d correlation + anomaly
    "/api/bond_ai_interpret",  # AI CLI 大白话解读 bond_monitor 数字
    "/api/thesis_forecast",    # 未来 45 天事件场景预测 (dovish/base/hawkish × 概率)
    "/api/thesis_history",     # 180d cut_prob 演化 (mini chart 用)
    "/api/trump_attribution",  # Trump 帖子对 cut_prob 影响归因
    "/api/policy_toolkit",     # 美债救援政策工具追踪 (回购/YCC/TGA/... × 30Y 反应)
    "/api/liquidity_history",  # 26 年流动性历史 (MOVE + funding + bank, monthly)
    "/api/fed_watch",     # CME FedWatch 加息预期 (AI CLI + live web search)
    "/api/ai_analysis",
    "/api/ai_targets",    # AI CLI 结构化交易目标 (paper_trader 用来挂 GTC 限价 + SELL STOP)
    "/api/ticker_options",
    "/api/options_flow",
    "/api/ticker_ai",
    "/api/events",
    "/api/jp_watch",
    "/api/option_walls_chart",
]

# Per-ticker endpoints — 迭代 watch list
# route → applies to ("all" | "us" | "jp")
PER_TICKER_ENDPOINTS = {
    "/api/capital_flow":   "all",
    "/api/fundamentals":   "all",
    "/api/supply_chain":   "us",
    "/api/equity_outlook": "us",   # 美股前瞻共识 + 预期定价（NVDA/MSFT/AAPL/... 6h 缓存）
    "/api/tostnet_hits":   "jp",
    "/api/jp_guidance":    "jp",
}

# 私人 endpoints — 明确跳过
PRIVATE_ENDPOINTS = {
    "/api/nav", "/api/positions", "/api/trades", "/api/log",
    "/api/benchmark", "/api/trump_verify", "/api/institutional",
}


# JP TSE 假日硬编码（moomoo JP 需付费订阅，用 overnight_jp_live 那份 list 保持一致）
_JP_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",  # 三が日
    "2026-01-12",  # 成人の日
    "2026-02-11",  # 建国記念の日
    "2026-02-23",  # 天皇誕生日
    "2026-03-20",  # 春分の日
    "2026-04-29",  # 昭和の日
    "2026-05-04", "2026-05-05", "2026-05-06",  # みどり/こどもの日/振替
    "2026-07-20",  # 海の日
    "2026-08-11",  # 山の日
    "2026-09-21",  # 敬老の日
    "2026-09-23",  # 秋分の日
    "2026-10-12",  # スポーツの日
    "2026-11-03",  # 文化の日
    "2026-11-23",  # 勤労感謝の日
    "2026-12-31",  # 大晦日 (TSE 休市)
}
# US 假日通过 moomoo API 查（免费 tier 支持）；假期缓存 6h
_US_HOLIDAY_CACHE: dict[str, tuple[float, bool]] = {}
_HOLIDAY_CACHE_TTL_SEC = 6 * 3600


def _is_us_trading_day(date_str: str) -> bool:
    """查 moomoo request_trading_days，len>0 即 trading day. 6h cache."""
    now = time.time()
    cached = _US_HOLIDAY_CACHE.get(date_str)
    if cached and (now - cached[0]) < _HOLIDAY_CACHE_TTL_SEC:
        return cached[1]
    try:
        from moomoo_pool import get_quote_ctx
        from moomoo import RET_OK, TrdMarket
        ctx = get_quote_ctx()
        ret, days = ctx.request_trading_days(market=TrdMarket.US, start=date_str, end=date_str)
        is_td = ret == RET_OK and hasattr(days, "__len__") and len(days) > 0
        _US_HOLIDAY_CACHE[date_str] = (now, is_td)
        return is_td
    except Exception:
        return True   # moomoo 挂了 → 保守 fall through to hour check


def _is_jp_trading_day(date_str: str) -> bool:
    """JP 用硬编码假日 (moomoo JP 需付费)。周末在上层 weekday check 里已排除。"""
    return date_str not in _JP_HOLIDAYS_2026


def _market_open_now() -> tuple[bool, str]:
    """任一市场活跃时段？(US regular + pre/post ext, JP regular)。返 (open, label).

    过滤：weekend + market holidays (US via moomoo, JP via hardcoded)。
    """
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    now_jst = datetime.now(ZoneInfo("Asia/Tokyo"))
    us_date = now_et.strftime("%Y-%m-%d")
    jp_date = now_jst.strftime("%Y-%m-%d")
    # US 扩展交易时段 04:00-20:00 ET Mon-Fri (盘前 4-9:30, regular 9:30-16, 盘后 16-20)
    us_open = (now_et.weekday() < 5
               and 4 <= now_et.hour < 20
               and _is_us_trading_day(us_date))
    # JP 常规时段 09:00-15:00 JST Mon-Fri (含午休)
    jp_open = (now_jst.weekday() < 5
               and 9 <= now_jst.hour < 15
               and _is_jp_trading_day(jp_date))
    if us_open and jp_open:
        return True, f"US+JP both open (ET {now_et.strftime('%H:%M')} / JST {now_jst.strftime('%H:%M')})"
    if us_open:
        return True, f"US open (ET {now_et.strftime('%H:%M')})"
    if jp_open:
        return True, f"JP open (JST {now_jst.strftime('%H:%M')})"
    # Label 里区分 weekend vs holiday
    us_reason = "weekend" if now_et.weekday() >= 5 else ("holiday" if not _is_us_trading_day(us_date) else "off-hours")
    jp_reason = "weekend" if now_jst.weekday() >= 5 else ("holiday" if not _is_jp_trading_day(jp_date) else "off-hours")
    return False, f"both closed (US {us_reason} · JP {jp_reason})"


def _should_run_now(force: bool = False) -> tuple[bool, str]:
    """自适应决策：市场开 → 每次 30min 都跑；两个都关 → 距上次跑 ≥2h 才跑."""
    if force:
        return True, "force flag"
    market_open, mkt_label = _market_open_now()
    if market_open:
        return True, f"market open [{mkt_label}]"
    # 盘外：查上次跑时间
    try:
        last = float(LAST_RUN_FILE.read_text().strip())
        elapsed = time.time() - last
        if elapsed >= OFF_HOURS_INTERVAL_SEC:
            return True, f"off-hours 2h elapsed ({elapsed/60:.0f}min since last)"
        return False, f"off-hours skip ({elapsed/60:.0f}min < 2h) [{mkt_label}]"
    except Exception:
        return True, f"off-hours first-run [{mkt_label}]"


def _mark_run_completed() -> None:
    try:
        LAST_RUN_FILE.write_text(str(time.time()))
    except Exception:
        pass


def _fetch(url: str, timeout: int = 60) -> dict | None:
    """HTTP GET → JSON dict, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "snapshot-generator/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ⚠ fetch fail {url[:80]}: {str(e)[:80]}")
        return None


def _url_to_filename(url: str) -> str:
    """/api/capital_flow?ticker=5857.T → capital_flow__ticker-5857.T.json"""
    path = url.lstrip("/")
    if path.startswith("api/"):
        path = path[4:]
    if "?" in path:
        route, query = path.split("?", 1)
        encoded = query.replace("=", "-").replace("&", "__")
        return f"{route}__{encoded}.json"
    return f"{path}.json"


def _looks_empty(data: dict) -> bool:
    """探测 webui 返回的 payload 是不是"实际啥都没有"（backend degraded）。
    避免把老快照覆盖成空 dict —— 之前 /api/signals 在 OpenD 挂时返 {tickers:{}}
    直接覆盖导致 dashboard 空白。"""
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return True
    # 常见空 payload 模式
    for k in ("tickers", "items", "data", "boards", "results", "rows"):
        v = data.get(k)
        if isinstance(v, (dict, list)) and len(v) == 0:
            # 只有这一个 key 空 = 空快照；有其它数据字段 = 是正常的 no-data 状态
            other_keys = [x for x in data.keys() if x != k and not x.startswith("_") and x not in ("ts", "log_path", "log_stale", "log_note", "generated_at")]
            if not any(data.get(ok) for ok in other_keys):
                return True
    return False


# 快照开始前记录老文件大小（用于 _save 守卫，即使 full-refresh 清了目录也不丢参考）
_PRE_SNAPSHOT_SIZES: dict[str, int] = {}


def _snapshot_old_sizes() -> None:
    """在 DATA_DIR 清空前记录每个 JSON 文件的字节数，供 _save 守卫参考。"""
    _PRE_SNAPSHOT_SIZES.clear()
    if not DATA_DIR.exists():
        return
    for f in DATA_DIR.glob("*.json"):
        try:
            _PRE_SNAPSHOT_SIZES[f.name] = f.stat().st_size
        except OSError:
            pass


def _save(url: str, data: dict) -> Path | None:
    """Save data to docs/data/<encoded>.json.
    Guards against overwriting a healthy snapshot with a degraded empty one:
      - 若 payload 空 且 老文件（快照前记录）> 5x 大 → 拒绝写入, 保留 old 文件。
      - 若 full-refresh 已把 old 文件删掉 → 恢复它（用 git checkout 或不删）。"""
    fname = _url_to_filename(url)
    out = DATA_DIR / fname
    out.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = json.dumps(data, ensure_ascii=False, indent=2)
    if _looks_empty(data):
        old_size = _PRE_SNAPSHOT_SIZES.get(fname) or (out.stat().st_size if out.exists() else 0)
        if old_size > len(new_bytes) * 5 and old_size > 500:
            print(f"  ⚠ SKIP overwrite {fname}: new empty ({len(new_bytes)}B) vs pre-snapshot ({old_size}B) — 试图恢复旧快照")
            # 如果 full-refresh 清空了目录，从 git HEAD 恢复
            if not out.exists():
                try:
                    import subprocess
                    subprocess.run(
                        ["git", "checkout", "HEAD", "--", str(out.relative_to(REPO_ROOT))],
                        cwd=str(REPO_ROOT), check=True, capture_output=True, timeout=10,
                    )
                    print(f"    ✓ 从 git HEAD 恢复 {fname}")
                except Exception as e:
                    print(f"    ✗ git restore 失败: {e}")
            return None
    out.write_text(new_bytes, encoding="utf-8")
    return out


def _watch_tickers() -> tuple[list[str], list[str]]:
    """从 /api/signals 拿 US 主 ticker 集合；从 /api/jp_watch 拿 JP 集合。

    JP 采用 **short name**（TDK/ARE/MUFG…）而非 .T symbol：dashboard.html JP watch
    卡代码里用的是 `t.ticker` (short name)，webui 后端做 short→symbol 映射。
    静态文件名必须与前端 URL 一致，否则 404。
    """
    us_tickers: list[str] = []
    jp_tickers_short: list[str] = []   # for fundamentals/jp_guidance (JP watch cards)
    jp_tickers_sym:   list[str] = []   # for capital_flow/tostnet_hits (直接调 5857.T)
    sig = _fetch(f"{BASE_URL}/api/signals", timeout=15)
    if sig and isinstance(sig.get("tickers"), dict):
        for tk in sig["tickers"].keys():
            us_tickers.append(tk)
    # Fallback：orchestrator 没跑或 /api/signals 空时（如 OpenD 挂了），从 config 拿完整 universe
    # 保证 equity_outlook / supply_chain 这类不依赖 moomoo 的静态快照仍能生成
    if not us_tickers:
        try:
            from config import TICKERS, TRACKED_TICKERS
            for tk_full in list(TICKERS) + list(TRACKED_TICKERS):
                tk = tk_full.replace("US.", "")
                if tk not in us_tickers:
                    us_tickers.append(tk)
            print(f"  [fallback] /api/signals 空，从 config 拉 {len(us_tickers)} 个 US ticker")
        except Exception as e:
            print(f"  [fallback] config import 失败: {e}")
    jpw = _fetch(f"{BASE_URL}/api/jp_watch", timeout=15)
    if jpw and isinstance(jpw.get("tickers"), list):
        for entry in jpw["tickers"]:
            short = entry.get("ticker")
            sym   = entry.get("symbol") or (short + ".T" if short else None)
            if short:
                jp_tickers_short.append(short)
            if sym:
                jp_tickers_sym.append(sym)
    return us_tickers, jp_tickers_short, jp_tickers_sym


def snapshot_all(only: str | None = None, skip_tickers: bool = False) -> dict:
    """执行全量快照。返回 stats dict."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 清目录之前记录老文件大小，供 _save 的空 payload 守卫参考
    _snapshot_old_sizes()
    # Full refresh 时清 stale JSON（防止 ticker 改名后老文件留下来占位）
    if not only and not skip_tickers:
        for old in DATA_DIR.glob("*.json"):
            try: old.unlink()
            except OSError: pass
    started = time.time()
    ok, fail = 0, 0
    saved_files: list[str] = []

    # 1) 全局 endpoints
    global_todo = [e for e in GLOBAL_ENDPOINTS if not only or only in e]
    for ep in global_todo:
        url = f"{BASE_URL}{ep}"
        print(f"[global] {ep}")
        data = _fetch(url)
        if data is not None:
            path = _save(ep, data)
            if path is not None:
                saved_files.append(path.name)
            ok += 1
        else:
            fail += 1

    # 2) Per-ticker endpoints
    if not skip_tickers:
        us_tickers, jp_short, jp_sym = _watch_tickers()
        print(f"[watch] US={len(us_tickers)} JP short={len(jp_short)} sym={len(jp_sym)}")

        # JP-side per-endpoint ticker format (must match how dashboard.html calls fetchJson)
        # capital_flow / tostnet_hits: dashboard 传 t.symbol (5857.T)
        # fundamentals / jp_guidance / supply_chain: dashboard 传 t.ticker (ARE)
        JP_TICKER_FORM = {
            "/api/capital_flow": jp_sym,
            "/api/tostnet_hits": jp_sym,
            "/api/fundamentals": jp_short,
            "/api/jp_guidance":  jp_short,
            "/api/supply_chain": jp_short,
        }

        for route, scope in PER_TICKER_ENDPOINTS.items():
            if only and only not in route:
                continue
            targets = []
            if scope in ("all", "us"):
                targets.extend(us_tickers)
            if scope in ("all", "jp"):
                targets.extend(JP_TICKER_FORM.get(route, jp_sym))
            for tk in targets:
                # 特殊 endpoint 需要额外 query 参数（与 dashboard.html fetchJson 调用签名对齐）
                query_extra = ""
                if route == "/api/tostnet_hits":
                    query_extra = "&days=10"
                elif route == "/api/fundamentals":
                    query_extra = "&period=year"
                ep = f"{route}?ticker={urllib.parse.quote(tk)}{query_extra}"
                url = f"{BASE_URL}{ep}"
                print(f"[ticker] {ep}")
                data = _fetch(url)
                if data is not None:
                    path = _save(ep, data)
                    saved_files.append(path.name)
                    ok += 1
                else:
                    fail += 1

    # 3) Snapshot metadata
    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_local": datetime.now().isoformat(timespec="seconds"),
        "endpoints_ok":     ok,
        "endpoints_fail":   fail,
        "elapsed_sec":      round(time.time() - started, 1),
        "saved_files":      sorted(saved_files),
    }
    (DATA_DIR / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return meta


def build_static_html(meta: dict) -> Path:
    """生成 docs/index.html 从 agents/dashboard.html + 静态模式注入."""
    if not DASH_HTML.exists():
        raise FileNotFoundError(f"dashboard source missing: {DASH_HTML}")
    src = DASH_HTML.read_text(encoding="utf-8")

    # 1) body 加 static-snapshot class
    transformed = src.replace("<body>", '<body class="static-snapshot">', 1)

    # 2) 在 <head> 尾部注入 STATIC_SNAPSHOT_MODE 全局
    static_meta = json.dumps({
        "generated_at": meta["generated_at_local"],
        "endpoints_ok": meta["endpoints_ok"],
        "endpoints_fail": meta["endpoints_fail"],
    }, ensure_ascii=False)
    inject = (
        "<script>\n"
        "  window.STATIC_SNAPSHOT_MODE = true;\n"
        f"  window.SNAPSHOT_META = {static_meta};\n"
        "  document.addEventListener('DOMContentLoaded', () => {\n"
        "    const el = document.getElementById('snapshot-ts');\n"
        "    if (el && window.SNAPSHOT_META) el.textContent = window.SNAPSHOT_META.generated_at;\n"
        "  });\n"
        "</script>\n"
    )
    transformed = transformed.replace("</head>", inject + "</head>", 1)

    out = DOCS_DIR / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(transformed, encoding="utf-8")
    return out


def write_readme():
    """docs/README.md 说明这是 GitHub Pages 静态快照，防止有人误认为源码。"""
    (DOCS_DIR / "README.md").write_text(
        "# Public Snapshot (GitHub Pages)\n\n"
        "This folder hosts a **read-only snapshot** of the quant trading dashboard,\n"
        "auto-generated every ~30 minutes by `agents/snapshot_generator.py`.\n\n"
        "Live URL: https://zzwjlwwdtg.github.io/quant-trading-framework/\n\n"
        "**Do not edit files here manually** — they are overwritten by the generator.\n\n"
        "For source code / architecture, see [../README.md](../README.md) and\n"
        "the `agents/` folder.\n",
        encoding="utf-8",
    )


def write_nojekyll():
    """GitHub Pages by default runs Jekyll which ignores files starting with _. Prevent."""
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--only", type=str, default=None, help="仅快照包含此关键字的 endpoint")
    p.add_argument("--skip-tickers", action="store_true", help="只跑 global endpoints，不迭代 tickers")
    p.add_argument("--no-html", action="store_true", help="只生成 data JSON，不重建 index.html")
    p.add_argument("--force", action="store_true", help="强制运行，不管市场时段/上次时间")
    args = p.parse_args()

    try:
        # 自适应频率：市场开 → 每 30 min；两个都关 → ≥2h 才跑
        should, reason = _should_run_now(force=args.force)
        if not should:
            print(f"== snapshot SKIP {datetime.now().isoformat(timespec='seconds')} — {reason} ==")
            return

        print(f"== snapshot start {datetime.now().isoformat(timespec='seconds')} — {reason} ==")
        meta = snapshot_all(only=args.only, skip_tickers=args.skip_tickers)
        print(f"data: ok={meta['endpoints_ok']} fail={meta['endpoints_fail']} elapsed={meta['elapsed_sec']}s")

        if not args.no_html:
            out = build_static_html(meta)
            print(f"html: wrote {out}")
            write_readme()
            write_nojekyll()

        _mark_run_completed()
        print(f"== done. docs at {DOCS_DIR} ==")
    finally:
        # moomoo OpenQuoteContext owns worker threads. Explicitly close it so
        # short-lived Task Scheduler jobs can return to snap_public.bat, finish
        # git commit/push, and let the terminal close by itself.
        try:
            from moomoo_pool import close_quote_ctx
            close_quote_ctx()
        except Exception:
            pass


if __name__ == "__main__":
    main()
