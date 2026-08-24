"""
webui.py
─────────
零依赖 WebUI — 用 Python 自带 http.server + 单页 dashboard.html。

启动：python webui.py  或  webui.bat
访问：http://127.0.0.1:8080

API 端点：
  GET /                   → dashboard.html
  GET /api/health         → { orchestrator_alive, opend_alive, last_log_ts, uptime }
  GET /api/nav            → NAV 历史 + peak + dd_pct
  GET /api/positions      → moomoo 当前持仓（如可用）
  GET /api/institutional  → 组合风险/压力/归因/杠杆路径/成交质量
  GET /api/trades?n=20    → 最近 N 笔 trade_log
  GET /api/log?n=100      → 当天 log 最后 N 行
  GET /api/benchmark      → 若有 signals/benchmark_report.md 则读
  GET /api/equity_outlook → 美股前瞻共识 + 预期定价匹配

安全：默认只绑 127.0.0.1，仅本机可访问。
"""
from __future__ import annotations

import ctypes
import copy
import hashlib
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jp_watch_contracts import (
    Q3_MACRO_THESIS,
    classify_ichimoku_position as _classify_ichimoku_position,
    compute_ichimoku_series as _compute_ichimoku_series,
    jp_earnings_event_state as _jp_earnings_event_state,
    jp_thesis_fit as _jp_thesis_fit,
    official_bank_fundamentals as _official_bank_fundamentals,
    official_jp_guidance as _official_jp_guidance,
)

SCRIPT_DIR = Path(__file__).parent
SIGNALS_DIR = SCRIPT_DIR / "signals"
LOGS_DIR    = SCRIPT_DIR / "logs"
LOCK_PATH   = SCRIPT_DIR / ".orchestrator.lock"
STATE_PATH  = SCRIPT_DIR / "trader_state.json"

# ---- 通用后台缓存 ----
# 页面刷新时优先返回上一版结果（1ms），后台线程算新值刷新缓存。
# 前端根据 refreshing 标志显示"更新中..."小图标。
_WEBUI_CACHE_DIR = SCRIPT_DIR / ".webui_cache"
_WEBUI_CACHE_DIR.mkdir(exist_ok=True)
_refresh_flag: dict[str, bool] = {}
_refresh_locks: dict[str, threading.Lock] = {}


def _cached(name: str, ttl_sec: int, compute_fn, first_call_async: bool = False,
            first_call_placeholder: dict | None = None, cache_validator=None):
    """返回缓存 JSON + 元数据 (cached_at / age_sec / stale / refreshing)。
    过期时后台线程刷新，本次请求仍返回旧值（首次无缓存时同步计算）。

    first_call_async=True: 首次无缓存也走后台线程，本次返回 first_call_placeholder
    （避免 AI CLI 等慢查询把 HTTP handler 阻塞几十秒）。
    """
    cache_path = _WEBUI_CACHE_DIR / f"{name}.json"
    now = time.time()
    data = None
    age_sec: float | None = None
    if cache_path.exists():
        try:
            age_sec = now - cache_path.stat().st_mtime
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
    contract_valid = True
    if data is not None and cache_validator is not None:
        try:
            contract_valid = bool(cache_validator(data))
        except Exception:
            contract_valid = False
    stale = (
        data is None
        or (age_sec is not None and age_sec > ttl_sec)
        or not contract_valid
    )

    def _spawn_refresh():
        if _refresh_flag.get(name):
            return
        lock = _refresh_locks.setdefault(name, threading.Lock())

        def _bg_refresh():
            with lock:
                if _refresh_flag.get(name):
                    return
                _refresh_flag[name] = True
                try:
                    fresh = compute_fn()
                    cache_path.write_text(
                        json.dumps(fresh, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception:
                    pass
                finally:
                    _refresh_flag[name] = False

        threading.Thread(target=_bg_refresh, daemon=True).start()

    # 后台刷新：有旧缓存过期 → 直接后台；或 first_call_async 允许首次也后台
    if stale and (data is not None or first_call_async):
        _spawn_refresh()

    if data is None:
        if first_call_async:
            # 首次无缓存 + 异步模式：返 placeholder，让浏览器下次刷新拿真值
            placeholder = dict(first_call_placeholder or {})
            placeholder["_meta"] = {
                "cached_at":  None,
                "age_sec":    0,
                "stale":      True,
                "refreshing": True,
                "first_call": True,
            }
            return placeholder
        # 首次请求同步计算（默认路径）
        try:
            data = compute_fn()
            cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            age_sec = 0
            stale = False
        except Exception as e:
            return {"error": str(e), "refreshing": False, "stale": True, "age_sec": None}

    return {
        **data,
        "_meta": {
            "cached_at":  datetime.fromtimestamp(cache_path.stat().st_mtime).isoformat() if cache_path.exists() else None,
            "age_sec":    int(age_sec) if age_sec is not None else 0,
            "stale":      bool(stale),
            "refreshing": bool(_refresh_flag.get(name, False)),
        },
    }

PORT = int(os.environ.get("WEBUI_PORT", "8080"))
HOST = os.environ.get("WEBUI_HOST", "127.0.0.1")


# ---- Dashboard display capabilities ----
# Keep asset classification and card capabilities in one backend contract. The
# frontend consumes this profile instead of maintaining a second ticker list.
_DEFAULT_TICKER_DISPLAY_PROFILE = {
    "card_group": "main",
    "asset_class": "equity",
    "asset_label_zh": "股票 / 股票 ETF",
    "show_options": True,
    "show_fundamentals": True,
    "show_forward_outlook": True,
    "show_ai_analysis": True,
    "show_supply_chain": True,
    "fundamental_profile": "industrial",
}

TICKER_DISPLAY_PROFILES = {
    "GLD": {
        "card_group": "macro",
        "asset_class": "commodity",
        "asset_label_zh": "黄金大宗商品",
        "show_options": False,
        "show_fundamentals": False,
        "show_forward_outlook": False,
        "show_ai_analysis": False,
        "show_supply_chain": False,
    },
    "SHY": {
        "card_group": "macro",
        "asset_class": "fixed_income",
        "asset_label_zh": "1–3 年美国国债",
        "show_options": False,
        "show_fundamentals": False,
        "show_forward_outlook": False,
        "show_ai_analysis": False,
        "show_supply_chain": False,
    },
    "IEI": {
        "card_group": "macro",
        "asset_class": "fixed_income",
        "asset_label_zh": "3–7 年美国国债",
        "show_options": False,
        "show_fundamentals": False,
        "show_forward_outlook": False,
        "show_ai_analysis": False,
        "show_supply_chain": False,
    },
    "MUFG": {
        "card_group": "jp",
        "asset_class": "bank",
        "asset_label_zh": "日本银行",
        "show_options": False,
        "show_fundamentals": True,
        "show_forward_outlook": False,
        "show_ai_analysis": False,
        "show_supply_chain": False,
        "fundamental_profile": "bank",
    },
}


def ticker_display_profile(ticker: str) -> dict:
    """Return a copy of the dashboard capability profile for ``ticker``."""
    tk = (ticker or "").upper().removeprefix("US.")
    return {**_DEFAULT_TICKER_DISPLAY_PROFILE, **TICKER_DISPLAY_PROFILES.get(tk, {})}


def _trump_summary_recent3(items: list) -> dict:
    """对最近 3 条推文调 AI CLI 出一句话概括。缓存 by post_ids sha1。非阻塞。

    返回 {"text": str|None, "state": "cached"|"generating"|"empty"|"error", "post_ids": [...]}
    - 命中缓存 → text 有值
    - 未命中且未在生成 → 启动后台线程调 AI CLI, 返回 state=generating text=None
    - 已有其它总结在跑 → 返回 state=generating
    """
    import hashlib
    if not items:
        return {"text": None, "state": "empty", "post_ids": []}
    # 按 post_time 降序取前 3
    def _pt(it):
        return it.get("post_time", "")
    top3 = sorted(items, key=_pt, reverse=True)[:3]
    ids = [it.get("post_id", "") for it in top3]
    key = hashlib.sha1("_".join(ids).encode("utf-8")).hexdigest()[:12]
    cache_path = _WEBUI_CACHE_DIR / f"trump_summary_{key}.txt"
    if cache_path.exists():
        return {"text": cache_path.read_text(encoding="utf-8").strip(),
                "state": "cached", "post_ids": ids}

    # 未命中：启动后台线程调 AI CLI
    if not _refresh_flag.get(f"trump_summary_{key}"):
        def _bg():
            _refresh_flag[f"trump_summary_{key}"] = True
            try:
                from ai_prompt import query_ai_cli
                previews = "\n".join(
                    f"{i+1}. [{it.get('event_type','')}] {(it.get('preview','') or '')[:200]}"
                    for i, it in enumerate(top3)
                )
                prompt = (
                    "请用一句中文（不超过 50 字）概括下面 3 条 Trump 推文的核心内容 + 市场情绪倾向。\n"
                    "只输出概括，不要前后解释，不要 markdown。\n\n"
                    f"推文：\n{previews}\n"
                )
                out, status, _, _ = query_ai_cli(prompt, timeout=60)
                if out and out.strip():
                    cache_path.write_text(out.strip(), encoding="utf-8")
            except Exception:
                pass
            finally:
                _refresh_flag[f"trump_summary_{key}"] = False
        threading.Thread(target=_bg, daemon=True).start()

    return {"text": None, "state": "generating", "post_ids": ids}


def _pid_alive(pid: int) -> bool:
    try:
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x0400, 0, pid)
        if not h:
            return False
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
        kernel32.CloseHandle(h)
        return exit_code.value == 259
    except Exception:
        return False


def _port_open(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except Exception:
        return False


def _today_log_path() -> Path:
    return LOGS_DIR / f"run_{datetime.now().strftime('%Y%m%d')}.log"


def api_health() -> dict:
    orch_pid = None
    orch_alive = False
    if LOCK_PATH.exists():
        try:
            orch_pid = int(LOCK_PATH.read_text().strip())
            orch_alive = _pid_alive(orch_pid)
        except Exception:
            pass
    opend_alive = _port_open("127.0.0.1", 11111)
    log_path = _today_log_path()
    last_log_ts = None
    if log_path.exists():
        try:
            last_log_ts = datetime.fromtimestamp(log_path.stat().st_mtime).isoformat()
        except Exception:
            pass
    return {
        "orchestrator_pid":    orch_pid,
        "orchestrator_alive":  orch_alive,
        "opend_alive":         opend_alive,
        "last_log_mtime":      last_log_ts,
        "log_path":            str(log_path),
        "now":                 datetime.now(timezone.utc).isoformat(),
    }


def api_nav(days: int = 30) -> dict:
    hist_path = SIGNALS_DIR / "nav_history.jsonl"
    if not hist_path.exists():
        return {"points": [], "peak": None, "current": None, "dd_pct": 0}
    points = []
    with open(hist_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                points.append(json.loads(line))
            except Exception:
                continue
    # 按天取最后一个
    from collections import OrderedDict
    day_last = OrderedDict()
    for p in points:
        try:
            d = p["ts"][:10]
            day_last[d] = p
        except Exception:
            continue
    tail = list(day_last.values())[-days:]
    if not tail:
        return {"points": [], "peak": None, "current": None, "dd_pct": 0}
    peak = max(p.get("peak", 0) for p in tail)
    current = tail[-1].get("nav", 0)
    dd = tail[-1].get("dd_pct", 0)
    return {
        "points":  [{"ts": p["ts"][:19], "nav": p.get("nav"), "peak": p.get("peak")}
                    for p in tail],
        "peak":    peak,
        "current": current,
        "dd_pct":  dd,
    }


def _fetch_moomoo_live_positions() -> list[dict] | None:
    """实时查 moomoo SIMULATE 账户全部持仓。失败返 None."""
    try:
        from paper_trader import _ctx_get, ACC_ID, TRD_ENV
        from moomoo import RET_OK
        ctx = _ctx_get()
        ret, pos = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
        if ret != RET_OK or pos is None or (hasattr(pos, "empty") and pos.empty):
            return []
        rows = []
        for _, r in pos.iterrows():
            code = str(r.get("code", ""))
            qty  = int(r.get("qty", 0) or 0)
            if not code or qty == 0:
                continue
            # moomoo pl_ratio 返回的是百分比 (e.g. 106.5 = +106.5%)，
            # 前端 fmtPct 会 ×100，故这里 ÷100 归一到小数 (0.5 = 50%)
            raw_pl_ratio = float(r.get("pl_ratio", 0) or 0)
            rows.append({
                "code":          code,
                "qty":           qty,
                "cost_price":    float(r.get("cost_price", 0) or 0),
                "current_price": float(r.get("nominal_price", 0) or 0),
                "market_val":    float(r.get("market_val", 0) or 0),
                "pl_val":        float(r.get("pl_val", 0) or 0),
                "pl_ratio":      raw_pl_ratio / 100.0,   # 归一到小数
            })
        return rows
    except Exception:
        return None


def api_positions() -> dict:
    """moomoo 实时持仓 + 本地 state metadata (entry_conf/pyramid_layer 等)。

    数据源优先级：
    1. moomoo position_list_query() = 真实持仓（含手动下单）
    2. trader_state.json = 附加 orchestrator 决策 metadata
    3. Merge 后 dashboard 显示（含 pnl / 成本价 / 现价）

    缓存 60s 避免频繁查 moomoo，同时保证 sync latency ≤1min。
    """
    def _compute():
        mm_positions = _fetch_moomoo_live_positions()
        state = {}
        if STATE_PATH.exists():
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        peak_meta = state.get("__nav_peak", {})

        positions = []
        seen_codes: set[str] = set()

        # A) moomoo 真实持仓（含手动下单）—— 权威源
        if mm_positions is not None:
            for mp in mm_positions:
                code = mp["code"]
                st = state.get(code, {}) if isinstance(state.get(code), dict) else {}
                seen_codes.add(code)
                positions.append({
                    "ticker":         code,
                    "qty":            mp["qty"],
                    "cost_price":     round(mp["cost_price"], 2),
                    "current_price":  round(mp["current_price"], 2),
                    "market_val":     round(mp["market_val"], 2),
                    "pl_val":         round(mp["pl_val"], 2),
                    "pl_ratio":       round(mp["pl_ratio"], 4),
                    # 本地 state 附加 metadata（可能缺失如手动下单）
                    "last_action":    st.get("last_action"),
                    "last_side":      st.get("last_side"),
                    "last_qty":       st.get("last_qty"),
                    "last_price":     st.get("last_price"),
                    "last_time_utc":  st.get("last_time_utc"),
                    "is_core":        st.get("is_core", False),
                    "pyramid_layer":  st.get("pyramid_layer"),
                    "entry_conf":     st.get("entry_conf"),
                    "entry_price":    st.get("entry_price"),
                    "entry_high":     st.get("entry_high"),
                    "source":         "moomoo_live" if st else "moomoo_manual",
                })

        # B) state 里有但 moomoo 已无（例如 24h 内平仓，方便看历史动作）
        for k, v in state.items():
            if k.startswith("__") or k in seen_codes:
                continue
            if isinstance(v, dict) and v.get("last_action"):
                try:
                    lt = v.get("last_time_utc") or ""
                    keep = False
                    if lt:
                        t = datetime.fromisoformat(lt.replace("Z", "+00:00"))
                        keep = (datetime.now(timezone.utc) - t).total_seconds() < 86400
                    if keep or v.get("last_action") in ("BUY", "PYRAMID", "REBUY"):
                        positions.append({
                            "ticker":       k,
                            "qty":          0,
                            "last_action":  v.get("last_action"),
                            "last_side":    v.get("last_side"),
                            "last_qty":     v.get("last_qty"),
                            "last_price":   v.get("last_price"),
                            "last_time_utc": v.get("last_time_utc"),
                            "is_core":      v.get("is_core", False),
                            "pyramid_layer": v.get("pyramid_layer"),
                            "entry_conf":   v.get("entry_conf"),
                            "entry_price":  v.get("entry_price"),
                            "entry_high":   v.get("entry_high"),
                            "source":       "state_only_recent",
                        })
                except Exception:
                    pass

        return {
            "positions":    positions,
            "peak_nav":     peak_meta.get("peak_nav"),
            "current_nav":  peak_meta.get("current_nav"),
            "sync_source":  "moomoo_live" if mm_positions is not None else "state_only_fallback",
            "moomoo_ok":    mm_positions is not None,
        }

    return _cached("positions_live", ttl_sec=60, compute_fn=_compute)


def api_trades(n: int = 20) -> dict:
    path = SIGNALS_DIR / "trade_log.jsonl"
    if not path.exists():
        return {"trades": []}
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)
    tail = lines[-n:]
    trades = []
    for line in reversed(tail):   # 最新在前
        try:
            trades.append(json.loads(line))
        except Exception:
            continue
    return {"trades": trades, "total": len(lines)}


def _institutional_market_inputs(positions: list[dict]) -> tuple[dict, dict, dict]:
    """Fetch 6m close history and 20d ADV for held names and leverage proxies."""
    histories: dict[str, list[float]] = {}
    proxy_histories: dict[str, list[float]] = {}
    average_volume: dict[str, float] = {}
    try:
        import yfinance as yf
        from leveraged_etf_risk import leveraged_profile
    except Exception:
        return histories, proxy_histories, average_volume
    symbols = set()
    proxies = {"SPY"}
    for position in positions:
        ticker = str(position.get("ticker") or "").replace("US.", "")
        if not ticker or float(position.get("qty") or 0) <= 0:
            continue
        symbols.add(ticker)
        profile = leveraged_profile(ticker)
        if profile:
            proxies.add(str(profile["proxy"]))
    for symbol in sorted(symbols | proxies):
        try:
            history = yf.Ticker(symbol).history(period="6mo", interval="1d", auto_adjust=True)
            if history is None or history.empty:
                continue
            closes = [float(x) for x in history["Close"].dropna().tolist()]
            if symbol in symbols:
                histories[symbol] = closes
                if "Volume" in history:
                    volume = history["Volume"].dropna().tail(20)
                    if len(volume):
                        average_volume[symbol] = float(volume.mean())
            if symbol in proxies:
                proxy_histories[symbol] = closes
        except Exception:
            continue
    return histories, proxy_histories, average_volume


def api_institutional() -> dict:
    """Institutional measurement layer for the paper portfolio."""
    def _compute():
        from execution_analytics import summarize_execution_log
        from portfolio_analytics import analyze_portfolio

        position_payload = api_positions()
        positions = [dict(p) for p in position_payload.get("positions", [])]
        histories, proxy_histories, average_volume = _institutional_market_inputs(positions)
        for position in positions:
            symbol = str(position.get("ticker") or "").replace("US.", "")
            if symbol in average_volume:
                position["avg_daily_volume"] = average_volume[symbol]
        nav = float(position_payload.get("current_nav") or 0)
        market_value = sum(float(p.get("market_val") or 0) for p in positions)
        report = analyze_portfolio(
            positions,
            nav=nav or market_value,
            cash=(nav - market_value) if nav else 0.0,
            histories=histories,
            proxy_histories=proxy_histories,
            benchmark_history=proxy_histories.get("SPY"),
        )
        quality_path = SIGNALS_DIR / "data_quality_report.json"
        try:
            report["data_quality"] = json.loads(quality_path.read_text(encoding="utf-8"))
        except Exception:
            report["data_quality"] = {"status": "unavailable", "checked_files": 0}
        report["execution_quality"] = summarize_execution_log(
            SIGNALS_DIR / "execution_ledger.jsonl"
        )
        validation = []
        for path in sorted(SIGNALS_DIR.glob("backtest_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rules = payload.get("results") or []
                validation.append({
                    "ticker": payload.get("ticker") or path.stem.replace("backtest_", ""),
                    "rules": len(rules),
                    "walk_forward_passed": sum(
                        1 for rule in rules if (rule.get("walk_forward") or {}).get("passed")
                    ),
                })
            except Exception:
                continue
        report["research_validation"] = validation
        report["position_sync_source"] = position_payload.get("sync_source")
        return report

    return _cached("institutional_live", ttl_sec=600, compute_fn=_compute, first_call_async=True)


def api_log(n: int = 100) -> dict:
    path = _today_log_path()
    if not path.exists():
        return {"lines": [], "path": str(path)}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = [ln.rstrip() for ln in all_lines[-n:]]
        return {"lines": tail, "path": str(path), "total": len(all_lines)}
    except Exception as e:
        return {"lines": [], "path": str(path), "error": str(e)}


def api_benchmark() -> dict:
    path = SIGNALS_DIR / "benchmark_report.md"
    if not path.exists():
        return {"exists": False}
    try:
        content = path.read_text(encoding="utf-8")
        return {"exists": True, "content": content, "path": str(path),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat()}
    except Exception as e:
        return {"exists": False, "error": str(e)}


def api_hmm() -> dict:
    """多周期市场环境；HMM 只作为慢周期背景，绝非买入信号。"""
    path = SIGNALS_DIR / "hmm_state.json"
    if not path.exists():
        return {"exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        trading = {}
        try:
            from regime_today import get_today_info, _market_date
            current = get_today_info()
            state = current or get_today_info(allow_stale=True)
            style = (state.get("short_style") or
                     (state.get("inputs") or {}).get("short_style") or {})
            regime_labels = {
                "bull_trending": "日线趋势偏多",
                "bull_extended": "日线趋势延伸",
                "bull_pulling": "日线偏多但在回调",
                "bull_chop": "偏多背景下震荡",
                "neutral_chop": "中性震荡",
                "risk_off": "风险收缩（价格/板块急跌）",
                "overheated": "过热警戒",
                "recession_risk": "衰退风险",
                "crisis": "危机防御",
                "neutral": "日线中性",
            }
            trading = {
                "regime": state.get("regime") or "neutral",
                "regime_zh": regime_labels.get(state.get("regime"), state.get("regime")),
                "base_regime": state.get("base_regime"),
                "style": style,
                "date": state.get("date"),
                "expected_date": _market_date(),
                "stale": not bool(current),
                "ts": state.get("ts"),
            }
        except Exception as exc:
            trading = {"regime": "neutral", "regime_zh": "日线状态不可用", "error": str(exc)}
        return {
            "exists":     True,
            "label":      data.get("current_label"),
            "prob":       data.get("current_prob"),
            "state":      data.get("current_state"),
            "ts":         data.get("ts"),
            "state_probs":data.get("all_state_probs", {}),
            "scope":      "slow_macro_background",
            "warning":    "慢周期背景，不是当前买入或加仓信号",
            "trading":    trading,
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def api_signals() -> dict:
    """从今日 log 解析最新每标的信号（action / conf / regime / 共振多空数 / 价格）。

    周末/假期/宕机: 今日无 log → fallback 到最近一次 log 文件（保留信号可见性）。
    """
    import re
    path = _today_log_path()
    log_stale = False
    if not path.exists():
        # fallback: 找最新 log 文件（避免周末美股信号全空）
        logs = sorted(LOGS_DIR.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return {"tickers": {}, "log_stale": True, "log_note": "no log files found"}
        path = logs[0]
        log_stale = True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return {"tickers": {}, "log_stale": True, "log_note": "read error"}

    # 倒序扫，抓每个 ticker 最新一次的完整信号块
    #   【TICKER】价格:X.XX  RSI:X.X  量比:X.X  趋势:up/down
    #     信号: XXX 置信度:N/M 依据:XXX 行情:XXX
    #     共振: 多头(X) vs 空头(Y) → XXX
    tickers = {}
    tk_re    = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+【(\w+)】"
                          r"价格:([\d.]+)\s+RSI:([\d.]+)\s+量比:([\d.]+)\s+趋势:(\w+)")
    # 中文 action label 全集（对应 decision_agent 输出）：
    # 关注买入=WATCH_BUY / 主动试探仓=WATCH_BUY_PROBE / 买入=BUY / 卖出=SELL
    # 减仓=REDUCE / 警示=CAUTION / 持仓观望=HOLD
    # 缺任何一个 → api_signals 拿不到 action_zh → dashboard 显示 "?"
    sig_re   = re.compile(r"信号:\s+(\S+)\s+(关注买入|主动试探仓|买入|卖出|减仓|警示|持仓观望)\s+"
                          r"置信度:(\d+)/(\d+).*?行情:(\S+)")
    reson_re = re.compile(r"共振:\s+多头\((\d+)\)\s+vs\s+空头\((\d+)\)")

    # 反向扫更快 — 每 ticker 只留最新
    seen = set()
    for i in range(len(lines) - 1, -1, -1):
        m = tk_re.match(lines[i])
        if not m:
            continue
        tk = m.group(1)
        if tk in seen:
            continue
        seen.add(tk)
        try:
            from config import get_ticker_name_zh
            _name_zh, _meaning_zh = get_ticker_name_zh(tk)
        except Exception:
            _name_zh, _meaning_zh = tk, ""
        info = {
            "ticker":    tk,
            "name_zh":   _name_zh,
            "meaning_zh":_meaning_zh,
            "price":     float(m.group(2)),
            "rsi":       float(m.group(3)),
            "vol_ratio": float(m.group(4)),
            "trend":     m.group(5),
            "ts":        lines[i][:19],
            "display_profile": ticker_display_profile(tk),
        }
        # 找该 ticker 之后 6 行内的 信号 + 共振
        for j in range(i, min(i + 10, len(lines))):
            sm = sig_re.search(lines[j])
            if sm and "action" not in info:
                info["action_zh"] = sm.group(2)
                info["conf"]      = int(sm.group(3))
                info["scale"]     = int(sm.group(4))
                info["regime"]    = sm.group(5)
            rm = reson_re.search(lines[j])
            if rm and "bull_count" not in info:
                info["bull_count"] = int(rm.group(1))
                info["bear_count"] = int(rm.group(2))
        tickers[tk] = info
    return {
        "tickers":   tickers,
        "log_path":  path.name,
        "log_stale": log_stale,
        "log_note":  f"信号来自 {path.stem[4:]}（今日无 log，可能是周末/假期）" if log_stale else None,
    }


def api_trump_verify(text: str) -> dict:
    """核实一句 Trump 引言是否真身发过（Truth Social + X Nitter）。

    用途: 用户看到 X/媒体上的 "Trump 说 xxx"，快速验证真伪。
    匹配逻辑:
      1) 完全 substring 命中 → is_authentic=True high
      2) SequenceMatcher ratio ≥ 0.85 → True high
      3) 0.50 ≤ ratio < 0.85 → 中间地带 (可能复述/断章) → None medium
      4) < 0.50 → False + 触发 Discord/TG 高仿警告
    """
    from difflib import SequenceMatcher
    text = (text or "").strip()
    if len(text) < 10:
        return {"is_authentic": None, "confidence": "low",
                "reason": "查询文本过短（<10 字符），无法比对"}

    query = re.sub(r"\s+", " ", text.lower())[:400]

    try:
        from trump_signal import fetch_recent_posts
        posts, source_label = fetch_recent_posts(hours=168)  # 7 天窗口
    except Exception as e:
        return {"is_authentic": None, "confidence": "low",
                "reason": f"数据源不可用: {e}"}

    if not posts:
        return {"is_authentic": None, "confidence": "low",
                "reason": "近 7 天无真身 Trump 推文数据可比对"}

    best_ratio = 0.0
    best_post = None
    for p in posts:
        content = re.sub(r"\s+", " ", (p.get("content") or "").lower().strip())
        if not content:
            continue
        # 短语完全命中优先
        if query in content or content in query:
            return {
                "is_authentic":  True,
                "confidence":    "high",
                "matched_post":  {"created_at": p.get("created_at"),
                                   "source": p.get("source", "?"),
                                   "content_excerpt": (p.get("content") or "")[:200]},
                "similarity":    1.0,
                "checked_count": len(posts),
                "reason":        f"✓ 完全匹配真身推文（源 {p.get('source','?')}, {p.get('created_at','')[:16].replace('T',' ')}）",
            }
        ratio = SequenceMatcher(None, query, content).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_post = p

    best_summary = {"created_at": best_post.get("created_at"),
                    "source": best_post.get("source", "?"),
                    "content_excerpt": (best_post.get("content") or "")[:200]} if best_post else None

    if best_ratio >= 0.85:
        return {"is_authentic": True, "confidence": "high",
                "matched_post": best_summary, "similarity": round(best_ratio, 3),
                "checked_count": len(posts),
                "reason": f"✓ 高度匹配真身推文（similarity {best_ratio*100:.0f}%）"}
    if best_ratio >= 0.50:
        return {"is_authentic": None, "confidence": "medium",
                "matched_post": best_summary, "similarity": round(best_ratio, 3),
                "checked_count": len(posts),
                "reason": f"⚠ 部分匹配（{best_ratio*100:.0f}%）— 可能是复述/翻译/截取，无法确定真伪"}

    # 明确未匹配 → 触发高仿警报（后台 fire-and-forget）
    def _alert():
        try:
            from notifications import send_alert
            send_alert(
                f"**🚨 Trump 高仿伪造警告**\n"
                f"查询文本: {text[:200]}\n"
                f"真身近 7 天 {len(posts)} 条推文中最接近的相似度仅 **{best_ratio*100:.0f}%**\n"
                f"最接近真身推文: {(best_summary or {}).get('content_excerpt','')[:150]}\n"
                f"→ 疑似 commentary 账号 / AI 伪造 / 断章取义\n"
                f"验证源: {source_label} · {len(posts)} 条真身推文",
                level="trump_fake",
            )
        except Exception:
            pass
    threading.Thread(target=_alert, daemon=True).start()

    return {"is_authentic": False,
            "confidence": "high" if best_ratio < 0.30 else "medium",
            "matched_post": best_summary,
            "similarity": round(best_ratio, 3),
            "checked_count": len(posts),
            "reason": (f"❌ 未匹配真身推文（best similarity {best_ratio*100:.0f}%）。"
                       f"疑似高仿 / commentary 账号 / AI 伪造 / 断章取义。"
                       f"已推送高仿警告到 Discord/TG。")}


def api_banners() -> dict:
    """从最新 signals/snapshot_YYYYMMDD_HHMMSS.txt 解析 Trump / Gold Macro / 期权墙 3 大 banner 概要。"""
    import re
    snapshots = sorted(SIGNALS_DIR.glob("snapshot_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not snapshots:
        return {"exists": False}
    latest = snapshots[0]
    try:
        content = latest.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"exists": False}

    banners = {"snapshot_path": str(latest.name),
                "snapshot_mtime": datetime.fromtimestamp(latest.stat().st_mtime).isoformat()}

    # Trump: "推文数 X  聚合方向 XXX  幅度 XXX  score +N"
    m = re.search(r"推文数\s+(\d+)\s+聚合方向\s+(\w+)\s+幅度\s+(\w+)\s+score\s+([+-]?\d+)", content)
    if m:
        trump = {
            "posts":     int(m.group(1)),
            "direction": m.group(2),
            "magnitude": m.group(3),
            "score":     int(m.group(4)),
            "event_counts": {},
            "breaking":    [],
        }
        # 从 aggregate_latest.json 拉每条 post 的事件分类 + 爆炸新闻
        items: list = []
        try:
            agg_path = SIGNALS_DIR / "trump_cache" / "aggregate_latest.json"
            if agg_path.exists():
                agg = json.loads(agg_path.read_text(encoding="utf-8"))
                items = agg.get("active_signals", []) or []
                # 事件类型计数
                for it in items:
                    et = it.get("event_type", "UNKNOWN")
                    trump["event_counts"][et] = trump["event_counts"].get(et, 0) + 1
                # 爆炸新闻: magnitude in large/extreme OR (非 neutral AND confidence>=7)
                MAG_RANK = {"extreme": 3, "large": 2, "medium": 1, "small": 0}
                def _is_breaking(it: dict) -> bool:
                    mag = it.get("magnitude", "")
                    dr = it.get("direction", "")
                    cf = int(it.get("confidence", 0) or 0)
                    return mag in ("large", "extreme") or (dr != "neutral" and cf >= 7)
                breaking = [it for it in items if _is_breaking(it)]
                # sort by (magnitude rank desc, confidence desc)
                breaking.sort(key=lambda x: (
                    -MAG_RANK.get(x.get("magnitude", ""), 0),
                    -int(x.get("confidence", 0) or 0),
                ))
                trump["breaking"] = [{
                    "event_type": it.get("event_type", ""),
                    "direction":  it.get("direction", ""),
                    "magnitude":  it.get("magnitude", ""),
                    "confidence": it.get("confidence", 0),
                    "tickers":    it.get("tickers_affected", []) or [],
                    "preview":    (it.get("preview", "") or "")[:180],
                    "verbatim":   (it.get("verbatim_evidence", "") or "")[:180],
                    "post_time":  it.get("post_time", ""),
                } for it in breaking[:5]]
        except Exception:
            pass
        # AI CLI 一句话总结（对最近 3 条推文）—— 缓存到 sha1(post_ids)，非阻塞
        trump["summary"] = _trump_summary_recent3(items)
        banners["trump"] = trump

    # Gold Macro: "综合方向: bullish   score=+2  conf=8/10"
    m = re.search(r"综合方向:\s*(\w+)\s+score=([+-]?\d+)\s+conf=(\d+)/(\d+)", content)
    if m:
        gold = {
            "direction": m.group(1),
            "score":     int(m.group(2)),
            "conf":      f"{m.group(3)}/{m.group(4)}",
            "factors":   [],
        }
        # 抓 6 因子明细：▼/▲/· 前缀 + 名称（可含空格）+ dir + weight
        # 例："▼ 实际利率           dir=bearish  w=3  value=2.43 trend=rising"
        # 例："· WTI 原油         dir=neutral  w=0  value=84.38 ..."
        # name 用非贪婪 .+? 直到遇到 dir= 之前
        factor_re = re.compile(r"^\s*([▼▲·])\s+(.+?)\s+dir=(\w+)\s+w=(\d+)(.*)$", re.MULTILINE)
        for fm in factor_re.finditer(content):
            arrow, name, direction, weight, rest = fm.group(1), fm.group(2), fm.group(3), fm.group(4), fm.group(5)
            # extract value / pct_chg / trend if present
            v_m = re.search(r"value=(\S+)", rest)
            trend_m = re.search(r"trend=(\S+)", rest)
            gold["factors"].append({
                "arrow":     arrow,
                "name":      name,
                "direction": direction,
                "weight":    int(weight),
                "value":     v_m.group(1) if v_m else None,
                "trend":     trend_m.group(1) if trend_m else None,
            })
            if len(gold["factors"]) >= 6:
                break
        banners["gold_macro"] = gold

    # 期权风险等级: low/medium/high  距下次三巫日 X天
    m = re.search(r"期权风险等级:\s*(\w+)\s*\|\s*距下次三巫日\s*(\d+)天", content)
    if m:
        banners["options_risk"] = {
            "level":              m.group(1),
            "days_to_witching":   int(m.group(2)),
        }

    return {"exists": True, **banners}


def api_oil() -> dict:
    """石油宏观 — 带 5min 后台缓存。首次同步，之后刷新页面 <10ms 返回旧值 + 后台异步刷新。"""
    return _cached("oil", ttl_sec=300, compute_fn=_compute_oil)


def _compute_oil() -> dict:
    """石油宏观 — 多因子综合。

    因子构成（借鉴机构分析框架）：
      · **WTI** (CL=F)           — 美国基准价，绝对水平
      · **Brent** (BZ=F)         — 全球基准价
      · **Brent-WTI 价差**       — 正常 $2-6；扩大 = 美国供应过剩
      · **XLE** 能源板块 ETF     — 板块 vs 大盘表现，验证油价传导
      · **USD 指数** (DX-Y.NYB)  — 美元强弱（油以美元计价，负相关）
      · **美国商业原油库存** (FRED WCESTUS1) — 库存增 = 供应过剩 = 利空
      · **地缘 tag** (Trump 24h) — 最近推文提到 oil/OPEC/Iran/Hormuz 时高亮
      · **能源公司股** — XOM (Exxon) / CVX (Chevron) 参考市场对油价的定价

    评分逻辑（-3 ~ +3 综合分）：
      · +2 if 20d ≥ +8%
      · +1 if 5d ≥ +5% 或 XLE 20d > SPY 20d + 5%
      · +1 if Brent-WTI spread 扩大 > $7（供应紧张）
      · +1 if 库存 20d 下降 > 5%
      · -1 if USD 5d > +2% (强美元压油)
      · -1 if 库存增加
    """
    import re
    import urllib.request
    import yfinance as yf
    from config import _cfg

    out = {"ts": datetime.now(timezone.utc).isoformat(), "factors": []}
    prices = {}

    # 拉 yfinance 各标的
    for tk, label in [("CL=F", "wti"), ("BZ=F", "brent"), ("XLE", "xle"),
                       ("USO", "uso"), ("XOM", "xom"), ("CVX", "cvx"),
                       ("DX-Y.NYB", "dxy"), ("SPY", "spy"), ("^OVX", "ovx")]:
        try:
            df = yf.Ticker(tk).history(period="30d", interval="1d", auto_adjust=True)
            if df.empty or len(df) < 6:
                continue
            close = df["Close"].astype(float)
            price = float(close.iloc[-1])
            pct_5d  = ((price / float(close.iloc[-6])  - 1) * 100) if len(close) >= 6 else None
            pct_20d = ((price / float(close.iloc[-21]) - 1) * 100) if len(close) >= 21 else None
            prices[label] = {"price": round(price, 2), "pct_5d": round(pct_5d, 2) if pct_5d else None,
                              "pct_20d": round(pct_20d, 2) if pct_20d else None}
        except Exception:
            continue

    score = 0

    # 因子 1: WTI 绝对涨跌
    if "wti" in prices and prices["wti"]["pct_20d"] is not None:
        w = prices["wti"]
        d = "bullish" if w["pct_20d"] >= 8 else ("bearish" if w["pct_20d"] <= -8 else
             ("bullish" if w["pct_5d"] and w["pct_5d"] >= 5 else
              ("bearish" if w["pct_5d"] and w["pct_5d"] <= -5 else "neutral")))
        wt = 2 if abs(w["pct_20d"]) >= 8 else 1
        arr = "▲" if d == "bullish" else ("▼" if d == "bearish" else "·")
        out["factors"].append({
            "arrow": arr, "name": "WTI 油价", "direction": d, "weight": wt,
            "value": f"${w['price']} (20d {w['pct_20d']:+.1f}%)",
            "hint": "美国基准油价 20 天大幅涨跌指示供需变化",
        })
        if d == "bullish": score += wt
        elif d == "bearish": score -= wt

    # 因子 2: Brent-WTI 价差
    if "brent" in prices and "wti" in prices:
        spread = prices["brent"]["price"] - prices["wti"]["price"]
        # 正常 $2-6，> $7 供应紧张，< $0 罕见
        d = "bullish" if spread > 7 else ("bearish" if spread < 1 else "neutral")
        arr = "▲" if d == "bullish" else ("▼" if d == "bearish" else "·")
        out["factors"].append({
            "arrow": arr, "name": "Brent-WTI 价差", "direction": d, "weight": 1,
            "value": f"${spread:.2f} (Brent ${prices['brent']['price']} - WTI ${prices['wti']['price']})",
            "hint": "正常 $2-6；>$7 = 供应紧张利多，接近 0 = 美国供应过剩利空",
        })
        if d == "bullish": score += 1
        elif d == "bearish": score -= 1

    # 因子 3: XLE 能源板块 vs SPY
    if "xle" in prices and "spy" in prices:
        xle_p20 = prices["xle"]["pct_20d"]
        spy_p20 = prices["spy"]["pct_20d"]
        if xle_p20 is not None and spy_p20 is not None:
            diff = xle_p20 - spy_p20
            d = "bullish" if diff >= 3 else ("bearish" if diff <= -3 else "neutral")
            arr = "▲" if d == "bullish" else ("▼" if d == "bearish" else "·")
            out["factors"].append({
                "arrow": arr, "name": "XLE 能源板块", "direction": d, "weight": 1,
                "value": f"XLE {xle_p20:+.1f}% vs SPY {spy_p20:+.1f}% (差 {diff:+.1f}pp)",
                "hint": "能源股跑赢大盘 = 市场认可油价上行；跑输 = 不信任",
            })
            if d == "bullish": score += 1
            elif d == "bearish": score -= 1

    # 因子 4: 美元指数
    if "dxy" in prices and prices["dxy"]["pct_5d"] is not None:
        dxy = prices["dxy"]
        d = "bearish" if dxy["pct_5d"] >= 2 else ("bullish" if dxy["pct_5d"] <= -2 else "neutral")
        arr = "▼" if d == "bearish" else ("▲" if d == "bullish" else "·")
        out["factors"].append({
            "arrow": arr, "name": "美元指数 DXY", "direction": d, "weight": 1,
            "value": f"{dxy['price']} (5d {dxy['pct_5d']:+.1f}%)",
            "hint": "油以美元计价，美元升值 = 油价承压。DXY 5d +2% = 利空油",
        })
        if d == "bullish": score += 1
        elif d == "bearish": score -= 1

    # 因子 5: 美国零售汽油价 (FRED GASREGW) — 需求侧代理
    try:
        key = _cfg("FRED_API_KEY", "")
        if key:
            url = (f"https://api.stlouisfed.org/fred/series/observations?"
                    f"series_id=GASREGW&api_key={key}&file_type=json"
                    f"&limit=12&sort_order=desc")
            with urllib.request.urlopen(url, timeout=8) as r:
                fred = json.loads(r.read().decode())
            obs = [o for o in fred.get("observations", []) if o.get("value") not in (".", None)]
            if len(obs) >= 5:
                latest = float(obs[0]["value"])
                # 4 周前 (weekly data)
                base = float(obs[4]["value"])
                delta_pct = (latest - base) / base * 100
                # 汽油价与油价强相关，上涨=需求旺盛/成本转嫁，下跌=需求疲软
                d = "bullish" if delta_pct >= 3 else ("bearish" if delta_pct <= -3 else "neutral")
                arr = "▲" if d == "bullish" else ("▼" if d == "bearish" else "·")
                out["factors"].append({
                    "arrow": arr, "name": "美国汽油零售价", "direction": d, "weight": 1,
                    "value": f"${latest:.2f}/gal (4周 {delta_pct:+.1f}%)",
                    "hint": "零售汽油价 = 需求端信号。上涨 3%+ = 需求强劲支撑油价 / 下跌 = 需求疲软利空",
                })
                if d == "bullish": score += 1
                elif d == "bearish": score -= 1
    except Exception:
        pass

    # 因子 6: OVX 油价波动率指数（类似 VIX 之于 SPY）
    if "ovx" in prices and prices["ovx"]["price"]:
        ovx = prices["ovx"]
        # OVX < 30 平稳，30-45 关注，> 45 高波动/恐慌
        if ovx["price"] > 45:
            d = "bearish"; note = "极高（>45），市场恐慌"
        elif ovx["price"] > 35:
            d = "neutral"; note = "偏高（35-45），关注"
        elif ovx["price"] < 25:
            d = "bullish"; note = "低（<25），市场平稳"
        else:
            d = "neutral"; note = "正常（25-35）"
        arr = "▲" if d == "bullish" else ("▼" if d == "bearish" else "·")
        out["factors"].append({
            "arrow": arr, "name": "OVX 油价波动率", "direction": d, "weight": 1,
            "value": f"{ovx['price']} · {note}",
            "hint": "OVX = CBOE 原油 ETF 波动率指数（类似 VIX）。低=平稳利多，高=恐慌利空",
        })
        if d == "bullish": score += 1
        elif d == "bearish": score -= 1

    # 因子 7: 石油类地缘政治提示（借 Trump signal，如果最近有 oil/opec/iran/hormuz mention）
    try:
        snapshots = sorted(SIGNALS_DIR.glob("snapshot_*.txt"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if snapshots:
            content = snapshots[0].read_text(encoding="utf-8", errors="replace")
            oil_kw = re.findall(r"(oil|opec|iran|hormuz|crude|petroleum|refinery|opec\+|saudi|russia oil)",
                                 content.lower())
            n = len(oil_kw)
            if n > 0:
                out["factors"].append({
                    "arrow": "·", "name": "地缘 tag", "direction": "info", "weight": 0,
                    "value": f"snapshot 里 oil/OPEC/Iran/Hormuz 关键词 x{n}",
                    "hint": "有相关地缘讨论 → 查看 Trump signal / 新闻。此项不计入 score",
                })
    except Exception:
        pass

    # 综合方向
    if score >= 2:    out["direction"] = "bullish"
    elif score <= -2: out["direction"] = "bearish"
    else:              out["direction"] = "neutral"
    out["score"] = score

    # 简要 notes
    notes = []
    if "wti" in prices:
        p = prices["wti"]
        if p["pct_20d"] and p["pct_20d"] >= 8:
            notes.append("油价 20d 大涨 → 通胀压力上升，利好油气板块，利空高估值成长股")
        elif p["pct_20d"] and p["pct_20d"] <= -8:
            notes.append("油价 20d 大跌 → 通胀降温，利多科技 / 黄金")
        if p["pct_5d"] and p["pct_5d"] <= -5:
            notes.append("油价 5d 急跌 → 需求疲软信号 / 或供应过剩")
    if not notes:
        notes.append(f"综合 score={score:+d}，多因子分歧，观察后续")
    out["notes"] = notes
    out["price"]  = prices.get("wti", {}).get("price")
    out["pct_5d"] = prices.get("wti", {}).get("pct_5d")
    out["pct_20d"] = prices.get("wti", {}).get("pct_20d")
    out["source"] = "WTI 期货 (CL=F) 主 + 5 因子"
    return out


def api_sectors() -> dict:
    """板块级 regime — 带 5min 后台缓存。"""
    # v2 避免旧版仅含方向标签的磁盘缓存掩盖新增 short_style 字段。
    return _cached("sectors_v2_short_style", ttl_sec=300, compute_fn=_compute_sectors)


def _compute_sectors() -> dict:
    """板块级 regime 概览 —— 用 yfinance 拉基准 ETF 最近走势，给 dashboard 板块卡。

    每个板块返回：方向 regime + 独立短线 style。短线 style 优先展示，
    避免把中期仍向上误读为当前适合追涨。
    regime 判定（简版，不涉及 HMM）：
      · 5d ≤ -5% → 危机
      · 5d ≤ -2% → 弱势
      · 5d 在 ±2% 内 → 中性
      · 5d ≥ +2% → 强势
      · 5d ≥ +5% → 过热
    """
    import yfinance as yf
    SECTORS = [
        {"key": "SPY",  "zh": "大盘 S&P500",   "linked": "参考"},
        {"key": "QQQ",  "zh": "纳斯达克 100",   "linked": "TQQQ 3x"},
        {"key": "SMH",  "zh": "半导体 SMH",    "linked": "SOXL 3x / DRAM / MULL"},
        {"key": "GLD",  "zh": "黄金 GLD",     "linked": "GLD 1x"},
    ]
    out = []
    for s in SECTORS:
        try:
            df = yf.Ticker(s["key"]).history(period="3mo", interval="1d", auto_adjust=True)
            if df.empty:
                continue
            close = df["Close"].astype(float)
            price = float(close.iloc[-1])
            pct_5d  = ((price / float(close.iloc[-6])  - 1) * 100) if len(close) >= 6  else None
            pct_20d = ((price / float(close.iloc[-21]) - 1) * 100) if len(close) >= 21 else None
            ma20    = float(close.tail(20).mean()) if len(close) >= 20 else None
            trend   = "up" if ma20 and price > ma20 else "down"
            dist_ma = ((price - ma20) / ma20 * 100) if ma20 else None
            # regime 综合判定：短期（5d）+ 中期（20d）+ 均线相对位置
            # 优先级：20d 深亏 → 技术熊 / 5d 深亏 → 危机 / 双正 → 强势 / 其它 → 中性
            regime = "neutral"
            regime_zh = "中性"
            if pct_20d is not None and pct_20d <= -10:
                regime, regime_zh = "bear", "技术熊"
            elif pct_5d is not None and pct_5d <= -5:
                regime, regime_zh = "crisis", "危机（单日大跌）"
            elif pct_20d is not None and pct_20d <= -5:
                regime, regime_zh = "weak", "弱势"
            elif pct_5d is not None and pct_5d <= -2:
                regime, regime_zh = "pullback", "回调"
            elif pct_5d is not None and pct_20d is not None and pct_5d >= 5 and pct_20d >= 10:
                regime, regime_zh = "overheated", "过热"
            elif pct_5d is not None and pct_20d is not None and pct_5d >= 2 and pct_20d >= 2:
                regime, regime_zh = "strong", "强势"
            from market_style import analyze_price_style
            style = analyze_price_style(close, df["High"], df["Low"])
            out.append({
                "key":       s["key"],
                "zh":        s["zh"],
                "linked":    s["linked"],
                "price":     round(price, 2),
                "pct_5d":    round(pct_5d, 2) if pct_5d is not None else None,
                "pct_20d":   round(pct_20d, 2) if pct_20d is not None else None,
                "dist_ma20": round(dist_ma, 2) if dist_ma is not None else None,
                "trend":     trend,
                "regime":    regime,
                "regime_zh": regime_zh,
                "style":     style.get("style"),
                "style_zh":  style.get("style_zh"),
                "chop_score": style.get("chop_score"),
                "is_choppy": style.get("is_choppy"),
                "style_reason": style.get("reason"),
                "style_metrics": style.get("metrics") or {},
                "policy_note": style.get("policy_note"),
            })
        except Exception as e:
            out.append({"key": s["key"], "zh": s["zh"], "linked": s["linked"], "error": str(e)})
    return {"sectors": out, "ts": datetime.now(timezone.utc).isoformat()}


def api_ai_analysis() -> dict:
    """读最新 signals/ai_analysis_*.md 供 dashboard 展示 AI 分析。
    覆盖：
      · ai_analysis_snapshot_<ts>.md（snap.bat / orchestrator 时间戳快照）
      · ai_analysis_morning_<date>.md / ai_analysis_review_<date>.md（每日覆盖版本）
    """
    files = sorted(SIGNALS_DIR.glob("ai_analysis_*.md"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"exists": False}
    latest = files[0]
    try:
        content = latest.read_text(encoding="utf-8")
        return {
            "exists":  True,
            "path":    latest.name,
            "mtime":   datetime.fromtimestamp(latest.stat().st_mtime).isoformat(),
            "content": content,
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def api_supply_chain(ticker: str) -> dict:
    """标的上下游供应链关联。AI CLI 生成（缓存 30 天），可选 FMP 交叉验证 peers。"""
    ticker = (ticker or "").upper().strip()
    if not ticker or len(ticker) > 8 or not ticker.replace(".", "").replace("-", "").isalnum():
        return {"error": "invalid ticker"}
    # 30 天 TTL —— 供应链结构变化极慢（并购/剥离/新产线才变），避免重复消耗 AI CLI 额度
    return _cached(f"supply_chain_{ticker}", ttl_sec=30 * 24 * 3600,
                    compute_fn=lambda: _compute_supply_chain(ticker))


def _compute_supply_chain(ticker: str) -> dict:
    """调统一 AI CLI 生成结构化供应链 JSON，可选 FMP 交叉验证 peers。"""
    try:
        from ai_prompt import query_ai_cli
    except Exception as e:
        return {"error": f"ai_prompt import: {e}", "ticker": ticker}

    # JP 小盘冷门 ticker（如 TEKSCEND 429A / ARE 5857）模型单看短名认不出，
    # 从 JP_WATCH_LIST 补 symbol/name_zh/name_en/thesis 作为上下文
    ticker_context = f"Ticker: {ticker}"
    for entry in JP_WATCH_LIST:
        if entry.get("ticker") == ticker:
            ticker_context = (
                f"Ticker: {ticker}\n"
                f"JP Symbol: {entry.get('symbol')}\n"
                f"日语公司名/中文名: {entry.get('name_zh')}\n"
                f"英文名: {entry.get('name_en')}\n"
                f"业务定位: {entry.get('thesis','')}"
            )
            break

    prompt = (
        "你是股票供应链分析师。为下面 ticker 输出严格 JSON（无 markdown 围栏、无 preamble、无 comment）：\n\n"
        f"{ticker_context}\n\n"
        "输出格式（严格 UTF-8 JSON, keys 用英文, values 中文说明）：\n"
        "{\n"
        '  "sector":     "行业中文名（如 半导体设计/云计算/新能源车）",\n'
        '  "business":   "1 句核心业务说明",\n'
        '  "upstream":   [{"name":"供应商公司", "ticker":"股票代码或 null", "note":"提供什么", "weight":0-100 相对重要度, "confidence":"high|medium|low"}],\n'
        '  "downstream": [{"name":"客户公司",   "ticker":"股票代码或 null", "note":"买什么用途", "weight":0-100, "confidence":"high|medium|low"}],\n'
        '  "peers":      [{"name":"同行竞争公司","ticker":"股票代码或 null", "note":"竞争方向", "confidence":"high|medium|low"}]\n'
        "}\n\n"
        "要求:\n"
        "1. upstream/downstream 各 3-6 项，按 weight 从高到低\n"
        "2. peers 3-5 项\n"
        "3. ticker 字段：仅当在美股/港股/日股/A股主板上市时给出（如 TSM/2330.TW/6752.T），否则填 null\n"
        "4. weight 是你对该关系相对重要度的估计（营收占比或战略重要度），不是精确数字\n"
        "5. confidence: high=10-K 明确披露 / medium=行业公开信息 / low=推测\n"
        "6. 若是 ETF 或杠杆产品，视为其追踪的标的（TQQQ→QQQ 前十大 / SOXL→半导体链）\n"
        "7. 严格只输出 JSON 对象，不要 ``` 围栏\n"
    )
    out, status, provider, _ = query_ai_cli(prompt, timeout=90)
    if not out:
        return {"error": f"{provider.lower()} {status}", "ticker": ticker}
    # 去除潜在的 markdown 围栏
    import re
    txt = out.strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```\s*$", "", txt)
    try:
        data = json.loads(txt)
    except Exception as e:
        return {"error": f"json parse: {e}", "raw": out[:500], "ticker": ticker}

    result = {
        "ticker":     ticker,
        "sector":     data.get("sector", ""),
        "business":   data.get("business", ""),
        "upstream":   data.get("upstream", []) or [],
        "downstream": data.get("downstream", []) or [],
        "peers":      data.get("peers", []) or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source":     provider.lower(),
    }

    # 可选：FMP 交叉验证 peers（免费 250 calls/day）
    try:
        fmp_key = _cfg_helper("FMP_API_KEY", "")
        if fmp_key:
            import urllib.request
            url = f"https://financialmodelingprep.com/api/v3/stock/peers?symbol={ticker}&apikey={fmp_key}"
            with urllib.request.urlopen(url, timeout=6) as r:
                fmp_data = json.loads(r.read().decode())
            fmp_peers = set()
            if isinstance(fmp_data, list) and fmp_data:
                fmp_peers = set(p.upper() for p in (fmp_data[0].get("peersList") or []))
            result["fmp_peers"] = sorted(fmp_peers)
            # 标记 AI 输出的 peers 是否被 FMP 覆盖
            for p in result["peers"]:
                p_tk = (p.get("ticker") or "").upper()
                p["fmp_verified"] = bool(p_tk and p_tk in fmp_peers)
            result["source"] = f"{provider.lower()}+fmp"
    except Exception as e:
        result["fmp_error"] = str(e)[:100]

    return result


def api_supply_chain_graph(ticker: str, depth: int = 2) -> dict:
    """多层供应链图（BFS）。返回 nodes/edges 供 D3 蜘蛛网渲染。

    每条边带 `reason`（来自 AI 输出的 note），供 hover 显示。
    layer 记录节点到 root 的最短跳数。未 cache 的 ticker 触发后台 AI CLI 拉取，
    本次请求返 `pending: [...]`，前端可稍后再请求补图。
    """
    import threading
    root = (ticker or "").upper().strip()
    if not root:
        return {"error": "invalid ticker"}
    max_depth = max(1, min(int(depth or 2), 3))  # 限 1-3 层

    nodes: dict = {}
    edges: list = []
    seen_edges: set = set()
    visited: set = {root}

    def _add_node(nid, **kw):
        if nid in nodes:
            # 记住最小 layer
            if kw.get("layer", 99) < nodes[nid].get("layer", 99):
                nodes[nid]["layer"] = kw["layer"]
            return
        nodes[nid] = {"id": nid, **kw}

    _add_node(root, name=root, ticker=root, layer=0, group="center", confidence="high")

    queue = [(root, 0)]
    pending: list = []
    while queue:
        cur, layer = queue.pop(0)
        if layer >= max_depth:
            continue
        cache_path = _WEBUI_CACHE_DIR / f"supply_chain_{cur}.json"
        if not cache_path.exists():
            if cur != root and cur not in pending:
                pending.append(cur)
                # 后台触发 AI CLI 拉取（非阻塞）
                threading.Thread(
                    target=lambda t=cur: _compute_supply_chain(t), daemon=True
                ).start()
            continue
        try:
            sc = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "error" in sc:
            continue

        for kind, items in (
            ("supply",   sc.get("upstream",   []) or []),
            ("customer", sc.get("downstream", []) or []),
            ("peer",     sc.get("peers",      []) or []),
        ):
            for x in items:
                tk = (x.get("ticker") or "").upper().strip()
                if not tk:
                    continue  # 跳过没有 ticker 的（无法多跳）
                grp = {"supply": "up", "customer": "down", "peer": "peer"}[kind]
                _add_node(tk, name=x.get("name"), ticker=tk, layer=layer + 1,
                          group=grp, confidence=x.get("confidence"),
                          weight=x.get("weight"))
                # 边方向: supply = supplier → consumer, customer = seller → customer, peer 双向（用 target 做展示）
                src, tgt = (tk, cur) if kind == "supply" else (cur, tk)
                key = (src, tgt, kind)
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append({
                        "source":     src,
                        "target":     tgt,
                        "reason":     x.get("note", ""),
                        "kind":       kind,
                        "confidence": x.get("confidence"),
                    })
                if tk not in visited:
                    visited.add(tk)
                    queue.append((tk, layer + 1))

    return {
        "root":    root,
        "depth":   max_depth,
        "nodes":   list(nodes.values()),
        "edges":   edges,
        "pending": pending,   # 前端可延迟重试补图
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _cfg_helper(key: str, default: str = "") -> str:
    """Wrapper for config._cfg to avoid import-order issues."""
    try:
        from config import _cfg
        return _cfg(key, default)
    except Exception:
        return default


# JP 短名 → 6-digit .T symbol 映射（美股用 config.get_fundamental_stock 单一源）
JP_SHORT_TO_SYMBOL = {
    "TDK":      "6762.T",   # TDK — 电子元器件/固态电池/HDD磁头，AAPL/NVDA 供应链
    "KIOXIA":   "285A.T",   # キオクシア — NAND 闪存全球#2（前东芝存储），2024-12 IPO
    "FUJIKURA": "5803.T",   # 藤倉 — 光纤/海底电缆/AI 数据中心互联概念
    "MURATA":   "6981.T",   # 村田製作所 — MLCC 全球龙头，Apple/NVDA 直接供应
    "TEKSCEND": "429A.T",   # TEKSCEND PHOTOMASK — 光罩制造，TSMC 关键工艺供应
    "ARE":      "5857.T",   # ARE Holdings (旧 Asahi Holdings) — 贵金属回收（金/银/铂）
    "MUFG":     "8306.T",   # 三菱 UFJ 金融集团 — 日本最大银行，日本加息受益股
    "SUMCO":    "3436.T",   # SUMCO 胜高 — 硅晶圆全球 #2
    "SHINETSU": "4063.T",   # 信越化学 — 硅晶圆全球 #1
    "CYAGENT":  "4751.T",   # CyberAgent — 互联网广告/游戏
    "DOWA":     "5714.T",   # 同和控股 DOWA — 非鉄金属精炼 + 半导体材料
}

def _resolve_fundamental_stock(ticker: str) -> str | None:
    """单一源基本面 stock 解析。
    - JP 短名（TDK/KIOXIA/...）→ 对应 6-digit .T symbol
    - 美股 → config.get_fundamental_stock（None = 无基本面商品/债券 ETF）
    加新美股不用改这里，进 TRACKED_TICKERS 就默认用自身。"""
    tk = (ticker or "").upper()
    if tk in JP_SHORT_TO_SYMBOL:
        return JP_SHORT_TO_SYMBOL[tk]
    try:
        from config import get_fundamental_stock
        return get_fundamental_stock(tk)
    except Exception:
        return tk

# 向后兼容旧调用（TICKER_TO_FUNDAMENTAL_STOCK.get(tk, tk)）—— 提供 dict-like 接口
# 关键：resolve 已内置 "未知→自身" 的 fallback，所以永远返 resolve 结果，
# 忽略传入 default（否则 GLD/USO 这类明确返 None 的会被 default 掩盖）。
class _FundamentalStockMap:
    def get(self, tk, default=None):
        return _resolve_fundamental_stock(tk)
    def __getitem__(self, tk):
        return _resolve_fundamental_stock(tk)

TICKER_TO_FUNDAMENTAL_STOCK = _FundamentalStockMap()

# 日股关注列表（不参与 orchestrator scan，只在 dashboard 单独展示）
JP_WATCH_LIST = [
    {"ticker": "TDK",      "symbol": "6762.T", "name_zh": "TDK",      "name_en": "TDK",
     "thesis": "电子元器件龙头 · Apple/NVDA 供应链 · MLCC + 固态电池 + HDD 磁头 + 无线充电"},
    {"ticker": "KIOXIA",   "symbol": "285A.T", "name_zh": "铠侠",     "name_en": "Kioxia",
     "thesis": "NAND 闪存全球#2 · 前东芝存储 · 2024-12 IPO · Data-center SSD + Client SSD"},
    {"ticker": "FUJIKURA", "symbol": "5803.T", "name_zh": "藤仓",     "name_en": "Fujikura",
     "thesis": "光纤/海底电缆 · AI 数据中心光互联需求爆发 · NVDA GPU 集群互联受益股"},
    {"ticker": "MURATA",   "symbol": "6981.T", "name_zh": "村田",     "name_en": "Murata",
     "thesis": "MLCC 全球龙头（市占 40%+）· Apple/NVDA/Tesla 直接供应 · AI 服务器 + EV 高价值 MLCC 需求"},
    {"ticker": "TEKSCEND", "symbol": "429A.T", "name_zh": "TEKSCEND", "name_en": "Tekscend Photomask",
     "thesis": "半导体光罩制造 · TSMC/Samsung 关键工艺供应 · 光刻上游（ASML 之外的国产替代）"},
    {"ticker": "ARE",      "symbol": "5857.T", "name_zh": "ARE 控股", "name_en": "ARE Holdings",
     "thesis": "贵金属回收（金/银/铂）· 半导体电子废弃物 urban mining · AI 芯片需求带动金/铂回收溢价"},
    {"ticker": "MUFG",     "symbol": "8306.T", "name_zh": "三菱 UFJ", "name_en": "Mitsubishi UFJ Financial",
     "thesis": "日本最大银行（总资产约 400+ 兆日元）· 日元利率上升带来 NII 增益 · 全球业务分散",
     "fundamental_profile": "bank", "show_supply_chain": False,
     "thesis_fit": _jp_thesis_fit("MUFG")},
    {"ticker": "SUMCO",    "symbol": "3436.T", "name_zh": "胜高",     "name_en": "SUMCO",
     "thesis": "硅晶圆全球 #2（信越化学后）· 300mm wafer 供 TSMC/Samsung/Intel · AI 芯片上游原材料 · 12 吋产能瓶颈受益"},
    {"ticker": "SHINETSU", "symbol": "4063.T", "name_zh": "信越化学", "name_en": "Shin-Etsu Chemical",
     "thesis": "硅晶圆全球 #1 (超 SUMCO) + PVC 世界龙头 + 光罩用光刻胶 · 半导体材料 upstream 最上游 · AI 需求+日元弱势双利好"},
    {"ticker": "CYAGENT",  "symbol": "4751.T", "name_zh": "CyberAgent", "name_en": "CyberAgent Inc.",
     "thesis": "日本最大互联网广告代理 · Uma Musume 手游长期收入台柱 · Abema TV 流媒体 · 生成 AI 内容/AI ads platform 投入加大"},
    {"ticker": "DOWA",     "symbol": "5714.T", "name_zh": "同和控股", "name_en": "DOWA Holdings",
     "thesis": "非鉄金属(锌/铅/铜)精炼 + 半导体电子材料 + urban mining (半导体电子废弃物金属回收) · 与 ARE 同赛道但更多元 · AI 芯片+EV 电池金属需求受益"},
]


def api_jp_watch() -> dict:
    """日股关注列表当前行情（不进 orchestrator scan）。5 min 缓存。

    同时检查 JP catalyst 触发条件（RSI<35 + 業績予想上修），触发则推送 Discord/TG。
    dedup 24h 防止刷屏。
    """
    # Versioned key prevents pre-fix, unshifted Ichimoku values from surviving a
    # deployment.  Dynamic metadata is re-attached so a midnight JST boundary
    # cannot leave an event labelled "tomorrow" for the full cache TTL.
    result = _cached("jp_watch_v2", ttl_sec=300, compute_fn=_compute_jp_watch)
    entries = {entry["ticker"]: entry for entry in JP_WATCH_LIST}
    for row in (result or {}).get("tickers", []):
        entry = entries.get(row.get("ticker"), {})
        if entry:
            row.update({key: value for key, value in entry.items() if key not in {"price", "source"}})
        guidance = _official_jp_guidance(row.get("ticker", ""))
        if guidance:
            row["earnings_event"] = guidance["earnings_event"]
    # 检查 catalyst 触发（不阻断响应，静默 fire-and-forget）
    try:
        threading.Thread(target=_check_jp_catalyst_triggers, args=(result,), daemon=True).start()
    except Exception:
        pass
    return result


def _check_jp_catalyst_triggers(jp_watch_data: dict) -> None:
    """检查每只 JP 股是否 满足 catalyst 条件 → 触发 Discord/TG 推送。

    条件（AND）：RSI < 35 且官方来源已核验、revision_type = raised。
    dedup: 每 24h 每 ticker 只推一次
    """
    dedup_dir = _WEBUI_CACHE_DIR / "jp_catalyst_sent"
    dedup_dir.mkdir(exist_ok=True)
    for t in (jp_watch_data or {}).get("tickers", []):
        if "error" in t:
            continue
        rsi = t.get("rsi", 100)
        tk  = t.get("ticker", "")
        if rsi >= 35 or not tk:
            continue
        # Read the same deterministic official contract as the API.  Guidance
        # caches are not authoritative and must never become trade alerts.
        gd = _official_jp_guidance(tk)
        if not gd:
            continue
        # Fail closed when the source is historical but a newer earnings
        # release is awaiting verification.
        if not gd.get("source_verified") or gd.get("revision_type") != "raised":
            continue
        if not gd.get("current_verified"):
            continue
        # dedup: 24h per ticker
        dedup_file = dedup_dir / f"{tk}.txt"
        if dedup_file.exists():
            age = datetime.now().timestamp() - dedup_file.stat().st_mtime
            if age < 24 * 3600:
                continue
        # fire
        try:
            from notifications import notify_jp_guidance_opportunity
            notify_jp_guidance_opportunity(
                ticker=tk, name=t.get("name_zh", tk),
                rsi=rsi, pct_5d=t.get("pct_5d", 0) or 0,
                guidance=gd,
            )
            dedup_file.write_text(datetime.now().isoformat(), encoding="utf-8")
        except Exception:
            pass


def _compute_jp_watch() -> dict:
    """3 只日股：完整技术栈（RSI/MA/BB/MACD/ADX/CCI/量比 + 共振）+ 一目均衡表。

    双轨：优先 moomoo openD JP kline（如果订阅了 JP 行情），失败降级 yfinance。
    """
    import yfinance as yf
    import pandas as pd
    import numpy as np
    # openD JP kline 尝试
    try:
        from moomoo_data import get_kline_via_openD, health_check
        h_state = health_check()
        openD_jp_ok = h_state.get("available") and h_state.get("market_jp") not in (None, "NONE")
    except Exception:
        openD_jp_ok = False
        get_kline_via_openD = None

    out = {"ts": datetime.now(timezone.utc).isoformat(), "tickers": [],
           "data_source": "openD JP + yfinance fallback" if openD_jp_ok else "yfinance"}
    for entry in JP_WATCH_LIST:
        try:
            h = None
            source_used = "yfinance"
            if openD_jp_ok and get_kline_via_openD:
                # 拉 250 天让 span_b 52 天热身 + 90 天显示都有真值（原 180 前一半 null）
                h = get_kline_via_openD(entry["symbol"], days=250)
                if h is not None and not h.empty and len(h) >= 78:
                    source_used = "openD"
                else:
                    h = None
            if h is None:
                t = yf.Ticker(entry["symbol"])
                # 1y (~250 交易日): span_b 52 rolling → 198 天真值 > 90 天显示，全 fill
                h = t.history(period="1y", auto_adjust=True)
            if h.empty or len(h) < 78:
                out["tickers"].append({**entry, "error": f"insufficient bars ({len(h)})"})
                continue
            close = h["Close"].astype(float)
            high  = h["High"].astype(float)
            low   = h["Low"].astype(float)
            vol   = h["Volume"].astype(float)
            price = float(close.iloc[-1])
            prev  = float(close.iloc[-2])
            pct_1d = ((price / prev) - 1) * 100
            pct_5d = ((price / float(close.iloc[-6])) - 1) * 100 if len(close) >= 6 else None

            # ── RSI(14) ──
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, 1e-9)
            rsi_val = float((100 - 100 / (1 + rs)).iloc[-1])

            # ── MA5/20/50 ──
            ma5  = float(close.rolling(5).mean().iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None

            # ── Bollinger(20, 2σ) ──
            bb_m = close.rolling(20).mean()
            bb_s = close.rolling(20).std()
            bb_up = float((bb_m + 2 * bb_s).iloc[-1])
            bb_dn = float((bb_m - 2 * bb_s).iloc[-1])
            bb_pct_b = (price - bb_dn) / (bb_up - bb_dn) if bb_up > bb_dn else 0.5

            # ── MACD(12,26,9) ──
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_dif = float(macd_line.iloc[-1])
            macd_dea = float(signal_line.iloc[-1])

            # ── ADX(14) ──
            up_move   = high.diff()
            down_move = -low.diff()
            plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0)
            minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0)
            tr1 = high - low
            tr2 = (high - close.shift()).abs()
            tr3 = (low - close.shift()).abs()
            tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(14).mean().replace(0, 1e-9)
            plus_di  = 100 * plus_dm.rolling(14).mean()  / atr
            minus_di = 100 * minus_dm.rolling(14).mean() / atr
            dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
            adx_val = float(dx.rolling(14).mean().iloc[-1])

            # ── CCI(20) ──
            tp = (high + low + close) / 3
            sma_tp = tp.rolling(20).mean()
            mad = tp.rolling(20).apply(lambda x: (x - x.mean()).abs().mean(), raw=False)
            cci_val = float(((tp - sma_tp) / (0.015 * mad.replace(0, 1e-9))).iloc[-1])

            # ── 量比 ──
            vol_avg = float(vol.tail(20).mean())
            vol_ratio = float(vol.iloc[-1]) / vol_avg if vol_avg else 1

            # ── 一目均衡表 Ichimoku ──
            # 転換線 Tenkan(9), 基準線 Kijun(26), Senkou A/B(52), 遅行 = 当前价 推 26 天前
            ichimoku_series = _compute_ichimoku_series(high, low, close)
            tenkan_series   = ichimoku_series["tenkan"]
            kijun_series    = ichimoku_series["kijun"]
            # The cloud visible at today's x-coordinate was calculated 26
            # sessions earlier.  Comparing against unshifted spans caused the
            # MUFG card to say 云中 when the standard cloud said 云上.
            senkou_a_series = ichimoku_series["senkou_a"]
            senkou_b_series = ichimoku_series["senkou_b"]
            tenkan   = float(tenkan_series.iloc[-1])
            kijun    = float(kijun_series.iloc[-1])
            senkou_a = float(senkou_a_series.iloc[-1])
            senkou_b = float(senkou_b_series.iloc[-1])
            ichi_pos, ichi_dir = _classify_ichimoku_position(price, senkou_a, senkou_b)
            cloud_color = "bullish" if senkou_a > senkou_b else "bearish"
            tk_cross = "金叉" if tenkan > kijun else "死叉"

            # ── 共振（bull/bear count）──
            bull, bear = 0, 0
            reasons_bull, reasons_bear = [], []
            def _add(cond, label, side):
                nonlocal bull, bear
                if cond:
                    if side == 'b': bull += 1; reasons_bull.append(label)
                    else: bear += 1; reasons_bear.append(label)
            _add(price > ma20, "价格站上MA20", 'b')
            _add(price < ma20, "价格跌破MA20", 's')
            if ma50:
                _add(ma20 > ma50, "均线多排 MA20>MA50", 'b')
                _add(ma20 < ma50, "均线空排 MA20<MA50", 's')
            _add(rsi_val < 30, f"RSI 超卖 {rsi_val:.0f}", 'b')
            _add(rsi_val > 70, f"RSI 超买 {rsi_val:.0f}", 's')
            _add(bb_pct_b < 0.1, "跌破布林下轨", 'b')
            _add(bb_pct_b > 0.9, "冲破布林上轨", 's')
            _add(macd_dif > macd_dea and macd_dif > 0, "MACD 多头零轴上", 'b')
            _add(macd_dif < macd_dea and macd_dif < 0, "MACD 空头零轴下", 's')
            _add(cci_val < -100, "CCI 超卖", 'b')
            _add(cci_val > 100, "CCI 超买", 's')
            _add(ichi_dir == "bullish", "一目 云上", 'b')
            _add(ichi_dir == "bearish", "一目 云下", 's')
            _add(tk_cross == "金叉" and price > kijun, "转换>基准 (金叉)", 'b')
            _add(tk_cross == "死叉" and price < kijun, "转换<基准 (死叉)", 's')

            # 简单 action
            if bull - bear >= 3 and rsi_val < 40: action = "关注买入"
            elif bull - bear >= 3: action = "偏多"
            elif bear - bull >= 3 and rsi_val > 60: action = "警示"
            elif bear - bull >= 3: action = "偏空"
            else: action = "观望"

            trend = "up" if price > ma20 else "down"

            # ── 时间序列（供 SVG 画 Ichimoku 云图，最后 90 天）──
            N = min(90, len(close))
            series_close   = close.tail(N).tolist()
            series_tenkan  = tenkan_series.tail(N).tolist()
            series_kijun   = kijun_series.tail(N).tolist()
            series_span_a  = senkou_a_series.tail(N).tolist()
            series_span_b  = senkou_b_series.tail(N).tolist()

            out["tickers"].append({
                **entry,
                "price":     round(price, 2),
                "pct_1d":    round(pct_1d, 2),
                "pct_5d":    round(pct_5d, 2) if pct_5d is not None else None,
                "rsi":       round(rsi_val, 1),
                "ma20":      round(ma20, 2),
                "ma50":      round(ma50, 2) if ma50 else None,
                "bb_pct_b":  round(bb_pct_b * 100, 1),
                "macd_dif":  round(macd_dif, 2),
                "macd_dea":  round(macd_dea, 2),
                "adx":       round(adx_val, 1),
                "cci":       round(cci_val, 1),
                "vol_ratio": round(vol_ratio, 2),
                "trend":     trend,
                "bull_count": bull, "bear_count": bear,
                "reasons_bull": reasons_bull, "reasons_bear": reasons_bear,
                "action_zh": action,
                "ichimoku": {
                    "tenkan":   round(tenkan, 2),
                    "kijun":    round(kijun, 2),
                    "senkou_a": round(senkou_a, 2),
                    "senkou_b": round(senkou_b, 2),
                    "position": ichi_pos,       # 云上/云中/云下
                    "direction": ichi_dir,
                    "cloud_color": cloud_color, # 云的颜色（红=空 / 绿=多）
                    "tk_cross": tk_cross,
                },
                "series": {
                    "close":  [round(float(v), 2) for v in series_close],
                    "tenkan": [round(float(v), 2) if v == v else None for v in series_tenkan],
                    "kijun":  [round(float(v), 2) if v == v else None for v in series_kijun],
                    "span_a": [round(float(v), 2) if v == v else None for v in series_span_a],
                    "span_b": [round(float(v), 2) if v == v else None for v in series_span_b],
                },
                "currency": "JPY",
                "source":   source_used,
            })
        except Exception as e:
            out["tickers"].append({**entry, "error": str(e)[:100]})
    return out


def api_fundamentals(ticker: str, period: str = 'year') -> dict:
    """4 个基本面指标：CROIC / Piotroski F / 金融借款 / 现金周转循环。

    period='year'    → 4 年年度（长线视角，稳）， TTL 30 天
    period='quarter' → 8 季季度（波段视角，抓拐点），TTL 15 天
    ETF 映射到代表单股（TQQQ/SOXL→NVDA, DRAM/MULL→MU）。
    """
    tk = (ticker or "").upper().strip()
    period = 'quarter' if period == 'quarter' else 'year'
    bank_payload = _official_bank_fundamentals(tk)
    if bank_payload is not None:
        return bank_payload
    if not ticker_display_profile(tk)["show_fundamentals"]:
        return {"ticker": tk, "error": "no_fundamentals_for_this_type"}
    stock = TICKER_TO_FUNDAMENTAL_STOCK.get(tk, tk)
    if stock is None:
        return {"ticker": tk, "error": "no_fundamentals_for_this_type"}
    ttl = 15 * 24 * 3600 if period == 'quarter' else 30 * 24 * 3600
    return _cached(f"fundamentals_{tk}_{period}", ttl_sec=ttl,
                    compute_fn=lambda: _compute_fundamentals(tk, stock, period))


def _compute_fundamentals(tk: str, stock: str, period: str = 'year') -> dict:
    """从 yfinance 拉财报算 4 指标。period=year|quarter"""
    import yfinance as yf
    try:
        t = yf.Ticker(stock)
        if period == 'quarter':
            inc = t.quarterly_income_stmt
            bal = t.quarterly_balance_sheet
            cf  = t.quarterly_cashflow
            piotroski_lag = 4   # YoY 同季对比（避免季节性），如 Q3'25 vs Q3'24
            annualize     = 4   # 季度 CROIC × 4 = 年化，可比
            days_factor   = 91.25   # 一季度 ≈ 91 天
        else:
            inc = t.income_stmt
            bal = t.balance_sheet
            cf  = t.cashflow
            piotroski_lag = 1
            annualize     = 1
            days_factor   = 365
        # 主基准：income statement 必须有；balance_sheet 也基本必要；cashflow 若空则相关指标降级
        if inc.empty or bal.empty:
            return {"ticker": tk, "source": stock, "period": period,
                    "error": f"no {period} income/balance statements from yfinance"}
        # 记录 cashflow 是否可用（JP 季度常常缺失）
        cf_available = not cf.empty
        # 取 inc/bal 交集的期数（JP 常出现 inc=4 bal=3 → 用 min=3 避免越界）
        n_periods = min(len(inc.columns), len(bal.columns))
        if n_periods == 0:
            return {"ticker": tk, "source": stock, "period": period,
                    "error": "empty income or balance sheet"}
        # 若 inc 比 n_periods 多，只取最近 n_periods 期（columns 最左=最近）
        inc = inc.iloc[:, :n_periods]
        bal = bal.iloc[:, :n_periods]
        if cf_available:
            cf = cf.iloc[:, :min(len(cf.columns), n_periods)]

        # yfinance 返 columns=日期（最近在左），rows=科目。倒序让最老在左
        def _get(df, keys, default=0):
            """按候选 key 列表查行，取所有列返 list（老 → 新）。
            始终对齐 n_periods 长度（不够的 pad default，超出的截断）。
            解决 JP 股 cf/bal/inc 列数不一致（如 ARE inc=5 bal=5 cf=4）导致的 list index 越界。
            """
            if df.empty:
                return [default] * n_periods
            for k in keys:
                if k in df.index:
                    vals = list(reversed(df.loc[k].tolist()))
                    vals = [float(v) if v == v else 0 for v in vals]
                    # 对齐 n_periods：不足前补 default，超出截取后 n_periods 个
                    if len(vals) < n_periods:
                        vals = [default] * (n_periods - len(vals)) + vals
                    elif len(vals) > n_periods:
                        vals = vals[-n_periods:]
                    return vals
            return [default] * n_periods

        # Period 标签: year → "2025" / quarter → "Q3'25"
        def _fmt(col):
            if period == 'quarter':
                q = (col.month - 1) // 3 + 1
                return f"Q{q}'{str(col.year)[2:]}"
            return str(col.year)
        years = [_fmt(c) for c in reversed(inc.columns)]

        revenue      = _get(inc, ["Total Revenue", "TotalRevenue"])
        cogs         = _get(inc, ["Cost Of Revenue", "CostOfRevenue"])
        net_income   = _get(inc, ["Net Income", "NetIncome"])
        gross        = _get(inc, ["Gross Profit", "GrossProfit"])

        total_assets      = _get(bal, ["Total Assets", "TotalAssets"])
        total_equity      = _get(bal, ["Total Equity Gross Minority Interest", "StockholdersEquity",
                                       "Total Stockholder Equity"])
        current_assets    = _get(bal, ["Current Assets", "CurrentAssets"])
        current_liab      = _get(bal, ["Current Liabilities", "CurrentLiabilities"])
        st_debt           = _get(bal, ["Current Debt", "Short Long Term Debt", "ShortLongTermDebt",
                                       "CurrentDebt"])
        lt_debt           = _get(bal, ["Long Term Debt", "LongTermDebt"])
        cash              = _get(bal, ["Cash And Cash Equivalents", "CashAndCashEquivalents"])
        inventory         = _get(bal, ["Inventory"])
        ar                = _get(bal, ["Accounts Receivable", "AccountsReceivable"])
        ap                = _get(bal, ["Accounts Payable", "AccountsPayable"])
        shares_out        = _get(bal, ["Ordinary Shares Number", "Share Issued", "ShareIssued"])

        ocf  = _get(cf, ["Operating Cash Flow", "OperatingCashFlow", "Total Cash From Operating Activities"])
        capex = _get(cf, ["Capital Expenditure", "CapitalExpenditure", "CapitalExpenditures"])

        n = len(years)

        def _safe_div(a, b, default=None):
            return (a / b) if b else default

        # 1. CROIC = FCF / IC（%）—— 季度用 × 4 年化，可与年度比较
        # 若 cashflow 缺失（yfinance JP 季度常见）→ 全部 None
        croic = []
        for i in range(n):
            if not cf_available:
                croic.append(None)
                continue
            fcf = ocf[i] - abs(capex[i])
            ic  = (st_debt[i] + lt_debt[i]) + total_equity[i] - cash[i]
            v = _safe_div(fcf, ic)
            croic.append(round(v * annualize * 100, 2) if v is not None else None)

        # 2. 金融借款 = ST + LT debt（$M）—— point-in-time, 无 period 调整
        fin_debt = [round((st_debt[i] + lt_debt[i]) / 1e6, 1) for i in range(n)]

        # 3. 现金周转循环 = DIO + DSO - DPO（天）—— 用对应期间天数
        ccc = []
        for i in range(n):
            dio = _safe_div(inventory[i], cogs[i], 0)
            dso = _safe_div(ar[i], revenue[i], 0)
            dpo = _safe_div(ap[i], cogs[i], 0)
            v = (dio + dso - dpo) * days_factor if all(x is not None for x in (dio, dso, dpo)) else None
            ccc.append(round(v, 1) if v is not None else None)

        # 4. Piotroski F-Score（0-9）
        # year 模式: 比较前一年 (lag=1)
        # quarter 模式: YoY 同季对比 (lag=4)，避免季节性
        # Piotroski: 若 cashflow 缺失，2 项测试无法算，最多得 7 分（标注 max=7）
        piotroski_max = 9 if cf_available else 7
        piotroski = [None] * min(piotroski_lag, n)
        for i in range(piotroski_lag, n):
            score = 0
            j = i - piotroski_lag
            # a) profitability
            score += 1 if net_income[i] > 0 else 0
            if cf_available:
                score += 1 if ocf[i] > 0 else 0
                score += 1 if ocf[i] > net_income[i] else 0  # quality of earnings
            roa_now  = _safe_div(net_income[i], total_assets[i], 0)
            roa_prev = _safe_div(net_income[j], total_assets[j], 0)
            score += 1 if roa_now > roa_prev else 0
            # b) leverage / liquidity
            score += 1 if lt_debt[i] < lt_debt[j] else 0
            cr_now  = _safe_div(current_assets[i], current_liab[i], 0)
            cr_prev = _safe_div(current_assets[j], current_liab[j], 0)
            score += 1 if cr_now > cr_prev else 0
            score += 1 if shares_out[i] <= shares_out[j] else 0
            # c) operating efficiency
            gm_now  = _safe_div(gross[i], revenue[i], 0)
            gm_prev = _safe_div(gross[j], revenue[j], 0)
            score += 1 if gm_now > gm_prev else 0
            turn_now  = _safe_div(revenue[i], total_assets[i], 0)
            turn_prev = _safe_div(revenue[j], total_assets[j], 0)
            score += 1 if turn_now > turn_prev else 0
            piotroski.append(score)

        # === Phase 3a: 5 项估值指标（PBR/PER/ROE/股息率/权益比率）
        # yfinance 返 dividendYield 已是 %（MSFT=0.93 表示 0.93%），不再 ×100
        # yfinance 返 returnOnEquity 是小数（0.30 表示 30%），需 ×100
        pbr = per = roe_pct = div_yield = None
        try:
            info = t.info or {}
            pbr       = round(info["priceToBook"], 2)      if info.get("priceToBook") else None
            per       = round(info["trailingPE"], 2)       if info.get("trailingPE")  else None
            roe_val   = info.get("returnOnEquity")
            roe_pct   = round(roe_val * 100, 2) if roe_val is not None else None
            div_val   = info.get("dividendYield")
            div_yield = round(div_val, 2) if div_val is not None else None
        except Exception:
            pass
        # 权益比率 series: Total Equity / Total Assets（每年一个数）
        equity_ratio = []
        for i in range(n):
            v = _safe_div(total_equity[i], total_assets[i], None)
            equity_ratio.append(round(v * 100, 1) if v is not None else None)

        return {
            "ticker":       tk,
            "source_stock": stock,
            "period":       period,
            "years":        years,     # 保持字段名向后兼容
            "croic":        croic,
            "financial_debt": fin_debt,
            "ccc":          ccc,
            "piotroski":    piotroski,
            "piotroski_max": piotroski_max,
            "cf_available":  cf_available,
            "is_jp":          stock.endswith(".T") or tk in ("TDK", "KIOXIA", "FUJIKURA"),
            # Phase 3a: 估值指标（PBR/PER/ROE/股息/权益比率）
            "pbr":            pbr,
            "per":            per,
            "roe_pct":        roe_pct,
            "div_yield_pct":  div_yield,
            "equity_ratio":   equity_ratio,  # 4 年 series (%)
            "latest": {
                "croic":     croic[-1] if croic else None,
                "fin_debt":  fin_debt[-1] if fin_debt else None,
                "ccc":       ccc[-1] if ccc else None,
                "piotroski": piotroski[-1] if piotroski else None,
                "equity_ratio": equity_ratio[-1] if equity_ratio else None,
            },
        }
    except Exception as e:
        return {"ticker": tk, "source": stock, "period": period, "error": str(e)[:150]}


def api_jp_guidance(ticker: str) -> dict:
    """業績予想状态。官方结构化事实优先，未接入时明确返回未核验。

    返 {ticker, direction: 上修/下修/据え置き/不明, magnitude: 小/中/大,
        last_update, ref: 关键理由, next_earnings, confidence}
    """
    tk = (ticker or "").upper().strip()
    stock = TICKER_TO_FUNDAMENTAL_STOCK.get(tk, tk)
    verified = _official_jp_guidance(tk)
    if verified is not None:
        # 官方 IR 事实优先；同时补上分析师共识供交叉核验（可能与官方指引背离，用户想看到）
        if stock and stock.endswith(".T"):
            ac = _cached(f"analyst_consensus_{tk}", ttl_sec=6 * 3600,
                         compute_fn=lambda: _fetch_analyst_consensus(stock, ticker=tk))
            if ac and not ac.get("error"):
                verified["analyst_consensus"] = ac
        return verified
    # 只 JP 有意义
    if not (stock and (stock.endswith(".T") or tk in ["TDK", "KIOXIA", "FUJIKURA"])):
        return {"ticker": tk, "error": "not_jp_stock"}
    placeholder = {
        "ticker": tk,
        "direction": "查询中",
        "revision_type": "computing",
        "magnitude": "-",
        "guidance_note": "AI CLI 正在联网查询业绩预想，30-90 秒后刷新可见（首次冷启动）",
        "confidence": "computing",
        "source_verified": False,
        "source_status": "computing",
    }
    # 72h TTL —— 業績予想 除财报当天基本不变，联网查询成本敏感
    # 财报日附近如需强制刷 → 手动 rm .webui_cache/jp_guidance_v2_<TK>.json
    return _cached(f"jp_guidance_v2_{tk}", ttl_sec=72 * 3600,
                    compute_fn=lambda: _compute_jp_guidance(tk, stock),
                    first_call_async=True,
                    first_call_placeholder=placeholder)


def _fetch_historical_pe_bands(stock: str) -> dict | None:
    """5 年历史 P/E 分布：median / p25 / p75 / min / max。用 yfinance history + income_stmt.

    对每个交易日，找 ≤该日的最近 EPS 报告 → 计算 P/E。
    """
    try:
        import yfinance as yf
        import statistics
        t = yf.Ticker(stock)
        prices = t.history(period="5y", auto_adjust=False)
        income = t.income_stmt
        if prices is None or prices.empty or income is None or income.empty:
            return None
        eps_row = None
        for k in ("Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"):
            if k in income.index:
                eps_row = income.loc[k]
                break
        if eps_row is None:
            return None
        eps_row = eps_row.dropna()
        if eps_row.empty:
            return None
        # eps_row.index = fiscal period end dates (Timestamp)
        eps_dates_sorted = sorted(eps_row.index)
        eps_values = {d: float(eps_row[d]) for d in eps_dates_sorted}
        pe_list = []
        for date_ts, price in prices["Close"].items():
            # 找 ≤ 该交易日 的最近 EPS
            applicable_eps = None
            for ed in reversed(eps_dates_sorted):
                # ed 是 pandas Timestamp, date_ts 也是；比较 .date()
                if ed.date() <= date_ts.date():
                    applicable_eps = eps_values[ed]
                    break
            if applicable_eps and applicable_eps > 0:
                pe = float(price) / applicable_eps
                if 3 < pe < 200:   # 剔除极端 outlier
                    pe_list.append(pe)
        if len(pe_list) < 100:
            return None
        pe_sorted = sorted(pe_list)
        n = len(pe_sorted)
        return {
            "median":  round(statistics.median(pe_list), 2),
            "p25":     round(pe_sorted[n // 4], 2),
            "p75":     round(pe_sorted[3 * n // 4], 2),
            "min":     round(pe_sorted[0], 2),
            "max":     round(pe_sorted[-1], 2),
            "n_days":  n,
        }
    except Exception:
        return None


def _fetch_peer_median_pe(ticker: str) -> dict | None:
    """从 signals cache 里读 supply_chain peers → yfinance 拉 forward_pe → median.

    ticker 是短名（TDK / NVDA / SHINETSU），对应 supply_chain_<ticker>.json 缓存文件。
    """
    try:
        import statistics
        sc_cache = _WEBUI_CACHE_DIR / f"supply_chain_{ticker}.json"
        if not sc_cache.exists():
            return None
        sc = json.loads(sc_cache.read_text(encoding="utf-8"))
        peers = sc.get("peers", []) or []
        peer_tickers = [p.get("ticker") for p in peers if p.get("ticker")]
        if not peer_tickers:
            return None
        import yfinance as yf
        pes = []
        for pt in peer_tickers[:6]:   # 上限 6 家避免超时
            try:
                info = yf.Ticker(pt).info or {}
                pe = info.get("forwardPE") or info.get("trailingPE")
                if pe and 3 < float(pe) < 200:
                    pes.append({"ticker": pt, "pe": round(float(pe), 2)})
            except Exception:
                continue
        if not pes:
            return None
        pe_vals = [p["pe"] for p in pes]
        return {
            "median":  round(statistics.median(pe_vals), 2),
            "n_peers": len(pes),
            "peers":   pes,
        }
    except Exception:
        return None


def _finite_number(value):
    """Convert pandas/numpy scalars to a JSON-safe finite float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 4)


def _estimate_frame_rows(frame, fields: tuple[str, ...]) -> dict:
    """Normalize a yfinance estimate DataFrame without depending on pandas."""
    if frame is None or getattr(frame, "empty", True):
        return {}
    rows: dict[str, dict] = {}
    try:
        iterator = frame.iterrows()
    except Exception:
        return {}
    for period, row in iterator:
        values: dict[str, float] = {}
        for field in fields:
            try:
                value = row.get(field) if hasattr(row, "get") else row[field]
            except Exception:
                continue
            number = _finite_number(value)
            if number is not None:
                values[field] = number
        if values:
            rows[str(period)] = values
    return rows


def _ticker_estimate_table(ticker_obj, attribute: str):
    try:
        return getattr(ticker_obj, attribute)
    except Exception:
        return None


def _fetch_forward_estimate_snapshot(ticker_obj) -> dict:
    """Fetch forward earnings/revenue consensus and revision direction."""
    earnings = _estimate_frame_rows(
        _ticker_estimate_table(ticker_obj, "earnings_estimate"),
        ("avg", "low", "high", "yearAgoEps", "numberOfAnalysts", "growth"),
    )
    revenue = _estimate_frame_rows(
        _ticker_estimate_table(ticker_obj, "revenue_estimate"),
        ("avg", "low", "high", "yearAgoRevenue", "numberOfAnalysts", "growth"),
    )
    trend = _estimate_frame_rows(
        _ticker_estimate_table(ticker_obj, "eps_trend"),
        ("current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"),
    )
    revisions = _estimate_frame_rows(
        _ticker_estimate_table(ticker_obj, "eps_revisions"),
        ("upLast7days", "upLast30days", "downLast7days", "downLast30days"),
    )

    preferred_periods = ("+1y", "0y", "+1q", "0q")
    period = next(
        (p for p in preferred_periods if p in trend or p in revisions),
        next(iter(trend or revisions), None),
    )
    trend_row = trend.get(period, {}) if period else {}
    revision_row = revisions.get(period, {}) if period else {}
    current = _finite_number(trend_row.get("current"))
    thirty_days_ago = _finite_number(trend_row.get("30daysAgo"))
    trend_pct_30d = None
    if current is not None and thirty_days_ago not in (None, 0):
        trend_pct_30d = round((current / thirty_days_ago - 1) * 100, 2)
    up_30d = _finite_number(revision_row.get("upLast30days"))
    down_30d = _finite_number(revision_row.get("downLast30days"))
    net_30d = None
    if up_30d is not None or down_30d is not None:
        net_30d = int(round((up_30d or 0) - (down_30d or 0)))

    if trend_pct_30d is None:
        trend_signal = "unavailable"
    elif trend_pct_30d > 1:
        trend_signal = "upward"
    elif trend_pct_30d < -1:
        trend_signal = "downward"
    else:
        trend_signal = "flat"
    if net_30d is None:
        breadth_signal = "unavailable"
    elif net_30d > 0:
        breadth_signal = "upward"
    elif net_30d < 0:
        breadth_signal = "downward"
    else:
        breadth_signal = "flat"

    directional = {trend_signal, breadth_signal} - {"flat", "unavailable"}
    if len(directional) > 1:
        signal = "mixed"
    elif directional:
        signal = next(iter(directional))
    elif trend_signal == "flat" or breadth_signal == "flat":
        signal = "flat"
    else:
        signal = "unavailable"

    return {
        "earnings": earnings,
        "revenue": revenue,
        "eps_trend": trend,
        "eps_revisions": revisions,
        "revision_summary": {
            "period": period,
            "signal": signal,
            "trend_signal": trend_signal,
            "breadth_signal": breadth_signal,
            "eps_current": current,
            "eps_30d_ago": thirty_days_ago,
            "eps_trend_pct_30d": trend_pct_30d,
            "up_30d": int(up_30d) if up_30d is not None else None,
            "down_30d": int(down_30d) if down_30d is not None else None,
            "net_30d": net_30d,
        },
    }


def _median_number(values: list[float]) -> float | None:
    clean = sorted(float(v) for v in values if _finite_number(v) is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2


def _build_expectation_pricing(consensus: dict) -> dict:
    """Compare price with forward consensus using several transparent anchors."""
    anchor_specs = (
        ("analyst_target", "分析师目标均值", "target_mean", "target_upside_pct"),
        ("pe_hold", "前瞻 EPS × 当前估值", "implied_pe_hold", "implied_pe_hold_upside"),
        ("historical_pe", "前瞻 EPS × 五年 PE 中位", "implied_hist_median", "implied_hist_upside"),
        ("peer_pe", "前瞻 EPS × 同行 PE 中位", "implied_peer_median", "implied_peer_upside"),
    )
    anchors = []
    for key, label, price_field, gap_field in anchor_specs:
        price = _finite_number(consensus.get(price_field))
        gap = _finite_number(consensus.get(gap_field))
        if price is None or gap is None or not (-95 < gap < 500):
            continue
        anchors.append({
            "key": key,
            "label": label,
            "implied_price": round(price, 2),
            "gap_pct": round(gap, 1),
        })

    median_gap = _median_number([row["gap_pct"] for row in anchors])
    median_gap = round(median_gap, 1) if median_gap is not None else None
    estimates = consensus.get("forward_estimates") or {}
    revision = estimates.get("revision_summary") or {}
    revision_signal = revision.get("signal") or "unavailable"

    current_price = _finite_number(consensus.get("current_price"))
    trailing_eps = _finite_number(consensus.get("trailing_eps"))
    historical_pe = _finite_number(consensus.get("hist_pe_median"))
    consensus_growth = _finite_number(consensus.get("eps_growth_pct"))
    required_growth = None
    growth_cushion = None
    if current_price and trailing_eps and trailing_eps > 0 and historical_pe and historical_pe > 0:
        required_eps = current_price / historical_pe
        required_growth = round((required_eps / trailing_eps - 1) * 100, 1)
        if consensus_growth is not None:
            growth_cushion = round(consensus_growth - required_growth, 1)

    if median_gap is None:
        code, label, tone = "insufficient", "数据不足", "muted"
    elif median_gap >= 12:
        if revision_signal in {"downward", "mixed"}:
            code, label, tone = "mixed", "估值与修正信号分歧", "warn"
        else:
            code, label, tone = "underpriced", "市场预期偏低", "ok"
    elif median_gap <= -15 and revision_signal == "mixed":
        code, label, tone = "mixed", "估值与修正信号分歧", "warn"
    elif median_gap <= -15 and revision_signal != "upward":
        code, label, tone = "overpriced", "预期透支", "bad"
    elif median_gap <= -5 or (growth_cushion is not None and growth_cushion <= -5):
        if revision_signal == "downward":
            code, label, tone = "overpriced", "预期透支", "bad"
        else:
            code, label, tone = "fully_priced", "利好已充分计价", "warn"
    elif median_gap >= 5 and revision_signal == "upward":
        code, label, tone = "underpriced", "市场预期偏低", "ok"
    else:
        code, label, tone = "matched", "预期与股价基本匹配", "muted"

    rationale = []
    if median_gap is not None:
        direction = "高于" if median_gap >= 0 else "低于"
        rationale.append(f"多口径隐含价中位数{direction}现价 {abs(median_gap):.1f}%")
    revision_zh = {
        "upward": "近 30 天 EPS 共识上修",
        "downward": "近 30 天 EPS 共识下修",
        "flat": "近 30 天 EPS 共识基本持平",
        "mixed": "近 30 天 EPS 数值与上调/下调家数方向分歧",
        "unavailable": "暂无可靠 EPS 修正序列",
    }.get(revision_signal, "暂无可靠 EPS 修正序列")
    rationale.append(revision_zh)
    if required_growth is not None and consensus_growth is not None:
        rationale.append(
            f"历史 PE 口径要求 EPS 增长 {required_growth:+.1f}%，共识为 {consensus_growth:+.1f}%"
        )

    analyst_count = int(_finite_number(consensus.get("n_analysts")) or 0)
    if len(anchors) >= 3 and analyst_count >= 8 and revision_signal != "unavailable":
        confidence = "high"
    elif len(anchors) >= 2 or analyst_count >= 5:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "classification": code,
        "label": label,
        "tone": tone,
        "confidence": confidence,
        "anchor_count": len(anchors),
        "anchors": anchors,
        "median_gap_pct": median_gap,
        "revision_signal": revision_signal,
        "required_eps_growth_pct": required_growth,
        "consensus_eps_growth_pct": consensus_growth,
        "growth_cushion_pct": growth_cushion,
        "rationale": rationale,
    }


def _fetch_analyst_consensus(
    stock: str,
    ticker: str | None = None,
    include_estimates: bool = False,
) -> dict | None:
    """yfinance 分析师聚合 → 归一化字段。失败返 {'error': ...}，无覆盖返 None。

    stock: yfinance symbol (e.g. NVDA / 6762.T)
    ticker: 内部短名 (e.g. NVDA / TDK) —— 用于查 peer supply_chain cache
    """
    try:
        import yfinance as yf
        t = yf.Ticker(stock)
        info = t.info or {}
        n_analysts   = info.get("numberOfAnalystOpinions")
        rec_key      = info.get("recommendationKey")
        target_mean  = info.get("targetMeanPrice")
        target_high  = info.get("targetHighPrice")
        target_low   = info.get("targetLowPrice")
        forward_eps  = info.get("forwardEps")
        trailing_eps = info.get("trailingEps")
        forward_pe   = info.get("forwardPE")
        trailing_pe  = info.get("trailingPE")
        earnings_growth = info.get("earningsGrowth")
        revenue_growth  = info.get("revenueGrowth")
        current_price   = info.get("currentPrice") or info.get("regularMarketPrice")
        next_earn = None
        try:
            cal = t.calendar
            if hasattr(cal, "get"):
                dates = cal.get("Earnings Date")
                if dates and len(dates) > 0:
                    next_earn = str(dates[0])
        except Exception:
            pass
        if not (n_analysts or forward_eps or target_mean):
            return None
        eps_growth_pct = None
        if forward_eps is not None and trailing_eps and trailing_eps > 0:
            eps_growth_pct = round((forward_eps / trailing_eps - 1) * 100, 1)
        target_upside_pct = None
        if target_mean and current_price and current_price > 0:
            target_upside_pct = round((target_mean / current_price - 1) * 100, 1)

        # ── 前瞻 EPS 派生估值 (multi-anchor PE) ──
        implied_pe_hold = None
        implied_pe_hold_upside = None
        peg_fair_price = None
        peg_ratio = None
        if forward_eps and trailing_pe:
            implied_pe_hold = round(forward_eps * trailing_pe, 2)
            if current_price and current_price > 0:
                implied_pe_hold_upside = round(
                    (implied_pe_hold / current_price - 1) * 100, 1
                )
        if forward_eps and eps_growth_pct and eps_growth_pct > 0:
            peg_fair_price = round(forward_eps * eps_growth_pct, 2)
        if forward_pe and eps_growth_pct and eps_growth_pct > 0:
            peg_ratio = round(forward_pe / eps_growth_pct, 2)

        # 5 年历史 P/E band (median / p25 / p75) —— 主流卖方 anchor 1
        hist_pe = _fetch_historical_pe_bands(stock)
        implied_hist_median = None
        implied_hist_p25 = None
        implied_hist_p75 = None
        implied_hist_upside = None
        if hist_pe and forward_eps:
            implied_hist_median = round(forward_eps * hist_pe["median"], 2)
            implied_hist_p25    = round(forward_eps * hist_pe["p25"], 2)
            implied_hist_p75    = round(forward_eps * hist_pe["p75"], 2)
            if current_price and current_price > 0:
                implied_hist_upside = round(
                    (implied_hist_median / current_price - 1) * 100, 1
                )

        # 同行 median PE —— 主流卖方 anchor 2
        peer_pe = _fetch_peer_median_pe(ticker) if ticker else None
        implied_peer_median = None
        implied_peer_upside = None
        if peer_pe and forward_eps:
            implied_peer_median = round(forward_eps * peer_pe["median"], 2)
            if current_price and current_price > 0:
                implied_peer_upside = round(
                    (implied_peer_median / current_price - 1) * 100, 1
                )

        result = {
            "n_analysts":         n_analysts,
            "recommendation":     rec_key,
            "current_price":      round(current_price, 2) if current_price else None,
            "forward_eps":        round(forward_eps, 2) if forward_eps else None,
            "trailing_eps":       round(trailing_eps, 2) if trailing_eps else None,
            "eps_growth_pct":     eps_growth_pct,
            "forward_pe":         round(forward_pe, 2) if forward_pe else None,
            "trailing_pe":        round(trailing_pe, 2) if trailing_pe else None,
            "target_mean":        round(target_mean, 2) if target_mean else None,
            "target_high":        round(target_high, 2) if target_high else None,
            "target_low":         round(target_low, 2) if target_low else None,
            "target_upside_pct":  target_upside_pct,
            "revenue_growth_pct": round(revenue_growth * 100, 1) if revenue_growth is not None else None,
            "earnings_growth_pct": round(earnings_growth * 100, 1) if earnings_growth is not None else None,
            "next_earnings":      next_earn,
            # 前瞻 EPS 派生估值 (multi-anchor)
            "implied_pe_hold":         implied_pe_hold,
            "implied_pe_hold_upside":  implied_pe_hold_upside,
            "peg_fair_price":          peg_fair_price,
            "peg_ratio":               peg_ratio,
            # 5y 历史 PE band
            "hist_pe_median":          hist_pe["median"] if hist_pe else None,
            "hist_pe_p25":             hist_pe["p25"] if hist_pe else None,
            "hist_pe_p75":             hist_pe["p75"] if hist_pe else None,
            "hist_pe_n_days":          hist_pe["n_days"] if hist_pe else None,
            "implied_hist_median":     implied_hist_median,
            "implied_hist_p25":        implied_hist_p25,
            "implied_hist_p75":        implied_hist_p75,
            "implied_hist_upside":     implied_hist_upside,
            # Peer median PE (from supply_chain)
            "peer_pe_median":          peer_pe["median"] if peer_pe else None,
            "peer_pe_n_peers":         peer_pe["n_peers"] if peer_pe else None,
            "peer_pe_details":         peer_pe["peers"] if peer_pe else None,
            "implied_peer_median":     implied_peer_median,
            "implied_peer_upside":     implied_peer_upside,
            "source":             "yfinance analyst aggregation",
        }
        if include_estimates:
            result["forward_estimates"] = _fetch_forward_estimate_snapshot(t)
        return result
    except Exception as e:
        return {"error": str(e)[:120]}


EQUITY_OUTLOOK_CACHE_SCHEMA = 1


def _equity_outlook_cache_name(source_stock: str) -> str:
    cache_key = re.sub(r"[^A-Z0-9]+", "_", source_stock.upper()).strip("_")
    return f"equity_outlook_v{EQUITY_OUTLOOK_CACHE_SCHEMA}_{cache_key}"


def _compute_equity_outlook(source_stock: str) -> dict:
    source_ticker = source_stock.split(".", 1)[0].upper()
    consensus = _fetch_analyst_consensus(
        source_stock,
        ticker=source_ticker,
        include_estimates=True,
    )
    if not consensus:
        return {
            "cache_schema": EQUITY_OUTLOOK_CACHE_SCHEMA,
            "source_stock": source_stock,
            "state": "unavailable",
            "error": "no_analyst_forward_coverage",
        }
    if consensus.get("error"):
        return {
            "cache_schema": EQUITY_OUTLOOK_CACHE_SCHEMA,
            "source_stock": source_stock,
            "state": "error",
            "error": consensus["error"],
        }
    return {
        "cache_schema": EQUITY_OUTLOOK_CACHE_SCHEMA,
        "source_stock": source_stock,
        "state": "ready",
        "analyst_consensus": consensus,
        "pricing": _build_expectation_pricing(consensus),
        "official_guidance_status": "not_connected",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "yfinance analyst aggregation",
    }


def api_equity_outlook(ticker: str) -> dict:
    """Forward consensus and expectation-vs-price analysis for equity cards."""
    tk = (ticker or "").upper().strip().removeprefix("US.")
    profile = ticker_display_profile(tk)
    if not tk or not profile.get("show_forward_outlook", True):
        return {"ticker": tk, "error": "no_forward_outlook_for_this_type"}
    source_stock = TICKER_TO_FUNDAMENTAL_STOCK.get(tk, tk)
    if source_stock is None:
        return {"ticker": tk, "error": "no_forward_outlook_for_this_type"}

    data = _cached(
        _equity_outlook_cache_name(source_stock),
        ttl_sec=6 * 3600,
        compute_fn=lambda: _compute_equity_outlook(source_stock),
        first_call_async=True,
        first_call_placeholder={
            "source_stock": source_stock,
            "state": "loading",
            "cache_schema": EQUITY_OUTLOOK_CACHE_SCHEMA,
        },
    )
    return {
        **data,
        "ticker": tk,
        "source_stock": source_stock,
        "is_proxy": source_stock.upper() != tk,
    }


_TRUSTED_JP_GUIDANCE_DOMAINS = (
    # JPX 官方 & 上市公司自有域
    "jpx.co.jp", "release.tdnet.info", ".co.jp", ".com/investor", ".com/ir",
    # 主流财经媒体
    "nikkei.com", "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "jiji.com", "kyodonews.jp", "asahi.com", "yomiuri.co.jp",
    # 证券分析平台
    "kabuyoho.jp", "shikiho.jp", "traders.co.jp", "morningstar.jp",
    "kabutan.jp", "minkabu.jp", "diamond.jp",
)


def _validate_guidance_sources(sources: list) -> list:
    """Only keep URLs pointing at trusted Japanese IR/news domains."""
    out = []
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url", "")).strip().lower()
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        if any(dom in url for dom in _TRUSTED_JP_GUIDANCE_DOMAINS):
            out.append({
                "url":   s.get("url"),
                "type":  s.get("type") or "unknown",
                "title": (s.get("title") or "")[:180],
            })
    return out


def _fetch_jp_guidance_via_ai(tk: str, stock: str, name_zh: str, name_en: str) -> dict | None:
    """AI CLI + live web search 查最新業績予想；每数字必须带可信 URL 出处。

    找不到 or URL 全部不可信 → 返 None（调用方保留 待核验 占位）。
    """
    try:
        from ai_prompt import query_ai_cli
    except Exception:
        return None

    stock_code = stock.replace(".T", "") if stock.endswith(".T") else stock
    prompt = f"""你是日本股权分析师。任务：查找并核验下列公司最近 90 天内官方发布的通期業績予想（full-year earnings guidance）。

Target:
  Ticker:  {tk}
  日语正式名/中文名: {name_zh}
  英文名:  {name_en}
  证券コード: {stock_code} (TSE)

严格要求：
1. 必须使用 WebSearch 查真实公开来源（快！最多 2 次搜索，不要多次迭代）：
   · 优先 release.tdnet.info（TDnet 決算短信）/ 公司 IR 官网（domain 含 investor 或 ir）
   · 次选 nikkei.com / reuters.com / bloomberg.com
2. 每个关键数字（EPS/売上/営業利益/純利益）必须能在你返回的 sources URL 里找到原文
3. 拒绝任何无来源的数字。找不到最新公告就返 direction="待核验"、confidence="low"
4. revision_reason 必须包含具体数值变化（如 "純利予想 2,800 → 3,200 億円 +14%"）
5. last_update 是公告发布日；next_earnings 是下次财报日
6. sources 1-3 条即可（不需要多），每条 URL 必须真实可打开
7. 时间预算：请在 3 分钟内完成，不要试图穷举所有来源

输出严格 JSON 对象（无 markdown 围栏、无解释文字）：
{{
  "direction":       "上修" | "下修" | "据え置き" | "新指引" | "待核验",
  "revision_type":   "upward" | "downward" | "maintained" | "initial" | "unverified",
  "magnitude":       "大" | "中" | "小" | "不明",
  "last_update":     "YYYY-MM-DD",
  "next_earnings":   "YYYY-MM-DD",
  "guidance_note":   "1-2 句核心指引数字（含单位，例：通期純利 3,200 億円 +8% YoY）",
  "revision_reason": "1-2 句修正原因（必含具体数据变化）",
  "market_reaction": "高" | "中" | "低" | "不明",
  "sources": [
    {{"url": "https://...", "type": "IR" | "TDnet" | "news", "title": "..."}}
  ],
  "confidence": "high" | "medium" | "low"
}}
"""
    to = int(os.environ.get("JP_GUIDANCE_TIMEOUT", "240"))
    out, status, provider, _ = query_ai_cli(
        prompt, timeout=to, web_search=True
    )
    provider = provider.lower()
    if not out:
        return {"_error": f"{provider}_{status[:120]}"}
    import re as _re
    txt = out.strip()
    txt = _re.sub(r"^```(?:json)?\s*", "", txt)
    txt = _re.sub(r"\s*```\s*$", "", txt)
    m = _re.search(r"\{.*\}", txt, _re.S)
    if m:
        txt = m.group(0)
    try:
        data = json.loads(txt)
    except Exception as e:
        return {"_error": f"parse_{str(e)[:80]}"}
    if not isinstance(data, dict):
        return {"_error": "not_dict"}

    verified_sources = _validate_guidance_sources(data.get("sources"))
    direction = str(data.get("direction", "")).strip() or "待核验"
    # 硬门槛：direction 非"待核验" → 必须至少 1 条可信 URL
    if direction != "待核验" and not verified_sources:
        return {"_error": "no_trusted_source", "raw_direction": direction}
    reason = (data.get("revision_reason") or "").strip() or None
    note   = (data.get("guidance_note") or "").strip() or None
    return {
        "direction":       direction,
        "revision_type":   data.get("revision_type") or "unverified",
        "magnitude":       data.get("magnitude") or "不明",
        "last_update":     data.get("last_update"),
        "next_earnings":   data.get("next_earnings"),
        "guidance_note":   note,
        "revision_reason": reason,
        "market_reaction": data.get("market_reaction") or "不明",
        "confidence":      data.get("confidence") or "low",
        "sources":         verified_sources,
        "source_via":      f"{provider}_cli_websearch",
    }


def _compute_jp_guidance(tk: str, stock: str) -> dict:
    """業績予想：AI CLI+WebSearch 优先（每数字带 URL），失败回落 unverified；始终附 yfinance 共识。"""
    result = {
        "ticker": tk,
        "source_stock": stock,
        "direction": "待核验",
        "revision_type": "unverified",
        "magnitude": "不明",
        "last_update": None,
        "next_earnings": None,
        "guidance_note": "尚未接入该公司的官方 IR 结构化数据；已停用无来源模型数字。",
        "revision_reason": None,
        "market_reaction": "不明",
        "confidence": "unverified",
        "source_verified": False,
        "source_status": "not_onboarded",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # 找 name_zh/name_en (from JP_WATCH_LIST) for prompt context
    name_zh = tk
    name_en = tk
    for entry in JP_WATCH_LIST:
        if entry.get("ticker") == tk:
            name_zh = entry.get("name_zh", tk)
            name_en = entry.get("name_en", tk)
            break
    # AI CLI 查带 URL 出处的业绩预想；旧变量名保留兼容。
    guidance_ai_enabled = os.environ.get(
        "JP_GUIDANCE_AI", os.environ.get("JP_GUIDANCE_CLAUDE", "1")
    )
    if guidance_ai_enabled != "0":
        cg = _fetch_jp_guidance_via_ai(tk, stock, name_zh, name_en)
        if cg and not cg.get("_error"):
            result.update({
                "direction":        cg["direction"],
                "revision_type":    cg["revision_type"],
                "magnitude":        cg["magnitude"],
                "last_update":      cg["last_update"],
                "next_earnings":    cg["next_earnings"],
                "guidance_note":    cg["guidance_note"] or result["guidance_note"],
                "revision_reason":  cg["revision_reason"],
                "market_reaction":  cg["market_reaction"],
                "confidence":       cg["confidence"],
                "sources":          cg["sources"],
                "source_via":       cg["source_via"],
                "source_verified":  bool(cg["sources"]),
                "source_status":    cg["source_via"],
            })
        elif cg and cg.get("_error"):
            result["ai_error"] = cg["_error"]
            if cg.get("raw_direction"):
                # AI 说了方向但没给可信 URL → 降级但标注
                result["direction"] = "待核验"
                result["guidance_note"] = f"AI 返回 {cg['raw_direction']} 但未附可核验来源，已拒收"
    # yfinance 分析师共识（独立于官方指引，无论 AI 成败都附上）
    ac = _fetch_analyst_consensus(stock, ticker=tk)
    if ac and not ac.get("error"):
        result["analyst_consensus"] = ac
        if not result["source_verified"]:
            result["source_verified"] = True
            result["source_status"] = "analyst_only"
        if ac.get("next_earnings") and not result["next_earnings"]:
            result["next_earnings"] = ac["next_earnings"]
    elif ac and ac.get("error"):
        result["analyst_consensus_error"] = ac["error"]
    return result


def api_tostnet_hits(ticker: str, days: int = 10) -> dict:
    """JPX ToSTNeT ≥50 亿円 block trade 命中：某 JP ticker 最近 N 日是否有大宗成交。

    T+1 官方免费数据源，用于印证 moomoo/yfinance capital flow anomaly 是否落地为 block trade。
    缓存 6h（ToSTNeT 每日 15:00-16:00 JST 后发布，一天最多刷一次）。
    """
    tk = (ticker or "").strip()
    if not tk:
        return {"error": "no_ticker"}
    def _compute():
        try:
            from jp_tostnet import check_ticker_hits
            hits = check_ticker_hits(tk, n_days=days)
            return {"ticker": tk, "days": days, "hits": hits, "count": len(hits)}
        except Exception as e:
            return {"ticker": tk, "days": days, "hits": [], "error": str(e)[:200]}
    return _cached(f"tostnet_{tk}_{days}", ttl_sec=6*3600, compute_fn=_compute)


def api_ai_targets() -> dict:
    """AI 价位计划 + 最新规则决策 + broker 实际未结订单。

    旧实现只返回最近一份 AI JSON，页面因此会把已经被新规则决策覆盖的
    价位计划误称为“实际挂单”。现在以 broker 未结订单为事实源；AI 只
    提供 entry/stop/target 参考，规则引擎的最新 action 决定计划是否仍有效。
    """
    files = sorted(SIGNALS_DIR.glob("ai_targets_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    latest = files[0] if files else None
    try:
        content = (
            json.loads(latest.read_text(encoding="utf-8"))
            if latest is not None else {"targets": []}
        )
        target_mtime = latest.stat().st_mtime if latest is not None else 0.0

        # 只把 paper_trader 提交的订单算作“系统自动单”；手工单不混入。
        submitted_ids: set[str] = set()
        ledger_path = SIGNALS_DIR / "execution_ledger.jsonl"
        if ledger_path.exists():
            for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("event") == "submitted" and event.get("order_id"):
                    submitted_ids.add(str(event["order_id"]))

        pending_rows: list[dict] | None
        try:
            from moomoo import RET_OK
            from paper_trader import _ctx_get, _TRADER_LOCK, ACC_ID, TRD_ENV
            with _TRADER_LOCK:
                ctx = _ctx_get()
                ret, orders = ctx.order_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
            if ret != RET_OK or orders is None:
                pending_rows = None
            else:
                pending_statuses = {
                    "SUBMITTED", "SUBMITTING", "WAITING_SUBMIT", "FILLED_PART",
                }
                pending_rows = []
                for _, row in orders.iterrows():
                    status = str(row.get("order_status") or "").upper()
                    if status not in pending_statuses:
                        continue
                    oid = str(row.get("order_id") or "")
                    pending_rows.append({
                        "order_id": oid,
                        "ticker": str(row.get("code") or ""),
                        "side": str(row.get("trd_side") or ""),
                        "qty": float(row.get("qty") or 0),
                        "dealt_qty": float(row.get("dealt_qty") or 0),
                        "price": float(row.get("price") or 0),
                        "status": status,
                        "system_managed": oid in submitted_ids,
                    })
        except Exception:
            pending_rows = None

        position_qty: dict[str, int] = {}
        live_positions = _fetch_moomoo_live_positions()
        if live_positions is not None:
            position_qty = {p["code"]: int(p["qty"]) for p in live_positions}

        raw_targets = content.get("targets") or []
        targets_by_ticker = {
            str(t.get("ticker") or ""): dict(t)
            for t in raw_targets if isinstance(t, dict) and t.get("ticker")
        }
        system_pending = [
            row for row in (pending_rows or []) if row.get("system_managed")
        ]
        # 即便 AI JSON 没列出某只票，也必须展示真实存在的系统未结单。
        for row in system_pending:
            targets_by_ticker.setdefault(row["ticker"], {
                "ticker": row["ticker"], "action": "hold", "entry_ref": None,
                "stop_ref": None, "target_ref": None, "use_limit": False,
                "notes": "broker 中存在系统订单，但当前 AI 价位文件未包含该标的",
            })

        merged_targets = []
        buy_actions = {"BUY", "WATCH_BUY", "WATCH_BUY_PROBE", "CRISIS_PROBE"}
        for ticker, target in targets_by_ticker.items():
            short = ticker.split(".")[-1]
            signal_path = SIGNALS_DIR / f"{short}_latest.json"
            signal = {}
            signal_mtime = 0.0
            if signal_path.exists():
                try:
                    signal = json.loads(signal_path.read_text(encoding="utf-8"))
                    signal_mtime = signal_path.stat().st_mtime
                except Exception:
                    signal = {}
            decision = signal.get("decision") or {}
            market = signal.get("market") or {}
            system_action = str(decision.get("action") or "UNKNOWN").upper()
            ticker_orders = [r for r in system_pending if r.get("ticker") == ticker]
            has_position = position_qty.get(ticker, 0) > 0
            ai_action = str(target.get("action") or "hold").lower()
            ai_buy_plan = ai_action in {"buy", "watch_buy"} and bool(target.get("entry_ref"))
            target_outdated = bool(signal_mtime and target_mtime and signal_mtime > target_mtime)

            if ticker_orders:
                execution_status = "submitted"
            elif has_position:
                execution_status = "position_open"
            elif system_action in buy_actions:
                execution_status = "candidate"
            elif ai_buy_plan and system_action not in buy_actions:
                execution_status = "inactive_signal"
            else:
                execution_status = "inactive"

            system_entry_ref = decision.get("entry_ref") or market.get("price")
            # 一旦出现更新的规则决策，旧 AI 数字只留在原始审计字段中，
            # 不再作为 dashboard/API 消费者的“当前可执行价”。
            if system_action in buy_actions:
                effective_entry_ref = (
                    target.get("entry_ref") if not target_outdated and ai_buy_plan
                    else system_entry_ref
                )
                effective_stop_ref = (
                    target.get("stop_ref") if not target_outdated and ai_buy_plan
                    else decision.get("stop_ref")
                )
                effective_target_ref = (
                    target.get("target_ref") if not target_outdated and ai_buy_plan
                    else decision.get("target_ref")
                )
            else:
                effective_entry_ref = None
                effective_stop_ref = None
                effective_target_ref = None

            target.update({
                "system_action": system_action,
                "system_confidence": decision.get("confidence"),
                "system_reason": decision.get("reason"),
                "system_ts": signal.get("ts"),
                "target_outdated": target_outdated,
                "execution_status": execution_status,
                "effective_entry_ref": effective_entry_ref,
                "effective_stop_ref": effective_stop_ref,
                "effective_target_ref": effective_target_ref,
                "effective_price_source": (
                    "ai_target" if not target_outdated and ai_buy_plan
                    else "latest_system_decision"
                ),
                "position_qty": position_qty.get(ticker, 0),
                "pending_orders": ticker_orders,
            })
            merged_targets.append(target)

        return {
            "exists": bool(latest is not None or system_pending),
            "path": latest.name if latest is not None else None,
            "mtime": (
                datetime.fromtimestamp(target_mtime).isoformat()
                if target_mtime else None
            ),
            "broker_status": "ok" if pending_rows is not None else "unavailable",
            "pending_count": len(system_pending),
            "pending_orders": system_pending,
            "manual_pending_count": len([
                row for row in (pending_rows or []) if not row.get("system_managed")
            ]),
            **content,
            "targets": merged_targets,
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def api_bond_monitor() -> dict:
    """美债 yields + TIPS 实际利率 + GLD 20d 相关性 + 异常波动。缓存 15 min。"""
    def _compute():
        try:
            from bond_monitor import get_bond_monitor
            return get_bond_monitor()
        except Exception as e:
            return {"error": str(e)[:200]}
    # v2 invalidates legacy cache files containing non-standard NaN tokens.
    return _cached("bond_monitor_v2", ttl_sec=900, compute_fn=_compute)


def api_bond_ai_interpret() -> dict:
    """bond_monitor 数字 → AI CLI 大白话解读。缓存 6h（macro 变化慢）+ 首次异步。"""
    placeholder = {
        "one_liner": "AI 正在解读宏观数据（首次约 30-60s）…",
        "verdict": "computing",
        "reasoning": "",
        "confidence": "computing",
    }
    def _compute():
        try:
            from bond_monitor import get_bond_monitor
            from bond_ai_interpret import interpret_bond_context
            bond = get_bond_monitor()
            return interpret_bond_context(bond)
        except Exception as e:
            return {"error": str(e)[:200]}
    return _cached("bond_ai_interpret", ttl_sec=6 * 3600,
                   compute_fn=_compute,
                   first_call_async=True,
                   first_call_placeholder=placeholder)


def api_fed_watch() -> dict:
    """CME FedWatch 加息/降息预期。AI CLI + live web search，缓存 8h。首次异步。"""
    placeholder = {
        "commentary": "Fed rate expectations 正在联网查询（AI CLI + Web，30-90s）…",
        "meetings": [],
        "confidence": "computing",
    }
    def _compute():
        try:
            from fed_watch import get_fed_watch
            return get_fed_watch()
        except Exception as e:
            return {"error": str(e)[:200]}
    # 8h TTL —— 加息预期几小时内基本不变，除 Powell 讲话/CPI 当天。节省 CLI 额度。
    return _cached("fed_watch", ttl_sec=8 * 3600,
                   compute_fn=_compute,
                   first_call_async=True,
                   first_call_placeholder=placeholder)


def api_capital_flow(ticker: str) -> dict:
    """大单/散户资金流向。US → moomoo super/big/mid/small；JP → yfinance 5min 量能异常代理。

    缓存 5 min（intraday 数据更新频繁，5 min 兼顾新鲜度和 API 频率）。
    """
    tk = (ticker or "").upper().strip()
    if not tk:
        return {"error": "no_ticker"}
    def _compute():
        try:
            from capital_flow import get_capital_flow
            return get_capital_flow(tk)
        except Exception as e:
            return {"ticker": tk, "source": "unavailable",
                    "error": str(e)[:200],
                    "flow": None,
                    "anomaly": {"detected": False, "reason": "", "score": 0}}
    return _cached(f"capital_flow_{tk}", ttl_sec=300, compute_fn=_compute)


def api_events(days_ahead: int = 45) -> dict:
    """未来 N 天内的宏观 + 财报事件（CPI/PPI/NFP/FOMC/NVDA earnings 等）。

    数据源：events_watch.EQUITY_CALENDAR。今日的事件保留，1 天前也保留（当日盘后可能有影响）。
    """
    try:
        from events_watch import EQUITY_CALENDAR
    except Exception as e:
        return {"events": [], "error": str(e)}
    from datetime import date
    today = date.today()
    out = []
    for ev in EQUITY_CALENDAR:
        try:
            ev_date = date.fromisoformat(ev["date"])
        except Exception:
            continue
        delta = (ev_date - today).days
        if delta < -1 or delta > days_ahead:
            continue
        out.append({
            "date":   ev["date"],
            "days":   delta,
            "event":  ev["event"],
            "impact": ev.get("impact", "med"),
        })
    out.sort(key=lambda x: x["days"])
    return {"events": out, "today": today.isoformat(), "days_ahead": days_ahead}


def _ticker_ai_snapshot_parse() -> dict:
    """从最新 ai_analysis_*.md 提取 '### TICKER' 段落。旧机制，作为 live 未覆盖时的 fallback。"""
    import re
    files = sorted(SIGNALS_DIR.glob("ai_analysis_*.md"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"exists": False, "tickers": {}}
    latest = files[0]
    try:
        content = latest.read_text(encoding="utf-8")
    except Exception as e:
        return {"exists": False, "error": str(e), "tickers": {}}
    body = content
    m_start = re.search(r"^##\s*分标的执行", body, re.MULTILINE)
    if m_start:
        body = body[m_start.end():]
    m_end = re.search(r"^##\s+", body, re.MULTILINE)
    if m_end:
        body = body[:m_end.start()]
    tickers: dict[str, str] = {}
    parts = re.split(r"^###\s+([A-Z]{2,6})\b", body, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        tk = parts[i].strip().upper()
        seg = parts[i + 1] if i + 1 < len(parts) else ""
        seg = seg.strip()
        if tk and seg:
            tickers[tk] = seg[:2000]
    return {
        "exists": True,
        "path":   latest.name,
        "mtime":  datetime.fromtimestamp(latest.stat().st_mtime).isoformat(),
        "tickers": tickers,
    }


def _read_fundamentals_cache_for_ai(ticker: str) -> dict | None:
    """Read the current fundamentals cache naming scheme for equity AI context."""
    tk = (ticker or "").upper().removeprefix("US.")
    if not ticker_display_profile(tk)["show_fundamentals"]:
        return None
    # Annual data is the stable long-horizon context. Fall back to quarterly
    # and then the pre-period legacy filename so existing caches remain usable.
    for name in (
        f"fundamentals_{tk}_year.json",
        f"fundamentals_{tk}_quarter.json",
        f"fundamentals_{tk}.json",
    ):
        path = _WEBUI_CACHE_DIR / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and "error" not in data:
            return data
    return None


def _read_equity_outlook_cache_for_ai(ticker: str) -> dict | None:
    """Read a completed outlook cache without triggering network work."""
    tk = (ticker or "").upper().removeprefix("US.")
    if not ticker_display_profile(tk).get("show_forward_outlook", True):
        return None
    source_stock = TICKER_TO_FUNDAMENTAL_STOCK.get(tk, tk)
    if not source_stock:
        return None
    path = _WEBUI_CACHE_DIR / f"{_equity_outlook_cache_name(source_stock)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("state") != "ready":
        return None
    return data


_TICKER_AI_CACHE_SCHEMA = 4


def _ticker_ai_cache_key(ticker_rows: list[tuple]) -> str:
    """Hash every input that can alter the generated per-ticker AI analysis."""
    import hashlib

    ticker_payloads = []
    for row in ticker_rows:
        tk, sig, opt, fundamentals = row[:4]
        outlook = row[4] if len(row) >= 5 else None
        ticker_payloads.append({
            "ticker": tk,
            "signal": {
                key: sig.get(key)
                for key in ("action_zh", "conf", "scale", "bull_count", "bear_count",
                            "rsi", "vol_ratio", "regime")
            },
            "options": {
                "spot": opt.get("spot"),
                "call_wall_oi": opt.get("call_wall_oi"),
                "put_wall_oi": opt.get("put_wall_oi"),
                "leveraged_mapping": opt.get("leveraged_mapping"),
                "squeeze_type": (
                    opt.get("squeeze_risk", {}).get("type")
                    if isinstance(opt.get("squeeze_risk"), dict) else None
                ),
            },
            "fundamentals": (
                {
                    key: fundamentals.get(key)
                    for key in ("source_stock", "period", "years", "latest", "croic",
                                "piotroski", "financial_debt", "ccc")
                }
                if fundamentals else None
            ),
            "outlook": (
                {
                    "source_stock": outlook.get("source_stock"),
                    "pricing": outlook.get("pricing"),
                    "consensus": {
                        key: (outlook.get("analyst_consensus") or {}).get(key)
                        for key in ("forward_eps", "forward_pe", "eps_growth_pct",
                                    "target_upside_pct", "next_earnings")
                    },
                }
                if outlook else None
            ),
        })

    payload = {
        "schema": _TICKER_AI_CACHE_SCHEMA,
        "tickers": ticker_payloads,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _ticker_ai_live_all(signals: dict, options: dict) -> dict:
    """一次 AI CLI 调用生成所有 tracked ticker 的即时分析。

    缓存 key = sha1(标的 + action + conf + spot + wall OI)。数据变才重跑 AI CLI。
    非阻塞：无缓存时后台线程跑，本次请求返 state=generating。
    """
    if not signals and not options:
        return {"tickers": {}, "state": "empty"}

    tk_list = []
    # AI coverage follows actual signal/option data, not the unrelated option
    # source mapping. Macro cards intentionally opt out via their profile.
    for tk in sorted(set(signals) | set(options)):
        if not ticker_display_profile(tk)["show_ai_analysis"]:
            continue
        sig = signals.get(tk, {})
        opt = options.get(tk, {})
        if not sig and (not opt or 'error' in opt):
            continue
        fundamentals = _read_fundamentals_cache_for_ai(tk)
        outlook = _read_equity_outlook_cache_for_ai(tk)
        tk_list.append((tk, sig, opt, fundamentals, outlook))

    if not tk_list:
        return {"tickers": {}, "state": "empty"}

    key = _ticker_ai_cache_key(tk_list)
    cache_path = _WEBUI_CACHE_DIR / f"ticker_ai_live_{key}.json"

    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            data["state"] = "cached"
            return data
        except Exception:
            pass

    flag_key = f"ticker_ai_live_{key}"
    if not _refresh_flag.get(flag_key):
        def _bg():
            _refresh_flag[flag_key] = True
            try:
                from ai_prompt import query_ai_cli
                lines = []
                for tk, s, o, fd, outlook in tk_list:
                    lines.append(f"--- {tk} ---")
                    lines.append(
                        f"信号: {s.get('action_zh','?')} conf={s.get('conf','?')}/{s.get('scale','?')} "
                        f"| 多{s.get('bull_count',0)}/空{s.get('bear_count',0)} | "
                        f"RSI={s.get('rsi','?')} 量比={s.get('vol_ratio','?')} regime={s.get('regime','?')}"
                    )
                    if o and 'error' not in o:
                        of = o.get('offense', {}) or {}
                        ds = o.get('defense', {}) or {}
                        sr = o.get('squeeze_risk', {}) or {}
                        sm = o.get('sentiment', {}) or {}
                        lm = o.get('leveraged_mapping') or {}
                        mapped_levels = lm.get('levels') or {}

                        def _mapped_suffix(level_name: str) -> str:
                            mapped = mapped_levels.get(level_name) or {}
                            if not mapped:
                                return ""
                            suffix = (
                                f" => {lm.get('ticker')}即时≈${mapped.get('spot_anchored_level')}"
                            )
                            if mapped.get('expiry_estimate') is not None:
                                suffix += (
                                    f"；到期路径估≈${mapped.get('expiry_estimate')}"
                                    f"（${mapped.get('expiry_range_low')}-${mapped.get('expiry_range_high')}）"
                                )
                            return suffix

                        chain_line = (
                            f"期权链={o.get('underlying','?')} spot=${o.get('spot')} exp={o.get('expiry')}"
                        )
                        if lm:
                            chain_line += (
                                f" | 交易标的={lm.get('ticker')} spot=${lm.get('leveraged_spot')} "
                                f"杠杆={lm.get('leverage')}x"
                            )
                        lines.append(chain_line)
                        lines.append(
                            f"  Call wall {o.get('underlying','?')} ${of.get('strike')}"
                            f"{_mapped_suffix('call_wall')} OI={of.get('oi',0)} 保费=${of.get('prem')} "
                            f"(距spot {of.get('dist_pct')}%) 强度{of.get('strength','?')}"
                        )
                        lines.append(
                            f"  Put  wall {o.get('underlying','?')} ${ds.get('strike')}"
                            f"{_mapped_suffix('put_wall')} OI={ds.get('oi',0)} 保费=${ds.get('prem')} "
                            f"(距spot {ds.get('dist_pct')}%) 强度{ds.get('strength','?')}"
                        )
                        if lm:
                            decay = lm.get('decay') or {}
                            if decay.get('available'):
                                lines.append(
                                    f"  杠杆折损估计: 历史{decay.get('sample_days')}交易日，"
                                    f"日均carry={decay.get('daily_carry_pct')}%，"
                                    f"到期尚有{lm.get('calendar_days')}日；即时价用于挂单，到期价仅情景估计"
                                )
                            else:
                                lines.append("  杠杆折损估计不可用；只能用即时锚定价，长期价位必须标注不确定")
                        lines.append(f"  C/P成交={sm.get('cp_ratio','?')} ({sm.get('label','')}) 挤压=[{sr.get('urgency')}]{sr.get('type')}")
                        # OI 失衡数据
                        p_oi = ds.get('oi', 0) or 0
                        c_oi = of.get('oi', 0) or 0
                        if p_oi and c_oi:
                            ratio = p_oi / c_oi
                            if ratio >= 3 or ratio <= 0.33:
                                lines.append(f"  ⚠ Put OI/Call OI = {ratio:.1f}倍 严重失衡")
                    # 基本面（若缓存里有 —— 不主动拉，避免拖慢 AI CLI 调用）
                    if fd:
                        latest = fd.get("latest", {})
                        lines.append(
                            f"  基本面(来源 {fd.get('source_stock','?')}): CROIC={latest.get('croic')}% "
                            f"Piotroski={latest.get('piotroski')}/9 借款=${latest.get('fin_debt')}M "
                            f"CCC={latest.get('ccc')}天 · 4 年趋势 CROIC={fd.get('croic')}"
                        )
                    if outlook:
                        ac = outlook.get("analyst_consensus") or {}
                        pricing = outlook.get("pricing") or {}
                        lines.append(
                            f"  前瞻(来源 {outlook.get('source_stock','?')}): "
                            f"分类={pricing.get('label','数据不足')} 可信度={pricing.get('confidence','low')} "
                            f"Forward EPS={ac.get('forward_eps')} 增长={ac.get('eps_growth_pct')}% "
                            f"Forward PE={ac.get('forward_pe')}x 目标价差={ac.get('target_upside_pct')}% "
                            f"多锚隐含价差中位={pricing.get('median_gap_pct')}% "
                            f"修正={pricing.get('revision_signal','unavailable')}"
                        )
                    lines.append("")

                prompt = (
                    "你是量化交易分析师。对下面每个标的用 3 行中文输出（无 preamble、无 markdown ```围栏），"
                    "格式严格如下：\n\n"
                    "### <TICKER>\n"
                    "- 综合: <多空信号+期权是否共振，1 行>\n"
                    "- 攻防: <关键防守/进攻位可操作性 + 到 spot 距离，1 行>\n"
                    "- 警示: <异常/机会/风险，无则写 暂无>\n\n"
                    "解读规则（严格遵循）：\n"
                    "1. Put OI 远大于 Call OI 时区分：ATM 附近=真恐慌 / OTM 远端=廉价保险；机构对冲(用户持仓可放心) vs 空头押跌\n"
                    "2. Call OI 远大于 Put OI 时：上方是 pin 磁吸位 / 下方无护盘\n"
                    "3. gamma_up 挤压[high] = 突破 call wall 加速上涨；put_break 挤压[high] = 跌破 put wall 加速下跌\n"
                    "4. 信号 conf ≥ 4 且期权攻防共振 → 明确机会；信号看多但 put OI 大幅堆积 → 警示 divergence\n"
                    "5. 若基本面存在 → 综合行里带一句：CROIC 趋势方向 + Piotroski 强/弱 + 借款激增警示\n"
                    "6. 若前瞻存在 → 综合行必须写预期定价分类；警示行说明 EPS 修正与估值是否背离。代理 ETF 只代表底层公司，不把公司目标价当 ETF 目标价\n"
                    "7. 严格只输出 ### 段落，不要另写总结、泛泛展望或重复问题\n\n"
                    "8. TQQQ/SOXL 的 QQQ/SOXX strike 只是信号源；写买入、支撑、阻力时必须优先写 => 后的杠杆 ETF 即时换算价，禁止把底层 strike 当成挂单价；若 => 换算缺失，只能写等待换算，不能给数字挂单价\n"
                    "9. 到期路径估价已含历史平均折损但仍不精确；期限较长时必须提示每日重置/路径依赖，并以触发时重新换算为准\n\n"
                    "标的数据：\n"
                    + "\n".join(lines)
                )
                out, status, provider, _ = query_ai_cli(prompt, timeout=120)
                if out and out.strip():
                    import re
                    parts = re.split(r"^###\s+([A-Z]{2,6})\b", out, flags=re.MULTILINE)
                    tickers = {}
                    for i in range(1, len(parts), 2):
                        tk = parts[i].strip().upper()
                        body = parts[i + 1] if i + 1 < len(parts) else ""
                        body = body.strip()
                        if tk and body:
                            tickers[tk] = body[:1500]
                    if tickers:
                        result = {
                            "tickers": tickers,
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "provider": provider,
                        }
                        cache_path.write_text(
                            json.dumps(result, ensure_ascii=False), encoding="utf-8"
                        )
                # else: silent fail, state stays generating; next refresh will retry
            except Exception:
                pass
            finally:
                _refresh_flag[flag_key] = False
        threading.Thread(target=_bg, daemon=True).start()

    return {"tickers": {}, "state": "generating"}


def api_ticker_ai() -> dict:
    """每标的 AI 分析。优先 live（覆盖所有 tracked ticker），fallback 到 snapshot。"""
    signals   = api_signals().get("tickers", {}) or {}
    opt_data  = api_ticker_options().get("tickers", {}) or {}
    live      = _ticker_ai_live_all(signals, opt_data)
    snapshot  = _ticker_ai_snapshot_parse()

    tickers: dict[str, str] = {}
    sources: dict[str, str] = {}
    # 优先 live
    for tk, body in (live.get("tickers") or {}).items():
        if not ticker_display_profile(tk)["show_ai_analysis"]:
            continue
        tickers[tk] = body
        sources[tk] = "live"
    # 补 snapshot（未被 live 覆盖的）
    for tk, body in (snapshot.get("tickers") or {}).items():
        if not ticker_display_profile(tk)["show_ai_analysis"]:
            continue
        if tk not in tickers:
            tickers[tk] = body
            sources[tk] = "snapshot"

    return {
        "exists":         bool(tickers) or live.get("state") == "generating",
        "tickers":        tickers,
        "sources":        sources,
        "live_state":     live.get("state", "empty"),  # cached / generating / empty
        "live_generated": live.get("generated_at"),
        "snapshot_path":  snapshot.get("path"),
        "snapshot_mtime": snapshot.get("mtime"),
    }


TICKER_TO_OPTION_SOURCE = {
    "TQQQ": "QQQ",   # 3x QQQ → 用 QQQ chain（同指数、更深）
    "SOXL": "SOXX",  # 3x SOX (ICE 半导体指数) → 1x 版本 SOXX，同指数最精准
    "DRAM": "DRAM",  # Roundhill Memory ETF 自身有 chain（贴近实际持仓）
    "MULL": "MU",    # 2x MU（Direxion）单股杠杆，用 MU 本体
    "GLD":  "GLD",
    "TSLA": "TSLA",
    "KLAC": "KLAC",
    "AMAT": "AMAT",
    "GOOGL": "GOOGL",
    "NVDA": "NVDA",
    "MSFT": "MSFT",
    "AAPL": "AAPL",
    # 2026-Q3 thesis
    "NBIS": "NBIS",
    "SHY":  "SHY",   # 1-3Y Treasury ETF
    "IEI":  "IEI",   # 3-7Y Treasury ETF
    "LITE": "LITE",  # Lumentum 有 options chain
    "CBRS": "CBRS",  # Cerebras — 14 个到期日 (2026-08-21 起)
    "USO":  "USO",   # WTI 原油 ETF — 17 个到期日 (2026-08-19 起)
    "XLV":  "XLV",   # 医疗行业 ETF — 12 个到期日 · 流动性充足
}

# 这些卡片用流动性更深的 1x ETF 期权链判断结构，但所有可执行价位必须
# 映射回用户实际交易的杠杆 ETF。映射以同一时点的两只 ETF 现价为锚；
# 到期估值另用历史日收益残差估算波动折损/跟踪偏差，并明确给出误差带。
LEVERAGED_OPTION_PRICE_MAP = {
    "TQQQ": {"source": "QQQ", "leverage": 3.0},
    "SOXL": {"source": "SOXX", "leverage": 3.0},
}

TICKER_OPTIONS_CACHE_SCHEMA = 6


def _series_by_day(series) -> dict[str, float]:
    """把 pandas Series / dict 归一成 YYYY-MM-DD -> 正价格，方便无 pandas 单测。"""
    if series is None:
        return {}
    try:
        items = series.items()
    except AttributeError:
        return {}
    result: dict[str, float] = {}
    for key, value in items:
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        day = key.date().isoformat() if hasattr(key, "date") else str(key)[:10]
        result[day] = price
    return result


def _estimate_leveraged_decay(proxy_closes, leveraged_closes,
                               leverage: float) -> dict:
    """估算杠杆 ETF 相对 `leverage × 底层日收益` 的历史折损。

    combined residual 同时包含波动路径折损与基金跟踪成本；tracking residual
    只比较基金实际日收益和理论日重置收益。到期换算使用 combined residual，
    因为只知道终点 strike，并不知道中间路径。
    """
    proxy = _series_by_day(proxy_closes)
    lev = _series_by_day(leveraged_closes)
    days = sorted(set(proxy) & set(lev))
    combined: list[float] = []
    tracking: list[float] = []
    for prev_day, day in zip(days, days[1:]):
        p0, p1 = proxy[prev_day], proxy[day]
        l0, l1 = lev[prev_day], lev[day]
        proxy_ratio = p1 / p0
        lev_ratio = l1 / l0
        theoretical_ratio = 1.0 + leverage * (proxy_ratio - 1.0)
        if proxy_ratio <= 0 or lev_ratio <= 0 or theoretical_ratio <= 0:
            continue
        combined.append(math.log(lev_ratio) - leverage * math.log(proxy_ratio))
        tracking.append(math.log(lev_ratio) - math.log(theoretical_ratio))

    if len(combined) < 10:
        return {
            "available": False,
            "sample_days": len(combined),
            "quality": "insufficient",
        }

    mean_combined = statistics.fmean(combined)
    mean_tracking = statistics.fmean(tracking)
    residual_std = statistics.stdev(combined) if len(combined) > 1 else 0.0
    annualized = math.exp(mean_combined * 252) - 1.0
    return {
        "available": True,
        "sample_days": len(combined),
        "quality": "good" if len(combined) >= 40 else "limited",
        "combined_residual_log_daily": mean_combined,
        "tracking_residual_log_daily": mean_tracking,
        "daily_carry_pct": round((math.exp(mean_combined) - 1.0) * 100, 4),
        "daily_decay_pct": round(max(0.0, 1.0 - math.exp(mean_combined)) * 100, 4),
        "tracking_daily_pct": round((math.exp(mean_tracking) - 1.0) * 100, 4),
        "annualized_carry_pct": round(annualized * 100, 2),
        "residual_std_daily_pct": round(residual_std * 100, 4),
    }


def _convert_proxy_level(proxy_level: float | None, proxy_spot: float | None,
                         leveraged_spot: float | None, leverage: float,
                         calendar_days: int = 0,
                         decay: dict | None = None) -> dict | None:
    """同时返回即时 3x 锚定价与包含历史折损的到期估值/误差带。"""
    try:
        source_level = float(proxy_level)
        source_spot = float(proxy_spot)
        target_spot = float(leveraged_spot)
        multiple = float(leverage)
    except (TypeError, ValueError):
        return None
    if min(source_level, source_spot, target_spot, multiple) <= 0:
        return None

    proxy_move = source_level / source_spot - 1.0
    instant_factor = 1.0 + multiple * proxy_move
    if instant_factor <= 0:
        return None
    instant_level = target_spot * instant_factor
    result = {
        "source_level": round(source_level, 2),
        "source_move_pct": round(proxy_move * 100, 2),
        "spot_anchored_level": round(instant_level, 2),
        "spot_anchored_move_pct": round((instant_factor - 1.0) * 100, 2),
        "calendar_days": max(0, int(calendar_days or 0)),
        "method": "current_spot_daily_reset",
    }

    # 历史 residual 是按交易日估计，不能直接乘含周末的日历天数。
    trading_days = max(0, round(result["calendar_days"] * 252 / 365))
    result["estimated_trading_days"] = trading_days
    if trading_days <= 0 or not decay or not decay.get("available"):
        return result

    mean_residual = float(decay.get("combined_residual_log_daily") or 0.0)
    residual_std = float(decay.get("residual_std_daily_pct") or 0.0) / 100.0
    expiry_level = target_spot * math.exp(
        multiple * math.log(source_level / source_spot)
        + mean_residual * trading_days
    )
    one_sigma = residual_std * math.sqrt(trading_days)
    result.update({
        "expiry_estimate": round(expiry_level, 2),
        "expiry_range_low": round(expiry_level * math.exp(-one_sigma), 2),
        "expiry_range_high": round(expiry_level * math.exp(one_sigma), 2),
        "expiry_method": "historical_path_decay",
    })
    return result


def _build_leveraged_option_mapping(ticker: str, source: str,
                                     proxy_spot: float | None,
                                     leveraged_spot: float | None,
                                     expiry: str | None,
                                     decay: dict | None,
                                     levels: dict[str, float | None]) -> dict | None:
    cfg = LEVERAGED_OPTION_PRICE_MAP.get(ticker)
    if not cfg or cfg.get("source") != source:
        return None
    try:
        expiry_day = datetime.strptime(expiry, "%Y-%m-%d").date() if expiry else None
        calendar_days = max(0, (expiry_day - datetime.now().date()).days) if expiry_day else 0
    except (TypeError, ValueError):
        calendar_days = 0
    converted = {
        key: _convert_proxy_level(value, proxy_spot, leveraged_spot,
                                  cfg["leverage"], calendar_days, decay)
        for key, value in levels.items()
    }
    converted = {key: value for key, value in converted.items() if value}
    if not converted:
        return None
    return {
        "ticker": ticker,
        "source": source,
        "leverage": cfg["leverage"],
        "proxy_spot": round(float(proxy_spot), 2),
        "leveraged_spot": round(float(leveraged_spot), 2),
        "expiry": expiry,
        "calendar_days": calendar_days,
        "decay": decay or {"available": False, "quality": "unavailable"},
        "levels": converted,
        "execution_rule": (
            f"{source} 只负责触发结构信号；{ticker} 挂单必须使用 spot_anchored_level，"
            "并在触发时按最新两只 ETF 现价重新换算"
        ),
        "long_horizon_warning": (
            "杠杆 ETF 每日重置且路径依赖；expiry_estimate 已加入历史平均折损，"
            "但不是可直接挂单的精确价格"
            if calendar_days >= 2 else None
        ),
    }


def _build_actionable_option_prices(ticker: str, source: str,
                                    mapping: dict | None) -> dict | None:
    """把 proxy 原价和杠杆 ETF 执行价绑定在同一个 level 对象里。

    这是给 API 消费者使用的无歧义契约。旧 top-level `spot`、
    `call_wall_strike` 等字段仍属于原始期权链，不能替换，否则 OI、保费、
    名义敞口和 SVG 分布会被混到另一价格空间。
    """
    if not isinstance(mapping, dict):
        return None
    levels = mapping.get("levels")
    if not isinstance(levels, dict):
        return None
    basis_by_level = {
        "call_wall": "open_interest",
        "put_wall": "open_interest",
        "call_wall_oi": "open_interest",
        "put_wall_oi": "open_interest",
        "call_wall_volume": "volume",
        "put_wall_volume": "volume",
        "upper_resistance": "open_interest",
        "lower_support": "open_interest",
        "pin": "gamma_flip",
        "max_pain": "volume_derived_max_pain",
    }
    actionable_levels = {}
    for name, basis in basis_by_level.items():
        mapped = levels.get(name)
        if not isinstance(mapped, dict) or mapped.get("source_level") is None:
            continue
        target = {
            "ticker": ticker,
            "spot_anchored": mapped.get("spot_anchored_level"),
            "move_pct": mapped.get("spot_anchored_move_pct"),
            "expiry_estimate": mapped.get("expiry_estimate"),
            "expiry_range_low": mapped.get("expiry_range_low"),
            "expiry_range_high": mapped.get("expiry_range_high"),
        }
        actionable_levels[name] = {
            "basis": (mapping.get("level_basis") or {}).get(name, basis),
            "source": {
                "ticker": source,
                "strike": mapped.get("source_level"),
                "move_pct": mapped.get("source_move_pct"),
            },
            "target": target,
        }
    if not actionable_levels:
        return None
    return {
        "contract_version": 2,
        "ticker": ticker,
        "spot": mapping.get("leveraged_spot"),
        "source_ticker": source,
        "source_spot": mapping.get("proxy_spot"),
        "leverage": mapping.get("leverage"),
        "expiry": mapping.get("expiry"),
        "levels": actionable_levels,
        "execution_price_path": "levels.<name>.target.spot_anchored",
        "warning": (
            f"{source} strike 只作结构触发；{ticker} 订单必须使用 target.spot_anchored，"
            "并在触发时按最新现价重算"
        ),
    }


def _option_price_context(ticker: str, source: str,
                          actionable: dict | None) -> dict:
    is_proxy = ticker != source
    return {
        "card_ticker": ticker,
        "option_source_ticker": source,
        "is_proxy": is_proxy,
        "top_level_price_space": source,
        "actionable_price_space": ticker,
        "top_level_field_semantics": {
            "spot": f"{source} spot",
            "call_wall_strike": f"{source} max-volume call wall",
            "put_wall_strike": f"{source} max-volume put wall",
            "call_wall_oi_strike": f"{source} max-OI call wall",
            "put_wall_oi_strike": f"{source} max-OI put wall",
            "max_pain": f"{source} volume-derived max pain",
        },
        "consumer_rule": (
            "proxy card: never use top-level source prices for orders; read actionable_prices"
            if is_proxy else "native card: top-level and card ticker share the same price space"
        ),
        "actionable_ready": bool(actionable),
    }


def _enrich_explicit_wall_mappings(payload: dict) -> None:
    """给 schema-5 cache 补出明确的 OI/volume wall 映射，原地更新。"""
    mapping = payload.get("leveraged_mapping")
    if not isinstance(mapping, dict):
        return
    levels = mapping.setdefault("levels", {})
    basis = mapping.setdefault("level_basis", {})
    source_fields = {
        "call_wall_oi": ("call_wall_oi_strike", "open_interest"),
        "put_wall_oi": ("put_wall_oi_strike", "open_interest"),
        "call_wall_volume": ("call_wall_strike", "volume"),
        "put_wall_volume": ("put_wall_strike", "volume"),
    }
    for level_name, (field_name, level_basis) in source_fields.items():
        source_level = payload.get(field_name)
        if source_level is not None and level_name not in levels:
            converted = _convert_proxy_level(
                source_level,
                mapping.get("proxy_spot"),
                mapping.get("leveraged_spot"),
                mapping.get("leverage"),
                mapping.get("calendar_days") or 0,
                mapping.get("decay"),
            )
            if converted:
                levels[level_name] = converted
        if level_name in levels:
            basis[level_name] = level_basis
    basis.setdefault(
        "call_wall",
        "open_interest" if payload.get("call_wall_oi_strike") is not None
        else "volume_fallback",
    )
    basis.setdefault(
        "put_wall",
        "open_interest" if payload.get("put_wall_oi_strike") is not None
        else "volume_fallback",
    )


def _latest_signal_price(ticker: str) -> float | None:
    """网络报价暂缺时，用同一轮 orchestrator 写下的标的现价兜底。"""
    for name in (ticker, f"US.{ticker}"):
        path = SIGNALS_DIR / f"{name}_latest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            price = float((payload.get("market") or {}).get("price"))
            if math.isfinite(price) and price > 0:
                return price
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _visible_option_sources() -> dict[str, str]:
    return {
        ticker: underlying
        for ticker, underlying in TICKER_TO_OPTION_SOURCE.items()
        if ticker_display_profile(ticker)["show_options"]
    }


def _ticker_options_cache_contract() -> str:
    """Fingerprint the schema and currently visible option universe."""
    sources = sorted(
        (ticker, underlying)
        for ticker, underlying in _visible_option_sources().items()
    )
    contract = json.dumps(
        {
            "schema": TICKER_OPTIONS_CACHE_SCHEMA,
            "sources": sources,
            "leveraged_price_map": LEVERAGED_OPTION_PRICE_MAP,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()[:12]


def _ticker_options_cache_valid(data: dict) -> bool:
    return data.get("cache_contract") == _ticker_options_cache_contract()


def _find_wall_bands(dist: list, side: str, spot: float,
                      min_strike_notional: float = 50e6,
                      max_gap_pct: float = 2.5) -> list:
    """把相邻的强 OI strike 合并成 wall band（例 AAPL $300+$295 双墙）。

    ⚠ 关键: 只在有效方向合并（put→spot 下方；call→spot 上方），
       避免把 ITM 保护性 put/call 误合入支撑/阻力带。

    Args:
        dist: distribution list (每项含 strike, call_oi, put_oi)
        side: 'call' 或 'put'
        spot: 现价（用来判定方向 + 算 gap）
        min_strike_notional: 单 strike 视为"强"的名义敞口下限（默认 $50M）
        max_gap_pct: 两个相邻 strong strike 距离 ≤ 现价 X% 视为同一 band
                     (默认 2.5% 即约 3 个连续 strike step)

    Returns list of bands:
        [{low_strike, high_strike, strikes: [...], combined_oi, combined_notional, count}]
    """
    oi_key = f"{side}_oi"
    # put 只看 spot 下方 (含 ATM)；call 只看 spot 上方 (含 ATM)
    # 避免 ITM 保护性 put/call 污染
    if side == "put":
        candidates = [d for d in dist if d["strike"] <= spot]
    else:
        candidates = [d for d in dist if d["strike"] >= spot]
    strong = [d for d in candidates if d[oi_key] * 100 * d["strike"] >= min_strike_notional]
    if not strong:
        return []
    strong.sort(key=lambda x: x["strike"])

    bands: list[list] = []
    current: list = [strong[0]]
    for d in strong[1:]:
        prev_strike = current[-1]["strike"]
        gap_pct = (d["strike"] - prev_strike) / spot * 100
        if gap_pct <= max_gap_pct:
            current.append(d)
        else:
            bands.append(current)
            current = [d]
    bands.append(current)

    return [{
        "low_strike":         min(x["strike"] for x in b),
        "high_strike":        max(x["strike"] for x in b),
        "strikes":            sorted([x["strike"] for x in b]),
        "combined_oi":        sum(x[oi_key] for x in b),
        "combined_notional":  int(sum(x[oi_key] * 100 * x["strike"] for x in b)),
        "count":              len(b),
    } for b in bands]


def _analyze_walls(spot, call_wall, put_wall, max_pain,
                   cw_vol, pw_vol, total_c, total_p,
                   cw_oi=0, pw_oi=0, cw_prem=None, pw_prem=None,
                   put_bands_below=None, call_bands_above=None) -> dict:
    """从 walls + spot 推导 攻/防位、C/P 情绪、挤压风险。

    OI-based walls 优先（有实际持仓才有对冲压力）；vol-based 作 fallback。
    band 支持: 若相邻多 strike 联合 notional 强 → 触发 band_break/gamma_band 挤压。
    """
    r: dict = {}
    # 用 OI 做强度（若无则退回 vol）
    _c_denom = sum([cw_oi, pw_oi]) or (total_c + total_p) or 1
    def _fmt_notional(n):
        if not n: return ""
        if n >= 1e9:  return f"~${n/1e9:.1f}B"
        if n >= 1e6:  return f"~${n/1e6:.0f}M"
        if n >= 1e3:  return f"~${n/1e3:.0f}K"
        return f"~${n:.0f}"

    if put_wall is not None:
        dist_pct = round((spot - put_wall) / spot * 100, 2)  # >0 = 支撑在下方
        share    = (pw_oi / max(pw_oi + cw_oi, 1)) if (pw_oi + cw_oi) else pw_vol / max(total_p, 1)
        strength = "强" if share >= 0.35 else ("中" if share >= 0.18 else "弱")
        r["defense"] = {
            "strike":   put_wall,
            "dist_pct": dist_pct,
            "strength": strength,
            "share":    round(share * 100, 1),
            "oi":       pw_oi,
            "prem":     pw_prem,
            "notional": int(pw_oi * 100 * put_wall) if pw_oi else 0,
        }
    if call_wall is not None:
        dist_pct = round((call_wall - spot) / spot * 100, 2)
        share    = (cw_oi / max(pw_oi + cw_oi, 1)) if (pw_oi + cw_oi) else cw_vol / max(total_c, 1)
        strength = "强" if share >= 0.35 else ("中" if share >= 0.18 else "弱")
        r["offense"] = {
            "strike":   call_wall,
            "dist_pct": dist_pct,
            "strength": strength,
            "share":    round(share * 100, 1),
            "oi":       cw_oi,
            "prem":     cw_prem,
            "notional": int(cw_oi * 100 * call_wall) if cw_oi else 0,
        }

    # 挤压风险 —— OI 达标才算真信号（>500 手，避免小盘噪音）
    risk = {"type": "none", "urgency": "low",
            "detail": "当前价格远离主要期权墙，无明显挤压压力"}
    if (call_wall is not None and 0 <= (call_wall - spot) / spot * 100 <= 1.5
            and (cw_oi >= 500 or not cw_oi)):  # OI 未知时也放行
        pct = (call_wall - spot) / spot * 100
        prem_s = f" · 单手保费 ${cw_prem}" if cw_prem else ""
        oi_s = f" · OI {cw_oi:,} ({_fmt_notional(cw_oi*100*call_wall)} 名义)" if cw_oi else ""
        risk = {"type": "gamma_up", "urgency": "high",
                "detail": f"Call wall ${call_wall} 距现价仅 {pct:.1f}%{oi_s}{prem_s}。突破且放量 → 做市商需买入对冲 → 短期加速上涨（gamma squeeze）"}
    elif (put_wall is not None and 0 <= (spot - put_wall) / spot * 100 <= 1.5
            and (pw_oi >= 500 or not pw_oi)):
        pct = (spot - put_wall) / spot * 100
        prem_s = f" · 单手保费 ${pw_prem}" if pw_prem else ""
        oi_s = f" · OI {pw_oi:,} ({_fmt_notional(pw_oi*100*put_wall)} 名义)" if pw_oi else ""
        risk = {"type": "put_break", "urgency": "high",
                "detail": f"Put wall ${put_wall} 距现价仅 {pct:.1f}%{oi_s}{prem_s}。跌破 = 关键支撑丢失，止损盘 + 做市商反手对冲 → 加速下跌"}
    else:
        # 联合 wall band 检测（例 AAPL $300+$295 = $285M 双墙即使距 spot > 1.5% 也需警示）
        strong_put_band = next((b for b in (put_bands_below or [])
                                if b["combined_notional"] >= 200e6 and b["count"] >= 2
                                and 0 <= (spot - b["high_strike"]) / spot * 100 <= 3.5), None)
        strong_call_band = next((b for b in (call_bands_above or [])
                                 if b["combined_notional"] >= 200e6 and b["count"] >= 2
                                 and 0 <= (b["low_strike"] - spot) / spot * 100 <= 3.5), None)
        if strong_call_band:
            b = strong_call_band
            pct = (b["low_strike"] - spot) / spot * 100
            risk = {"type": "gamma_band_up", "urgency": "medium",
                    "detail": (f"Call 联合带 ${b['low_strike']}-${b['high_strike']} "
                               f"({b['count']} strike, 合计 {_fmt_notional(b['combined_notional'])}) "
                               f"距 spot {pct:.1f}%。价格触及带下沿会分阶段释放对冲压力 → 突破全带需强量")}
        elif strong_put_band:
            b = strong_put_band
            pct = (spot - b["high_strike"]) / spot * 100
            risk = {"type": "put_band_break", "urgency": "medium",
                    "detail": (f"Put 联合带 ${b['low_strike']}-${b['high_strike']} "
                               f"({b['count']} strike, 合计 {_fmt_notional(b['combined_notional'])}) "
                               f"距 spot -{pct:.1f}%。首层 ${b['high_strike']} 触及触发首轮对冲卖压，"
                               f"跌破全带 ${b['low_strike']} = 无缓冲加速下跌")}
        elif max_pain is not None and abs(spot - max_pain) / spot * 100 >= 3:
            pull = "下拉" if spot > max_pain else "上拉"
            pct = abs(spot - max_pain) / spot * 100
            risk = {"type": "max_pain_gravity", "urgency": "medium",
                    "detail": f"Max Pain ${max_pain}（距现价 {pct:.1f}%）。到期日临近，期权卖方倾向将价格{pull}至此"}
    r["squeeze_risk"] = risk
    # C/P 情绪 —— 带小白详细解读
    if total_p > 0:
        cp = total_c / total_p
        if cp >= 1.8:
            label = "看多情绪偏热"
            explain = (
                f"Call 成交是 Put 的 {cp:.1f} 倍。**多头拥挤**：\n"
                "  · 大家在追多 / 抢反弹 / 押突破\n"
                "  · 历史规律：C/P > 1.8 时短期回调概率偏高（反向指标）\n"
                "  · 尤其若配合价格已涨很多 → 警惕获利了结压力"
            )
        elif cp <= 0.5:
            label = "看空情绪极端"
            explain = (
                f"Put 成交是 Call 的 {1/cp:.1f} 倍。**空头拥挤 / 大量对冲**：\n"
                "  · 场景 a) 恐慌下跌中 → 多为空头押跌\n"
                "  · 场景 b) 机构大量买 put 做保险（他们仍持股）→ 中性偏温和多\n"
                "  · 场景 c) 有明确利空事件预期（财报/宏观/政策）\n"
                "  · 历史规律：C/P < 0.5 时短期反弹概率 ~60%（『恐慌见底』反向指标）\n"
                "  · 区分 a vs b：看这些 put 是 ATM（真怕跌）还是远 OTM（廉价 tail hedge）"
            )
        else:
            label = "多空均衡"
            explain = "Call/Put 成交比例接近 1:1，市场无明显方向性偏见。"
        r["sentiment"] = {"cp_ratio": round(cp, 2), "label": label, "explain": explain}
    # 墙 OI 失衡解读（Put OI 远大于 Call OI 或反之）
    if pw_oi > 0 and cw_oi > 0:
        ratio = pw_oi / cw_oi
        wall_note = None
        if ratio >= 3:
            wall_note = (
                f"**Put 墙 OI 是 Call 墙的 {ratio:.1f} 倍**：下方『真金白银』押注远超上方。\n"
                "  · 若你手上有仓位：这是**对冲/保险堆积**的层，跌到这里通常有反弹\n"
                "  · 若空头思路：说明大家已经知道下方风险了 → 你不占信息优势\n"
                "  · 例外：如果 Put 墙很接近 spot（<2%），跌破会引发做市商反手对冲 → **加速下跌**"
            )
        elif ratio <= 0.33:
            wall_note = (
                f"**Call 墙 OI 是 Put 墙的 {1/ratio:.1f} 倍**：上方阻力/追多力量集中，下方对冲少。\n"
                "  · 通常出现在强势股 / 短期热门标的 → 上方是 pin / 磁吸位\n"
                "  · 下方无护盘 → 一旦下跌可能无缓冲、直接找下一档 support"
            )
        r["wall_imbalance"] = wall_note  # 可能是 None
    return r


def api_options_flow() -> dict:
    """Latest small-universe option-flow scan; never performs network I/O."""
    try:
        from option_flow import load_option_flow_signal
        return load_option_flow_signal()
    except Exception as exc:
        return {
            "generated_at": None,
            "events": [],
            "positions": {},
            "status": "unavailable",
            "error": str(exc)[:160],
        }


def api_ticker_options() -> dict:
    """每个 tracked ticker 的期权墙 + 攻防位 + 挤压风险。10min 后台缓存。"""
    # 24h TTL —— 期权 wall/OI 日级变化就够，无必要秒级刷（拖累 yfinance + 间接
    # 让 ticker_ai 的 AI hash 每天最多变一次）
    data = _cached(
        "ticker_options",
        ttl_sec=24 * 3600,
        compute_fn=_compute_ticker_options_incremental,
        first_call_async=True,
        first_call_placeholder={"ts": None, "tickers": {}, "data_source": "pending"},
        cache_validator=_ticker_options_cache_valid,
    )
    # Old cache files can still contain macro assets. Enforce the current
    # capability contract at the API boundary as well as during recomputation.
    if isinstance(data.get("tickers"), dict):
        flow = api_options_flow()
        flow_positions = flow.get("positions") if isinstance(flow, dict) else {}
        if not isinstance(flow_positions, dict):
            flow_positions = {}
        data["tickers"] = {
            tk: value for tk, value in data["tickers"].items()
            if ticker_display_profile(tk)["show_options"]
        }
        # 即使后台仍在刷新旧 cache，也在 API 边界即时补上 v2 价格空间契约，
        # 防止消费者把 proxy top-level strike 和杠杆 ETF 映射价拼错。
        for tk, value in data["tickers"].items():
            if not isinstance(value, dict):
                continue
            source = value.get("underlying") or tk
            _enrich_explicit_wall_mappings(value)
            actionable = _build_actionable_option_prices(
                tk, source, value.get("leveraged_mapping")
            ) or value.get("actionable_prices")
            value["actionable_prices"] = actionable
            value["price_context"] = _option_price_context(tk, source, actionable)
            value["flow"] = flow_positions.get(tk)
        data["flow_meta"] = {
            "generated_at": flow.get("generated_at") if isinstance(flow, dict) else None,
            "status": flow.get("status") if isinstance(flow, dict) else "unavailable",
            "age_sec": flow.get("age_sec") if isinstance(flow, dict) else None,
            "stale": bool(flow.get("stale")) if isinstance(flow, dict) else True,
        }
        data["api_contract_version"] = 2
    return data


def _compute_ticker_options(sources: dict[str, str] | None = None) -> dict:
    """对每个 tracked ticker 拉最近到期日期权链 + 分析。串行 ~15-30s 首次。

    双轨：优先 moomoo openD（更快 + JP 支持），失败降级 yfinance。
    """
    import yfinance as yf
    # 检查 openD 可用性（每次 refresh 只查一次）
    try:
        from moomoo_data import (
            get_option_chain_last_error,
            get_option_chain_via_openD,
            health_check,
        )
        openD_ok = health_check().get("available", False)
    except Exception:
        openD_ok = False
        get_option_chain_via_openD = None
        get_option_chain_last_error = None

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tickers": {},
        "data_source": "yfinance",
        "api_contract_version": 2,
        "cache_schema": TICKER_OPTIONS_CACHE_SCHEMA,
        "cache_contract": _ticker_options_cache_contract(),
    }
    if openD_ok:
        out["data_source"] = "openD + yfinance fallback"

    source_map = TICKER_TO_OPTION_SOURCE if sources is None else sources
    for tk, underlying in source_map.items():
        if not ticker_display_profile(tk)["show_options"]:
            continue
        try:
            t = yf.Ticker(underlying)
            source_used = "yfinance"
            source_wait_sec = 0.0
            opend_fallback_error = None
            # 优先尝试 openD
            openD_chain = None
            if openD_ok and get_option_chain_via_openD:
                openD_chain = get_option_chain_via_openD(underlying)
            if openD_chain:
                exp   = openD_chain["expiry"]
                calls = openD_chain["calls"].copy()
                puts  = openD_chain["puts"].copy()
                source_used = "openD"
                source_wait_sec = float(openD_chain.get("rate_limit_wait_sec", 0) or 0)
            else:
                if get_option_chain_last_error:
                    opend_fallback_error = get_option_chain_last_error(underlying)
                expiries = list(t.options or [])
                if not expiries:
                    out["tickers"][tk] = {"underlying": underlying, "error": "no_option_chain"}
                    continue
                # 跳过 ≤5 天内到期的合约（0DTE/1DTE OI 极小，视觉图像意义不大）
                # 优先选：≥5 天且 ≤40 天的第一个（覆盖 weekly + monthly 有意义窗口）
                from datetime import date as _date, datetime as _dt
                today = _date.today()
                good_expiries = []
                for e in expiries:
                    try:
                        d_obj = _dt.strptime(e, "%Y-%m-%d").date()
                        days = (d_obj - today).days
                        if 5 <= days <= 40:
                            good_expiries.append((e, days))
                    except Exception:
                        continue
                if good_expiries:
                    exp = good_expiries[0][0]   # 第一个有意义的
                else:
                    exp = expiries[0]           # fallback 最近
                ch = t.option_chain(exp)
                calls, puts = ch.calls.copy(), ch.puts.copy()
            try:
                spot = float(t.history(period="1d")["Close"].iloc[-1])
            except Exception:
                spot = float(calls["strike"].median())
            lo, hi = spot * 0.90, spot * 1.10
            calls = calls[(calls["strike"] >= lo) & (calls["strike"] <= hi)]
            puts  = puts [(puts ["strike"] >= lo) & (puts ["strike"] <= hi)]
            calls["volume"]       = calls["volume"].fillna(0)
            puts["volume"]        = puts["volume"].fillna(0)
            if "openInterest" in calls.columns:
                calls["openInterest"] = calls["openInterest"].fillna(0)
                puts["openInterest"]  = puts["openInterest"].fillna(0)

            def _mid(row):
                """bid/ask mid 优先，fallback lastPrice；无值返回 None。"""
                if row.empty:
                    return None
                try:
                    b = float(row["bid"].iloc[0] or 0) if "bid" in row.columns else 0
                    a = float(row["ask"].iloc[0] or 0) if "ask" in row.columns else 0
                    if b > 0 and a > 0:
                        return round((b + a) / 2, 2)
                    lp = row["lastPrice"].iloc[0] if "lastPrice" in row.columns else None
                    return round(float(lp), 2) if lp else None
                except Exception:
                    return None

            all_s = sorted(set(calls["strike"].tolist()) | set(puts["strike"].tolist()))
            dist = []
            for s in all_s:
                cr = calls[calls["strike"] == s]
                pr = puts [puts ["strike"] == s]
                cv = int(cr["volume"].sum())
                pv = int(pr["volume"].sum())
                if cv + pv < 20:
                    continue
                c_oi = int(cr["openInterest"].sum()) if "openInterest" in cr.columns else 0
                p_oi = int(pr["openInterest"].sum()) if "openInterest" in pr.columns else 0
                dist.append({
                    "strike":    float(s),
                    "call_vol":  cv, "put_vol":  pv,
                    "call_oi":   c_oi, "put_oi":  p_oi,
                    "call_prem": _mid(cr),
                    "put_prem":  _mid(pr),
                })
            dist.sort(key=lambda x: -(x["call_vol"] + x["put_vol"]))
            dist = sorted(dist[:15], key=lambda x: x["strike"])
            if not dist:
                out["tickers"][tk] = {"underlying": underlying, "error": "no_liquid_strikes"}
                continue

            # Vol-based walls（图表用的还是 vol，反映日内活跃度）
            call_wall = max(dist, key=lambda x: x["call_vol"])
            put_wall  = max(dist, key=lambda x: x["put_vol"])
            # OI-based walls（分析用的是 OI，反映实际押注/持仓）
            # ⚠ 关键：offense = spot 上方的 call 墙 / defense = spot 下方的 put 墙
            # 否则 ITM 保护性 put OI 会污染 defense 位（如 AAPL spot $308 有 $325 put OI 16K
            # 是机构在 $325 保护股票，不是"下方支撑"）
            call_above = [d for d in dist if d["strike"] >= spot and d["call_oi"] > 0]
            put_below  = [d for d in dist if d["strike"] <= spot and d["put_oi"]  > 0]
            call_wall_oi = (max(call_above, key=lambda x: x["call_oi"]) if call_above
                            else (max(dist, key=lambda x: x["call_oi"]) if any(d["call_oi"] > 0 for d in dist) else None))
            put_wall_oi  = (max(put_below,  key=lambda x: x["put_oi"])  if put_below
                            else (max(dist, key=lambda x: x["put_oi"])  if any(d["put_oi"] > 0 for d in dist) else None))
            # Wall bands: 相邻强 OI strike 合并（例 AAPL $300+$295 = $285M 联合防御）
            put_bands  = _find_wall_bands(dist, "put",  spot)
            call_bands = _find_wall_bands(dist, "call", spot)
            # 只保留 spot 附近方向的 band（下方 put / 上方 call）
            put_bands_below  = [b for b in put_bands  if b["high_strike"] <= spot * 1.02]
            call_bands_above = [b for b in call_bands if b["low_strike"]  >= spot * 0.98]
            total_c = sum(d["call_vol"] for d in dist)
            total_p = sum(d["put_vol"] for d in dist)
            # Max pain
            best_s, best_pain = None, float("inf")
            for s in [d["strike"] for d in dist]:
                loss = 0.0
                for d in dist:
                    if d["strike"] < s: loss += d["call_vol"] * (s - d["strike"])
                    elif d["strike"] > s: loss += d["put_vol"] * (d["strike"] - s)
                if loss < best_pain:
                    best_pain, best_s = loss, s

            # 大单标记（在 wall 计算之前，方便前端拿到 flag）
            # 阈值: OI ≥ 5000 手 OR notional ≥ $30M （wall 级重要）
            for d in dist:
                cn = d["call_oi"] * 100 * d["strike"]
                pn = d["put_oi"]  * 100 * d["strike"]
                d["call_big"] = bool(d["call_oi"] >= 5000 or cn >= 30_000_000)
                d["put_big"]  = bool(d["put_oi"]  >= 5000 or pn >= 30_000_000)
                # 异常成交（vol 相对 OI 明显偏高 = 今日新建仓/大单扫单）
                d["call_unusual_vol"] = bool(d["call_vol"] > 2000 and d["call_oi"] > 0
                                             and d["call_vol"] > 0.5 * d["call_oi"])
                d["put_unusual_vol"]  = bool(d["put_vol"]  > 2000 and d["put_oi"] > 0
                                             and d["put_vol"] > 0.5 * d["put_oi"])

            # 分析优先用 OI 墙（fallback vol）
            an_call = call_wall_oi or call_wall
            an_put  = put_wall_oi  or put_wall
            analysis = _analyze_walls(
                spot=spot,
                call_wall=an_call["strike"], put_wall=an_put["strike"],
                max_pain=best_s,
                cw_vol=an_call["call_vol"], pw_vol=an_put["put_vol"],
                cw_oi=an_call["call_oi"],    pw_oi=an_put["put_oi"],
                cw_prem=an_call["call_prem"], pw_prem=an_put["put_prem"],
                total_c=total_c, total_p=total_p,
                put_bands_below=put_bands_below,
                call_bands_above=call_bands_above,
            )
            if isinstance(analysis.get("offense"), dict):
                analysis["offense"]["basis"] = (
                    "open_interest" if call_wall_oi else "volume_fallback"
                )
                analysis["offense"]["source_ticker"] = underlying
            if isinstance(analysis.get("defense"), dict):
                analysis["defense"]["basis"] = (
                    "open_interest" if put_wall_oi else "volume_fallback"
                )
                analysis["defense"]["source_ticker"] = underlying

            def _wall_meta(w, side):
                """把 wall dict 展平成 strike / prem / oi / notional"""
                if not w: return {}
                oi = w[f"{side}_oi"]
                prem = w[f"{side}_prem"]
                strike = w["strike"]
                notional = oi * 100 * strike  # 名义 = OI × 100 × 现价（用 strike 近似）
                return {
                    f"{side}_wall_strike":   strike,
                    f"{side}_wall_vol":      w[f"{side}_vol"],
                    f"{side}_wall_oi":       oi,
                    f"{side}_wall_prem":     prem,
                    f"{side}_wall_notional": int(notional),
                }

            # 财报上下文：从 option_walls._next_earnings_for 拿到 (股票, 财报日, 天数)
            earnings_meta = {}
            try:
                from option_walls import _TICKER_EARNINGS, _ETF_EARNINGS_LINKS, get_earnings_implied_move
                from datetime import date as _date, datetime as _dt
                today = _date.today()
                # 关联财报：先看是不是单股（直接查），再看是不是 ETF（走 links）
                candidate_stocks = [tk] if tk in _TICKER_EARNINGS else list(_ETF_EARNINGS_LINKS.get(tk, []))
                best = None
                for e_stock in candidate_stocks:
                    for d_str in _TICKER_EARNINGS.get(e_stock, []):
                        try:
                            d_obj = _dt.strptime(d_str, "%Y-%m-%d").date()
                        except Exception:
                            continue
                        days = (d_obj - today).days
                        if 0 <= days <= 60:  # 60 天窗口（比 option_walls 的 30 天宽）
                            if best is None or days < best[2]:
                                best = (e_stock, d_str, days)
                if best:
                    e_stock, e_date, e_days = best
                    earnings_meta = {
                        "earnings_stock":     e_stock,
                        "earnings_date":      e_date,
                        "days_to_earnings":   e_days,
                        "expiry_covers_earnings": exp >= e_date,
                    }
                    # 隐含波动幅度（ATM straddle 反推），只在 ≤ 14 天且是单股时算
                    if e_days <= 14 and tk in ("MU", "NVDA"):
                        try:
                            im = get_earnings_implied_move(tk, earnings_date=e_date)
                            if im and im.get("implied_move_pct"):
                                earnings_meta["implied_move_pct"] = round(im["implied_move_pct"], 2)
                        except Exception:
                            pass
            except Exception:
                pass

            # === GEX + IV Regime + Skew 综合分析（对期权小白输出结论）===
            gex_analysis = None
            try:
                from gex_calc import full_analysis as _gex_full
                from datetime import datetime as _dt, date as _date
                import math as _math
                # DTE
                try:
                    exp_dt = _dt.strptime(exp, "%Y-%m-%d").date()
                    dte = max(1, (exp_dt - _date.today()).days)
                except Exception:
                    dte = 30
                # ⚠ 关键：IV 单位归一
                # yfinance 返 impliedVolatility 是**小数** (0.335 = 33.5%)
                # moomoo openD 返是**百分比** (33.5 = 33.5%)
                # gex_calc 期待小数格式，openD 情况下 ÷100
                calls_gex = calls.copy()
                puts_gex  = puts.copy()
                # 用 max 值判断单位：>3 (即 300%) 一定是百分比格式
                iv_max = max(
                    float(calls_gex["impliedVolatility"].max() or 0) if "impliedVolatility" in calls_gex.columns else 0,
                    float(puts_gex["impliedVolatility"].max() or 0)  if "impliedVolatility" in puts_gex.columns else 0,
                )
                if iv_max > 3.0:
                    calls_gex["impliedVolatility"] = calls_gex["impliedVolatility"] / 100.0
                    puts_gex["impliedVolatility"]  = puts_gex["impliedVolatility"]  / 100.0
                # 60d realized vol (小数格式)
                rv = None
                try:
                    import yfinance as _yf
                    hist = _yf.Ticker(underlying).history(period="60d")["Close"]
                    rets = hist.pct_change().dropna()
                    if len(rets) >= 10:
                        rv = float(rets.std() * _math.sqrt(252))
                except Exception:
                    rv = None
                gex_analysis = _gex_full(
                    calls_gex, puts_gex, spot, dte,
                    realized_vol_60d=rv,
                    next_earnings_days=earnings_meta.get("days_to_earnings"),
                    call_wall_strike=call_wall_oi["strike"] if call_wall_oi else (call_wall["strike"] if call_wall else None),
                    put_wall_strike=put_wall_oi["strike"] if put_wall_oi else (put_wall["strike"] if put_wall else None),
                )
            except Exception as e:
                gex_analysis = {"error": f"gex_calc_fail: {str(e)[:100]}"}

            # === QQQ/SOXX 结构价 -> TQQQ/SOXL 可执行价 ===
            # GEX/墙的底层计算始终保留在原期权链单位；这里只另外生成杠杆 ETF
            # 的即时锚定价和带历史折损的到期估值，避免把 $700 的 QQQ strike
            # 误显示成 TQQQ 挂单价。
            leveraged_mapping = None
            mapping_cfg = LEVERAGED_OPTION_PRICE_MAP.get(tk)
            if mapping_cfg and mapping_cfg.get("source") == underlying:
                proxy_history = None
                leveraged_history = None
                leveraged_spot = None
                try:
                    proxy_history = t.history(period="3mo")
                except Exception:
                    proxy_history = None
                try:
                    leveraged_history = yf.Ticker(tk).history(period="3mo")
                    if leveraged_history is not None and not leveraged_history.empty:
                        leveraged_spot = float(leveraged_history["Close"].iloc[-1])
                except Exception:
                    leveraged_history = None
                if not leveraged_spot:
                    leveraged_spot = _latest_signal_price(tk)

                try:
                    proxy_closes = proxy_history["Close"] if proxy_history is not None else None
                    leveraged_closes = (
                        leveraged_history["Close"] if leveraged_history is not None else None
                    )
                    decay = _estimate_leveraged_decay(
                        proxy_closes, leveraged_closes, mapping_cfg["leverage"]
                    )
                except Exception:
                    decay = {"available": False, "sample_days": 0, "quality": "unavailable"}

                key_prices = (
                    (gex_analysis.get("stock_verdict") or {}).get("key_prices") or {}
                    if isinstance(gex_analysis, dict) else {}
                )
                leveraged_mapping = _build_leveraged_option_mapping(
                    ticker=tk,
                    source=underlying,
                    proxy_spot=spot,
                    leveraged_spot=leveraged_spot,
                    expiry=exp,
                    decay=decay,
                    levels={
                        "upper_resistance": key_prices.get("upper_resistance", an_call["strike"]),
                        "pin": key_prices.get("pin"),
                        "lower_support": key_prices.get("lower_support", an_put["strike"]),
                        # call_wall / put_wall 是决策引擎采用的 OI 墙；同时提供
                        # 明确命名的 OI/volume 两套字段，禁止 API 消费者串线。
                        "call_wall": an_call["strike"],
                        "put_wall": an_put["strike"],
                        "call_wall_oi": call_wall_oi["strike"] if call_wall_oi else None,
                        "put_wall_oi": put_wall_oi["strike"] if put_wall_oi else None,
                        "call_wall_volume": call_wall["strike"],
                        "put_wall_volume": put_wall["strike"],
                        "max_pain": best_s,
                    },
                )
                if leveraged_mapping:
                    call_basis = "open_interest" if call_wall_oi else "volume_fallback"
                    put_basis = "open_interest" if put_wall_oi else "volume_fallback"
                    leveraged_mapping["level_basis"] = {
                        "call_wall": call_basis,
                        "put_wall": put_basis,
                        "call_wall_oi": "open_interest",
                        "put_wall_oi": "open_interest",
                        "call_wall_volume": "volume",
                        "put_wall_volume": "volume",
                        "upper_resistance": call_basis,
                        "lower_support": put_basis,
                        "pin": "gamma_flip",
                        "max_pain": "volume_derived_max_pain",
                    }

            actionable_prices = _build_actionable_option_prices(
                tk, underlying, leveraged_mapping
            )
            price_context = _option_price_context(tk, underlying, actionable_prices)

            out["tickers"][tk] = {
                "underlying":       underlying,
                "spot":             round(spot, 2),
                "expiry":           exp,
                "distribution":     dist,
                # 保留 vol-based walls（SVG 图用）
                **_wall_meta(call_wall, "call"),
                **_wall_meta(put_wall, "put"),
                "call_wall_basis": "volume",
                "put_wall_basis":  "volume",
                # OI-based walls（分析用；若与 vol wall 同一 strike 则相同）
                "call_wall_oi_strike": call_wall_oi["strike"] if call_wall_oi else None,
                "put_wall_oi_strike":  put_wall_oi["strike"]  if put_wall_oi  else None,
                "call_wall_oi_basis": "open_interest",
                "put_wall_oi_basis":  "open_interest",
                # 联合 wall bands（相邻强 OI strike 合并 → 隐性双墙识别）
                "put_bands_below":  put_bands_below,
                "call_bands_above": call_bands_above,
                "max_pain":         best_s,
                "source":           source_used,
                "source_wait_sec":  source_wait_sec,
                "opend_fallback_error": opend_fallback_error,
                # 期权结构综合读 (GEX + IV Regime + Skew + Verdict)
                "gex_analysis":     gex_analysis,
                "leveraged_mapping": leveraged_mapping,
                "actionable_prices": actionable_prices,
                "price_context":    price_context,
                **analysis,
                **earnings_meta,
            }
        except Exception as e:
            out["tickers"][tk] = {"underlying": underlying, "error": str(e)[:100]}
    return out


def _has_valid_gex(ticker_payload: dict) -> bool:
    analysis = ticker_payload.get("gex_analysis")
    return bool(
        isinstance(analysis, dict)
        and not analysis.get("error")
        and isinstance(analysis.get("gex"), dict)
        and isinstance(analysis.get("stock_verdict"), dict)
    )


def _retain_last_known_good_gex(previous: dict, refreshed: dict) -> dict:
    """Keep valid same-expiry GEX when a transient upstream refresh lacks OI.

    Walls/volume and price remain fresh.  Only the GEX block is retained, and
    it is explicitly marked stale so the UI never presents it as current.
    """
    result = copy.deepcopy(refreshed)
    old_tickers = previous.get("tickers") if isinstance(previous, dict) else {}
    new_tickers = result.get("tickers") if isinstance(result, dict) else {}
    if not isinstance(old_tickers, dict) or not isinstance(new_tickers, dict):
        return result

    for ticker, current in new_tickers.items():
        if not isinstance(current, dict):
            continue
        current_analysis = current.get("gex_analysis")
        current_error = (
            current_analysis.get("error")
            if isinstance(current_analysis, dict) else None
        )
        if not current_error:
            continue
        old = old_tickers.get(ticker)
        if not isinstance(old, dict) or not _has_valid_gex(old):
            continue
        if (old.get("underlying") != current.get("underlying")
                or old.get("expiry") != current.get("expiry")):
            continue

        retained = copy.deepcopy(old["gex_analysis"])
        retained.update({
            "stale": True,
            "stale_reason": current_error,
            "stale_as_of": retained.get("stale_as_of") or previous.get("ts"),
        })
        current["gex_analysis"] = retained
        # retained verdict 里的 pin/支撑/阻力也来自旧 GEX；同步重建它们的杠杆
        # 映射，避免 UI 一边显示旧 QQQ pin、一边缺少对应 TQQQ 价格。
        mapping = current.get("leveraged_mapping")
        key_prices = (retained.get("stock_verdict") or {}).get("key_prices") or {}
        if isinstance(mapping, dict) and key_prices:
            levels = mapping.setdefault("levels", {})
            for key in ("upper_resistance", "pin", "lower_support"):
                converted = _convert_proxy_level(
                    key_prices.get(key),
                    mapping.get("proxy_spot"),
                    mapping.get("leveraged_spot"),
                    mapping.get("leverage"),
                    mapping.get("calendar_days") or 0,
                    mapping.get("decay"),
                )
                if converted:
                    levels[key] = converted
        current["gex_data_status"] = "stale_last_known_good"
    return result


def _compute_ticker_options_incremental() -> dict:
    """Refresh new symbols incrementally; refresh the full universe on TTL."""
    cache_path = _WEBUI_CACHE_DIR / "ticker_options.json"
    existing: dict = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    expected = _visible_option_sources()
    existing_schema = int(existing.get("cache_schema", 1) or 1)
    expected_contract = _ticker_options_cache_contract()
    contract_changed = existing.get("cache_contract") != expected_contract
    if (existing and existing_schema == TICKER_OPTIONS_CACHE_SCHEMA
            and contract_changed):
        old_tickers = existing.get("tickers")
        if not isinstance(old_tickers, dict):
            old_tickers = {}
        needs_refresh = {
            ticker: underlying
            for ticker, underlying in expected.items()
            if not isinstance(old_tickers.get(ticker), dict)
            or old_tickers[ticker].get("underlying") != underlying
        }
        patch = _compute_ticker_options(needs_refresh) if needs_refresh else None
        merged = {
            ticker: value
            for ticker, value in old_tickers.items()
            if ticker in expected
        }
        if patch:
            merged.update(patch.get("tickers", {}))
        return {
            **existing,
            "ts": (patch or {}).get("ts") or existing.get("ts"),
            "tickers": merged,
            "cache_schema": TICKER_OPTIONS_CACHE_SCHEMA,
            "cache_contract": expected_contract,
        }

    refreshed = _compute_ticker_options(expected)
    if existing and existing_schema == TICKER_OPTIONS_CACHE_SCHEMA:
        refreshed = _retain_last_known_good_gex(existing, refreshed)
    return refreshed


def api_option_walls_chart() -> dict:
    """三个主要指数（QQQ/SMH/GLD）最近到期日期权成交量分布 + 关键墙。
    重仓 ETF 对应的板块指数：
      · QQQ  ← TQQQ 3x 追踪
      · SMH  ← SOXL 3x / DRAM / MULL 追踪
      · GLD  ← 黄金现货
    15 min 后台缓存。
    """
    return _cached("option_walls_chart", ttl_sec=24 * 3600, compute_fn=_compute_option_walls_chart)


def _compute_option_walls_chart() -> dict:
    import yfinance as yf
    TICKERS = [
        {"key": "QQQ", "zh": "纳斯达克100 QQQ", "note": "TQQQ 3x 追踪"},
        {"key": "SMH", "zh": "半导体 SMH",     "note": "SOXL 3x / DRAM / MULL 追踪"},
        {"key": "GLD", "zh": "黄金 GLD",       "note": "GLD 现货追踪"},
    ]
    out = {"ts": datetime.now(timezone.utc).isoformat(), "boards": []}
    for spec in TICKERS:
        try:
            t = yf.Ticker(spec["key"])
            expiries = list(t.options or [])
            if not expiries:
                out["boards"].append({**spec, "error": "no_option_chain"})
                continue
            # 优先 0DTE / 最近周度
            exp = expiries[0]
            ch = t.option_chain(exp)
            calls, puts = ch.calls.copy(), ch.puts.copy()
            try:
                spot = float(t.history(period="1d")["Close"].iloc[-1])
            except Exception:
                spot = float(calls["strike"].median())
            lo, hi = spot * 0.90, spot * 1.10  # ATM ±10%
            calls = calls[(calls["strike"] >= lo) & (calls["strike"] <= hi)]
            puts  = puts [(puts ["strike"] >= lo) & (puts ["strike"] <= hi)]
            calls["volume"] = calls["volume"].fillna(0)
            puts["volume"]  = puts["volume"].fillna(0)
            # 聚合每个 strike
            all_strikes = sorted(set(calls["strike"].tolist()) | set(puts["strike"].tolist()))
            dist = []
            for s in all_strikes:
                cv = int(calls[calls["strike"] == s]["volume"].sum())
                pv = int(puts [puts ["strike"] == s]["volume"].sum())
                if cv + pv < 30:
                    continue
                dist.append({"strike": float(s), "call_vol": cv, "put_vol": pv})
            # 取活跃度 top 20 后按 strike 升序
            dist.sort(key=lambda x: -(x["call_vol"] + x["put_vol"]))
            dist = sorted(dist[:20], key=lambda x: x["strike"])
            call_wall = max(dist, key=lambda x: x["call_vol"], default=None) if dist else None
            put_wall  = max(dist, key=lambda x: x["put_vol"],  default=None) if dist else None
            # Max pain
            def _max_pain(dist_list):
                if not dist_list: return None
                strikes = [d["strike"] for d in dist_list]
                best_s, best_pain = None, float("inf")
                for s in strikes:
                    total = 0.0
                    for d in dist_list:
                        if d["strike"] < s: total += d["call_vol"] * (s - d["strike"])
                        elif d["strike"] > s: total += d["put_vol"] * (d["strike"] - s)
                    if total < best_pain:
                        best_pain, best_s = total, s
                return best_s
            out["boards"].append({
                **spec,
                "spot":              round(spot, 2),
                "expiry":            exp,
                "distribution":      dist,
                "call_wall_strike":  call_wall["strike"] if call_wall else None,
                "call_wall_vol":     call_wall["call_vol"] if call_wall else None,
                "put_wall_strike":   put_wall["strike"]  if put_wall  else None,
                "put_wall_vol":      put_wall["put_vol"] if put_wall  else None,
                "max_pain":          _max_pain(dist),
                "n_strikes":         len(dist),
            })
        except Exception as e:
            out["boards"].append({**spec, "error": str(e)})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默 http 访问日志

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: Path, status=200):
        if not path.exists():
            self.send_error(404, f"not found: {path.name}")
            return
        body = path.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlparse(self.path)
        path = parts.path
        qs = parse_qs(parts.query)
        n = int(qs.get("n", ["100"])[0])
        days = int(qs.get("days", ["30"])[0])

        try:
            if path == "/" or path == "/dashboard":
                self._html(SCRIPT_DIR / "dashboard.html")
            elif path == "/api/health":
                self._json(api_health())
            elif path == "/api/nav":
                self._json(api_nav(days=days))
            elif path == "/api/positions":
                self._json(api_positions())
            elif path == "/api/institutional":
                self._json(api_institutional())
            elif path == "/api/trades":
                self._json(api_trades(n=n))
            elif path == "/api/log":
                self._json(api_log(n=n))
            elif path == "/api/benchmark":
                self._json(api_benchmark())
            elif path == "/api/hmm":
                self._json(api_hmm())
            elif path == "/api/signals":
                self._json(api_signals())
            elif path == "/api/banners":
                self._json(api_banners())
            elif path == "/api/trump_verify":
                text = qs.get("text", [""])[0]
                self._json(api_trump_verify(text))
            elif path == "/api/ai_analysis":
                self._json(api_ai_analysis())
            elif path == "/api/sectors":
                self._json(api_sectors())
            elif path == "/api/oil":
                self._json(api_oil())
            elif path == "/api/option_walls_chart":
                self._json(api_option_walls_chart())
            elif path == "/api/ticker_options":
                self._json(api_ticker_options())
            elif path == "/api/options_flow":
                self._json(api_options_flow())
            elif path == "/api/ticker_ai":
                self._json(api_ticker_ai())
            elif path == "/api/events":
                self._json(api_events())
            elif path == "/api/supply_chain":
                tk = qs.get("ticker", [""])[0]
                self._json(api_supply_chain(tk))
            elif path == "/api/supply_chain_graph":
                tk = qs.get("ticker", [""])[0]
                dp = int(qs.get("depth", ["2"])[0])
                self._json(api_supply_chain_graph(tk, depth=dp))
            elif path == "/api/fundamentals":
                tk = qs.get("ticker", [""])[0]
                pd = qs.get("period", ["year"])[0]
                self._json(api_fundamentals(tk, period=pd))
            elif path == "/api/equity_outlook":
                tk = qs.get("ticker", [""])[0]
                self._json(api_equity_outlook(tk))
            elif path == "/api/jp_watch":
                self._json(api_jp_watch())
            elif path == "/api/jp_guidance":
                tk = qs.get("ticker", [""])[0]
                self._json(api_jp_guidance(tk))
            elif path == "/api/capital_flow":
                tk = qs.get("ticker", [""])[0]
                self._json(api_capital_flow(tk))
            elif path == "/api/tostnet_hits":
                tk = qs.get("ticker", [""])[0]
                d  = int(qs.get("days", ["10"])[0])
                self._json(api_tostnet_hits(tk, days=d))
            elif path == "/api/ai_targets":
                self._json(api_ai_targets())
            elif path == "/api/bond_monitor":
                self._json(api_bond_monitor())
            elif path == "/api/bond_ai_interpret":
                self._json(api_bond_ai_interpret())
            elif path == "/api/fed_watch":
                self._json(api_fed_watch())
            else:
                self.send_error(404, f"route not found: {path}")
        except Exception as e:
            self._json({"error": str(e)}, status=500)


def main():
    print(f"WebUI 启动于 http://{HOST}:{PORT}")
    print(f"仅本机可访问（HOST=127.0.0.1）。Ctrl+C 停止。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWebUI 停止")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
