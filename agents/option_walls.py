"""
OptionWalls — 期权墙分析（市场对哪些价位有心理预期）。

数据源：yfinance（OI 字段当前不可用，改用今日 Volume 作为"机构态度"代理）。
结构上预留 OI 接口——哪天 yfinance 修了或换数据源，直接切。

关键指标（all volume-weighted）：
  · Call Wall  : 今日 call 成交量最大的执行价 → 上方阻力 / 多头共识
  · Put Wall   : 今日 put 成交量最大的执行价 → 下方支撑 / 空头/对冲共识
  · Max Pain   : 让总持仓亏损最小的执行价 → 到期日"磁吸"价
  · C/P Ratio  : 总 call vol / 总 put vol → 当日多空倾向

每日 EOD 缓存到 signals/option_walls_<TICKER>_<DATE>.json (TTL 1 小时)。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from config import SIGNALS_DIR, TICKERS as _CFG_TICKERS


CACHE_TTL_SEC = 3600
# 从 config.TICKERS 派生 (去 "US." 前缀), + GLD (hedge) + SPY/QQQ (market-wide gamma)
# 若 config.TICKERS 变动, option_walls 自动跟进; SPY/QQQ 固定作 market benchmark
DEFAULT_TICKERS = [t.replace("US.", "") for t in _CFG_TICKERS] + ["GLD", "SPY", "QQQ"]

# Leveraged ETF option walls are useful, but the deeper gamma pool often sits
# in the unlevered proxy. Convert proxy wall levels back to the leveraged ETF's
# current price scale with a local daily-reset approximation:
#   leveraged_level ~= leveraged_spot * (1 + leverage * proxy_pct_move)
UNDERLYING_WALL_PROXIES = {
    "TQQQ": [{"ticker": "QQQ", "leverage": 3.0, "label": "QQQ underlying"}],
    "SOXL": [
        {"ticker": "SOXX", "leverage": 3.0, "label": "SOXX underlying"},
        {"ticker": "SMH", "leverage": 3.0, "label": "SMH liquid proxy"},
    ],
    "DRAM": [{"ticker": "MU", "leverage": 1.0, "label": "MU memory proxy"}],
    "MULL": [{"ticker": "MU", "leverage": 2.0, "label": "MU underlying"}],  # 2x MU
}

# 关注的单股财报日历（影响相关 ETF/杠杆 ETF），按 ticker 列出
# 财报日是 ticker-specific 的，不放进 EQUITY_CALENDAR（不影响其它 ETF 决策）
_TICKER_EARNINGS = {
    "MU": [   # Micron 财季（Q1=12月底/Q2=3月底/Q3=6月底/Q4=9月底，财报次月公布）
        "2026-03-19", "2026-06-25", "2026-09-24", "2026-12-17",
    ],
    "NVDA": [
        "2026-05-20", "2026-08-26", "2026-11-18", "2027-02-25",
    ],
}
# 哪些 ETF 受这些单股财报影响（用于 banner 提示）
_ETF_EARNINGS_LINKS = {
    "MULL": ["MU"],   # MULL = 2x MU
    "DRAM": ["MU"],   # MU 是 DRAM 板块龙头
    "SOXL": ["NVDA"], # SOXL 重仓 NVDA
    "TQQQ": ["NVDA"], # TQQQ 也含 NVDA
}


def _next_earnings_for(ticker: str) -> tuple[str, str, int] | None:
    """返回 (related_stock, earnings_date, days_to) 或 None"""
    from datetime import date as _date
    today = _date.today()
    best = None
    for stock in _ETF_EARNINGS_LINKS.get(ticker, []):
        for d_str in _TICKER_EARNINGS.get(stock, []):
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            days = (d - today).days
            if 0 <= days <= 30:  # 30 天内
                if best is None or days < best[2]:
                    best = (stock, d_str, days)
    return best

# 过滤条件
ATM_RANGE_PCT = 0.20   # ATM ±20% 内的 strike 才算
MIN_VOLUME    = 50     # 单 strike 成交量低于 50 视为噪音


# ── 三巫日识别（每季度第三周周五，3/6/9/12 月）────────────────────────────
def is_witching_period(date_obj: datetime | None = None) -> dict:
    """识别三巫日（每季度 3/6/9/12 月第三个周五）及附近 gamma 影响期。

    **始终按 ET (美东) 日期判定** — 三巫日是美股期权到期日，JST 已 6-19 时 ET
    可能还是 6-18 盘前/盘中，必须按 ET 日历判断。

    返回:
      {
        "is_witching_day": bool,        # 今日就是三巫日
        "days_to_witching": int,        # 距下一个三巫日的天数（0 = 今日，<0 = 已过去）
        "phase": "today"|"adjacent"|"approaching"|"far",
        "next_witching_date": "YYYY-MM-DD",
      }

    phases:
      · today      : 当日（0 天）— 最高 gamma 风险
      · adjacent   : 前 1-2 天 — gamma 已开始累积
      · approaching: 前 3-5 天 — 关注
      · far        : ≥ 6 天 — 不影响
    """
    if date_obj is None:
        # 始终用 ET 日期（美股三巫日是 ET 日历）
        from zoneinfo import ZoneInfo
        date_obj = datetime.now(ZoneInfo("America/New_York"))
    today = date_obj.date()
    # 找今年内所有三巫日（每季 3/6/9/12 月第三个周五）
    from datetime import date as _date, timedelta as _td
    year = today.year
    witching_days = []
    for month in (3, 6, 9, 12):
        first = _date(year, month, 1)
        # 第一个周五
        first_friday = first + _td(days=(4 - first.weekday()) % 7)
        # 第三个周五
        third_friday = first_friday + _td(weeks=2)
        witching_days.append(third_friday)
    # 加上一年12月（处理跨年）
    witching_days.append(_date(year + 1, 3, 1) + _td(days=(4 - _date(year + 1, 3, 1).weekday()) % 7) + _td(weeks=2))

    # 找最近的一个（可能是今日、未来、或最近的过去）
    future = [d for d in witching_days if d >= today]
    if future:
        next_w = future[0]
        days_to = (next_w - today).days
    else:
        next_w = witching_days[-1]
        days_to = (next_w - today).days

    if days_to == 0:                phase = "today"
    elif 1 <= days_to <= 2:         phase = "adjacent"
    elif 3 <= days_to <= 5:         phase = "approaching"
    else:                            phase = "far"

    return {
        "is_witching_day": days_to == 0,
        "days_to_witching": days_to,
        "phase": phase,
        "next_witching_date": next_w.isoformat(),
    }


def _calc_gex_proxy(walls: list) -> dict:
    """简化 GEX 代理：用末日 + 周度的 C/P OI ratio 算 dealer 净 gamma 倾向。

    GEX 正 (call dominated) → dealer 短 gamma 在上方 → 价格被压制（pin to max pain）
    GEX 负 (put dominated)  → dealer 长 gamma 在下方 → 更易突破（squeeze 风险）

    返回:
      {
        "direction": "positive_pin"|"negative_squeeze"|"neutral",
        "cp_oi_ratio": float,
        "weighted_max_pain_pct": float,  # 距 spot 的加权 max pain（gamma 磁吸位置）
        "strength": "weak"|"medium"|"strong"
      }
    """
    # 只看 0-7 天到期（gamma 集中区）
    near_walls = [w for w in walls if w.get("days", 99) <= 7
                   and w.get("call_wall") and w.get("put_wall")]
    if not near_walls:
        return {"direction": "neutral", "cp_oi_ratio": 1.0,
                "weighted_max_pain_pct": 0, "strength": "weak"}

    total_call = sum(w.get("total_call_vol", 0) for w in near_walls)
    total_put = sum(w.get("total_put_vol", 0) for w in near_walls)
    cp_ratio = total_call / total_put if total_put > 0 else 1.0

    # max pain 加权平均（按 vol 加权）
    mp_sum = 0
    weight_sum = 0
    for w in near_walls:
        mp_pct = w.get("max_pain_pct")
        if mp_pct is None: continue
        weight = w.get("total_call_vol", 0) + w.get("total_put_vol", 0)
        mp_sum += mp_pct * weight
        weight_sum += weight
    weighted_mp_pct = (mp_sum / weight_sum) if weight_sum > 0 else 0

    # 方向判定
    if cp_ratio >= 1.5:
        direction = "positive_pin"
        strength = "strong" if cp_ratio >= 2.5 else "medium"
    elif cp_ratio <= 0.7:
        direction = "negative_squeeze"
        strength = "strong" if cp_ratio <= 0.4 else "medium"
    else:
        direction = "neutral"
        strength = "weak"

    return {
        "direction": direction,
        "cp_oi_ratio": round(cp_ratio, 2),
        "weighted_max_pain_pct": round(weighted_mp_pct, 2),
        "strength": strength,
    }


def _cache_path(ticker: str, date_str: str) -> Path:
    return Path(SIGNALS_DIR) / f"option_walls_{ticker}_{date_str}.json"


def _is_monthly(expiry_str: str) -> bool:
    """判断是否第 3 个周五月度合约（机构主战场）。"""
    try:
        d = datetime.strptime(expiry_str, "%Y-%m-%d")
        return d.weekday() == 4 and 15 <= d.day <= 21
    except ValueError:
        return False


def _calc_max_pain(calls, puts) -> float | None:
    """
    Volume-weighted Max Pain：对每个候选 strike S，算出"如果到期日价格落在 S"时
    所有期权持有者的总损失，返回最小损失对应的 strike（= 期权卖方利益最大化点
    = 价格"磁吸"目标）。
    """
    strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
    if not strikes:
        return None
    best_strike, best_pain = None, float("inf")
    # 预提速：转 list
    call_rows = [(float(r["strike"]), float(r.get("volume") or 0)) for _, r in calls.iterrows()]
    put_rows  = [(float(r["strike"]), float(r.get("volume") or 0)) for _, r in puts.iterrows()]
    for s in strikes:
        call_pain = sum(max(s - k, 0) * v for k, v in call_rows)
        put_pain  = sum(max(k - s, 0) * v for k, v in put_rows)
        total = call_pain + put_pain
        if total < best_pain:
            best_pain, best_strike = total, s
    return best_strike


def _categorize_expiry(exp_str: str) -> tuple[str, int]:
    """返回 (类别, 距今天数)。类别: 0DTE / weekly / monthly / other。"""
    try:
        d = datetime.strptime(exp_str, "%Y-%m-%d").date()
        days = (d - datetime.now().date()).days
    except ValueError:
        return ("other", 99)
    if days <= 1:   return ("0DTE",    days)
    if days <= 7:   return ("weekly",  days)
    if _is_monthly(exp_str): return ("monthly", days)
    return ("other", days)


def _select_expiries(all_expiries: list[str]) -> list[tuple[str, str, int]]:
    """
    选择策略：
      · 优先末日（0-1 天）+ 本周末（2-7 天）—— gamma 集中，对今晚明天最有影响
      · 再选 1 个近月（2-8 周内的月度合约）—— 中期共识参考
      · 最多返回 3 个合约
    返回 [(expiry, category, days), ...]，按 days 升序
    """
    bucketed = [(e, *_categorize_expiry(e)) for e in all_expiries]
    short_term = [b for b in bucketed if b[1] in ("0DTE", "weekly")][:2]
    monthly    = [b for b in bucketed if b[1] == "monthly" and b[2] <= 60][:1]
    chosen = short_term + monthly
    chosen.sort(key=lambda x: x[2])
    return chosen


def find_walls(ticker: str, max_expiries: int = 3) -> dict:
    """对单个标的算期权墙。返回 dict 含 spot / walls 列表 / error。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cache = _cache_path(ticker, today)
    if cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < CACHE_TTL_SEC:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    result: dict = {"ticker": ticker, "ts": datetime.now().isoformat()}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # 拿现价
        try:
            spot = float(t.info.get("regularMarketPrice"))
        except (TypeError, ValueError):
            hist = t.history(period="1d")
            spot = float(hist["Close"].iloc[-1]) if not hist.empty else None
        if not spot or spot <= 0:
            result["error"] = "no_spot_price"
            _save_cache(cache, result)
            return result
        result["spot"] = round(spot, 2)

        all_expiries = t.options or []
        if not all_expiries:
            result["error"] = "no_option_chain"
            _save_cache(cache, result)
            return result

        # 优先末日 + 周度（gamma 集中），加 1 个近月做对照
        chosen_with_meta = _select_expiries(all_expiries)
        walls = []
        for exp, category, days in chosen_with_meta:
            try:
                ch = t.option_chain(exp)
                calls, puts = ch.calls.copy(), ch.puts.copy()
                lo, hi = spot * (1 - ATM_RANGE_PCT), spot * (1 + ATM_RANGE_PCT)
                calls = calls[(calls["strike"] >= lo) & (calls["strike"] <= hi)]
                puts  = puts [(puts["strike"]  >= lo) & (puts["strike"]  <= hi)]
                calls["volume"] = calls["volume"].fillna(0)
                puts["volume"]  = puts["volume"].fillna(0)
                # 噪音过滤
                calls = calls[calls["volume"] >= MIN_VOLUME]
                puts  = puts [puts["volume"]  >= MIN_VOLUME]

                if calls.empty and puts.empty:
                    walls.append({"expiry": exp, "category": category, "days": days,
                                  "note": "no liquid strikes in ATM ±20%"})
                    continue

                cw = pw = None
                if not calls.empty:
                    top_c = calls.nlargest(1, "volume").iloc[0]
                    cw = {
                        "strike":         float(top_c["strike"]),
                        "volume":         int(top_c["volume"]),
                        "pct_from_spot":  round((float(top_c["strike"]) - spot) / spot * 100, 2),
                    }
                if not puts.empty:
                    top_p = puts.nlargest(1, "volume").iloc[0]
                    pw = {
                        "strike":         float(top_p["strike"]),
                        "volume":         int(top_p["volume"]),
                        "pct_from_spot":  round((float(top_p["strike"]) - spot) / spot * 100, 2),
                    }
                mp = _calc_max_pain(calls, puts)
                total_c = int(calls["volume"].sum())
                total_p = int(puts["volume"].sum())
                walls.append({
                    "expiry":         exp,
                    "category":       category,
                    "days":           days,
                    "is_monthly":     category == "monthly",
                    "call_wall":      cw,
                    "put_wall":       pw,
                    "max_pain":       round(mp, 2) if mp else None,
                    "max_pain_pct":   round((mp - spot) / spot * 100, 2) if mp else None,
                    "total_call_vol": total_c,
                    "total_put_vol":  total_p,
                    "cp_ratio":       round(total_c / total_p, 2) if total_p > 0 else None,
                })
            except Exception as e:
                walls.append({"expiry": exp, "error": f"chain fetch: {e}"})

        result["walls"] = walls
    except Exception as e:
        result["error"] = str(e)

    _save_cache(cache, result)
    return result


# ── 财报隐含波动 (implied move from ATM straddle) ──────────────────────────
def get_earnings_implied_move(stock: str, earnings_date: str | None = None,
                              force: bool = False) -> dict:
    """读 stock 财报当周 ATM straddle，算市场对财报的隐含 ±%。

    用途：让 DRAM/MULL 在 MU 财报前根据期权市场预期单日 move 来决定是否屏蔽。

    Args:
        stock: 个股 ticker (例: "MU")
        earnings_date: 财报日 YYYY-MM-DD；不传则用 _TICKER_EARNINGS 里最近一个
        force: 跳过缓存
    Returns:
        {
          stock, earnings_date, days_to_earnings,
          expiry,               # 实际用的期权到期日（财报日 ≤ expiry）
          spot, atm_strike,
          atm_call_mid, atm_put_mid,
          straddle_price,       # ATM call + ATM put
          implied_move_pct,     # straddle / spot * 100
          atm_iv_call, atm_iv_put,
          cp_volume_ratio,      # 财报到期日 ATM±5% 的 call vol / put vol
          smoothed,             # 是否用 ATM±1 strike 平均（更稳）
        }
    """
    from datetime import date as _date, timedelta as _td

    # 财报日：未传则查表取最近一个（30 天内）
    if earnings_date is None:
        today = _date.today()
        best = None
        for d_str in _TICKER_EARNINGS.get(stock, []):
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            days = (d - today).days
            if 0 <= days <= 30 and (best is None or days < best[1]):
                best = (d_str, days)
        if best is None:
            return {"stock": stock, "error": "no_upcoming_earnings"}
        earnings_date, days_to = best
    else:
        try:
            e_date = datetime.strptime(earnings_date, "%Y-%m-%d").date()
        except ValueError:
            return {"stock": stock, "error": "bad_earnings_date_format"}
        days_to = (e_date - _date.today()).days

    # 缓存（1 小时 TTL，财报临近时手动 force=True 刷）
    cache = Path(SIGNALS_DIR) / f"earnings_im_{stock}_{datetime.now().strftime('%Y%m%d')}.json"
    if not force and cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < CACHE_TTL_SEC:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    result: dict = {
        "stock": stock,
        "earnings_date": earnings_date,
        "days_to_earnings": days_to,
        "ts": datetime.now().isoformat(),
    }
    try:
        import yfinance as yf
        t = yf.Ticker(stock)
        # 现价
        try:
            spot = float(t.info.get("regularMarketPrice"))
        except (TypeError, ValueError):
            hist = t.history(period="1d")
            spot = float(hist["Close"].iloc[-1]) if not hist.empty else None
        if not spot or spot <= 0:
            result["error"] = "no_spot_price"
            _save_cache(cache, result)
            return result
        result["spot"] = round(spot, 2)

        # 找财报日 >= 第一个期权到期日
        all_expiries = t.options or []
        if not all_expiries:
            result["error"] = "no_option_chain"
            _save_cache(cache, result)
            return result

        e_d = datetime.strptime(earnings_date, "%Y-%m-%d").date()
        chosen_exp = None
        for exp in all_expiries:
            try:
                ed = datetime.strptime(exp, "%Y-%m-%d").date()
            except ValueError:
                continue
            if ed >= e_d:
                chosen_exp = exp
                break
        if chosen_exp is None:
            result["error"] = "no_expiry_after_earnings"
            _save_cache(cache, result)
            return result
        result["expiry"] = chosen_exp

        ch = t.option_chain(chosen_exp)
        calls, puts = ch.calls.copy(), ch.puts.copy()
        if calls.empty or puts.empty:
            result["error"] = "empty_chain"
            _save_cache(cache, result)
            return result

        # ATM strike：离 spot 最近
        calls["dist"] = (calls["strike"] - spot).abs()
        puts["dist"]  = (puts["strike"]  - spot).abs()
        atm_call_row = calls.nsmallest(1, "dist").iloc[0]
        atm_put_row  = puts.nsmallest(1, "dist").iloc[0]
        atm_strike   = float(atm_call_row["strike"])
        result["atm_strike"] = atm_strike

        def _mid(row):
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last = float(row.get("lastPrice") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return last  # fallback

        call_mid = _mid(atm_call_row)
        put_mid  = _mid(atm_put_row)
        result["atm_call_mid"] = round(call_mid, 3)
        result["atm_put_mid"]  = round(put_mid,  3)
        straddle = call_mid + put_mid
        result["straddle_price"] = round(straddle, 3)
        result["implied_move_pct"] = round(straddle / spot * 100, 2) if straddle > 0 else None

        # 平滑：取 ATM±1 strike 的 straddle 均值（如果 strike 链够密）
        try:
            calls_s = calls.sort_values("strike").reset_index(drop=True)
            puts_s  = puts.sort_values("strike").reset_index(drop=True)
            idx_c = (calls_s["strike"] - spot).abs().idxmin()
            idx_p = (puts_s["strike"]  - spot).abs().idxmin()
            if 1 <= idx_c <= len(calls_s) - 2 and 1 <= idx_p <= len(puts_s) - 2:
                straddles = []
                for dc, dp in zip([-1, 0, 1], [-1, 0, 1]):
                    cm = _mid(calls_s.iloc[idx_c + dc])
                    pm = _mid(puts_s .iloc[idx_p + dp])
                    if cm > 0 and pm > 0:
                        straddles.append(cm + pm)
                if len(straddles) >= 2:
                    avg = sum(straddles) / len(straddles)
                    result["smoothed_straddle"] = round(avg, 3)
                    result["smoothed_implied_move_pct"] = round(avg / spot * 100, 2)
                    result["smoothed"] = True
        except Exception:
            pass

        # IV (yfinance 提供 impliedVolatility，已经是 decimal e.g. 0.45)
        try:
            iv_call = float(atm_call_row.get("impliedVolatility") or 0)
            iv_put  = float(atm_put_row .get("impliedVolatility") or 0)
            result["atm_iv_call"] = round(iv_call * 100, 1) if iv_call > 0 else None
            result["atm_iv_put"]  = round(iv_put  * 100, 1) if iv_put  > 0 else None
        except Exception:
            pass

        # C/P 成交量比：ATM ±5% 内
        lo, hi = spot * 0.95, spot * 1.05
        c_atm = calls[(calls["strike"] >= lo) & (calls["strike"] <= hi)]
        p_atm = puts [(puts["strike"]  >= lo) & (puts["strike"]  <= hi)]
        c_vol = int(c_atm["volume"].fillna(0).sum())
        p_vol = int(p_atm["volume"].fillna(0).sum())
        result["call_volume_atm"] = c_vol
        result["put_volume_atm"]  = p_vol
        result["cp_volume_ratio"] = round(c_vol / p_vol, 2) if p_vol > 0 else None

    except Exception as e:
        result["error"] = str(e)

    _save_cache(cache, result)
    return result


def _convert_level(
    strike: float | None,
    leveraged_spot: float | None,
    proxy_spot: float | None,
    leverage: float,
) -> dict | None:
    if not strike or not leveraged_spot or not proxy_spot:
        return None
    proxy_pct = (float(strike) - float(proxy_spot)) / float(proxy_spot)
    leveraged_level = float(leveraged_spot) * (1 + float(leverage) * proxy_pct)
    leveraged_pct = (leveraged_level - float(leveraged_spot)) / float(leveraged_spot) * 100
    return {
        "proxy_strike": round(float(strike), 2),
        "proxy_pct": round(proxy_pct * 100, 2),
        "leveraged_level": round(leveraged_level, 2),
        "leveraged_pct": round(leveraged_pct, 2),
        "mapping_method": "current_spot_daily_reset",
        "path_dependent": True,
    }


def get_underlying_adjusted_walls(leveraged_ticker: str) -> list[dict]:
    """Return unlevered-proxy option walls converted to leveraged ETF levels."""
    tk = leveraged_ticker.upper().replace("US.", "")
    proxies = UNDERLYING_WALL_PROXIES.get(tk, [])
    if not proxies:
        return []

    lev = find_walls(tk)
    leveraged_spot = lev.get("spot")
    if lev.get("error") or not leveraged_spot:
        return []

    converted_groups = []
    for proxy in proxies:
        proxy_ticker = proxy["ticker"]
        leverage = float(proxy.get("leverage") or 1.0)
        try:
            raw = find_walls(proxy_ticker)
        except Exception as exc:
            converted_groups.append({
                "proxy": proxy_ticker,
                "label": proxy.get("label", proxy_ticker),
                "error": str(exc),
            })
            continue
        proxy_spot = raw.get("spot")
        group = {
            "proxy": proxy_ticker,
            "label": proxy.get("label", proxy_ticker),
            "proxy_spot": proxy_spot,
            "leveraged_ticker": tk,
            "leveraged_spot": leveraged_spot,
            "leverage": leverage,
            "gex": _calc_gex_proxy(raw.get("walls") or []),
            "walls": [],
        }
        if raw.get("error") or not proxy_spot:
            group["error"] = raw.get("error") or "no_proxy_spot"
            converted_groups.append(group)
            continue
        for w in raw.get("walls") or []:
            if w.get("error"):
                group["walls"].append({"expiry": w.get("expiry"), "error": w.get("error")})
                continue
            cw = w.get("call_wall") or {}
            pw = w.get("put_wall") or {}
            converted = {
                "expiry": w.get("expiry"),
                "category": w.get("category"),
                "days": w.get("days"),
                "call_wall": _convert_level(cw.get("strike"), leveraged_spot, proxy_spot, leverage),
                "put_wall": _convert_level(pw.get("strike"), leveraged_spot, proxy_spot, leverage),
                "max_pain": _convert_level(w.get("max_pain"), leveraged_spot, proxy_spot, leverage),
                "total_call_vol": w.get("total_call_vol", 0),
                "total_put_vol": w.get("total_put_vol", 0),
                "cp_ratio": w.get("cp_ratio"),
            }
            if converted["call_wall"]:
                converted["call_wall"]["volume"] = cw.get("volume")
            if converted["put_wall"]:
                converted["put_wall"]["volume"] = pw.get("volume")
            group["walls"].append(converted)
        converted_groups.append(group)
    return converted_groups


def _save_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def get_options_risk_signal(tickers: list[str] | None = None) -> dict:
    """主入口：聚合三巫日 + GEX 代理 → 期权风险信号 dict。
    供 events_watch 注入 events.options_risk，Claude prompt 可读。

    返回:
      {
        "ts": ISO,
        "witching": {"is_witching_day", "days_to_witching", "phase", "next_witching_date"},
        "per_ticker": {tk: {"gex": {...}, "spot": ..., "walls_n": ...}},
        "summary": {
          "max_risk": "low"|"elevated"|"high"|"extreme",
          "reason": str
        },
      }
    """
    tickers = tickers or DEFAULT_TICKERS
    witch = is_witching_period()
    per_tk = {}
    for tk in tickers:
        try:
            r = find_walls(tk)
            walls = r.get("walls") or []
            gex = _calc_gex_proxy(walls)
            per_tk[tk] = {"gex": gex, "spot": r.get("spot"),
                          "walls_n": len(walls)}
            adjusted = get_underlying_adjusted_walls(tk)
            if adjusted:
                per_tk[tk]["underlying_adjusted"] = adjusted
        except Exception as e:
            per_tk[tk] = {"error": str(e)}

    # 综合 max_risk：三巫日 + 强 GEX 任一触发 → 升级
    strong_gex_count = sum(
        1 for v in per_tk.values()
        if isinstance(v.get("gex"), dict) and v["gex"].get("strength") == "strong"
    )
    if witch["phase"] == "today" and strong_gex_count >= 1:
        max_risk = "extreme"
        reason = f"三巫日 + {strong_gex_count} 只标的强 GEX"
    elif witch["phase"] == "today":
        max_risk = "high"
        reason = "三巫日（gamma 集中到期）"
    elif witch["phase"] == "adjacent" and strong_gex_count >= 1:
        max_risk = "high"
        reason = f"三巫日前 {witch['days_to_witching']}天 + 强 GEX"
    elif witch["phase"] == "adjacent":
        max_risk = "elevated"
        reason = f"三巫日前 {witch['days_to_witching']}天"
    elif witch["phase"] == "approaching":
        max_risk = "elevated" if strong_gex_count >= 2 else "moderate"
        reason = f"三巫日临近（{witch['days_to_witching']}天）"
    else:
        max_risk = "low"
        reason = f"距下次三巫日 {witch['days_to_witching']}天"

    return {
        "ts": datetime.now().isoformat(),
        "witching": witch,
        "per_ticker": per_tk,
        "summary": {"max_risk": max_risk, "reason": reason},
    }


def format_walls_report(tickers: list[str] | None = None) -> list[str]:
    """生成可塞进 logger 的多行报告。"""
    tickers = tickers or DEFAULT_TICKERS
    W = 76
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = "期权墙分析 (yfinance 今日 volume, 月度合约)"
    lines = [
        "+" + "=" * (W - 2) + "+",
        f"|  {title}  |  {now}".ljust(W - 1) + "|",
        "+" + "=" * (W - 2) + "+",
    ]
    # 三巫日 + GEX 风险摘要（顶部）
    try:
        risk = get_options_risk_signal(tickers)
        witch = risk["witching"]
        summary = risk["summary"]
        risk_icon = {"extreme": "⚠⚠⚠", "high": "⚠⚠", "elevated": "⚠",
                      "moderate": "·", "low": "·"}.get(summary["max_risk"], "·")
        if witch["is_witching_day"]:
            lines.append(f"  {risk_icon} 三巫日（今日）— 期权到期 + gamma 集中风险最高")
        elif witch["phase"] in ("adjacent", "approaching"):
            lines.append(f"  {risk_icon} 距三巫日 {witch['days_to_witching']} 天"
                         f"（{witch['next_witching_date']}）— {summary['reason']}")
        lines.append(f"  期权风险等级: {summary['max_risk']}  | {summary['reason']}")
        # 各 ticker GEX 简表
        for tk, info in risk["per_ticker"].items():
            gex = info.get("gex") if isinstance(info, dict) else None
            if gex and gex.get("direction") != "neutral":
                arrow = "↓pin" if gex["direction"] == "positive_pin" else "↑squeeze"
                lines.append(
                    f"    {tk:<6} GEX {arrow:<10} C/P={gex['cp_oi_ratio']:.2f} "
                    f"加权磁吸={gex['weighted_max_pain_pct']:+.2f}%  ({gex['strength']})"
                )
        # 关联单股财报提示（MU 影响 MULL/DRAM，NVDA 影响 SOXL/TQQQ）
        earnings_alerts = []
        for tk in tickers:
            er = _next_earnings_for(tk)
            if er:
                stock, ed, days = er
                tag = "★" if days <= 7 else "·"
                earnings_alerts.append(f"{tag} {tk}→{stock} 财报 {ed} ({days}d)")
        if earnings_alerts:
            lines.append(f"  ⚠ 关联财报: {' | '.join(earnings_alerts)}")
        lines.append("  " + "-" * (W - 4))
    except Exception as exc:
        lines.append(f"  ⚠ 风险摘要失败: {exc}")

    for tk in tickers:
        try:
            r = find_walls(tk)
        except Exception as e:
            lines.append(f"  {tk}: ❌ {e}")
            continue
        if r.get("error"):
            lines.append(f"  {tk}: ❌ {r['error']}")
            continue
        spot = r.get("spot")
        lines.append("")
        lines.append(f"  {tk}  现价 ${spot}")
        walls = r.get("walls") or []
        if not walls:
            lines.append(f"    (无可用期权数据)")
            continue
        for w in walls:
            if w.get("error"):
                lines.append(f"    到期 {w['expiry']}: {w['error']}")
                continue
            cat = w.get("category", "?")
            days = w.get("days", 0)
            cat_label = {
                "0DTE":    f"末日 ({days}d, ★★★ gamma极高)",
                "weekly":  f"周度 ({days}d, ★★ gamma 高)",
                "monthly": f"月度 ({days}d, ★ 中期参考)",
            }.get(cat, f"{cat} ({days}d)")
            lines.append(f"    到期 {w['expiry']} {cat_label}:")
            if w.get("note"):
                lines.append(f"      {w['note']}")
                continue
            cw, pw, mp = w.get("call_wall"), w.get("put_wall"), w.get("max_pain")
            if cw:
                lines.append(
                    f"      Call Wall: ${cw['strike']:>7.1f} "
                    f"(vol {cw['volume']:>7,})  {cw['pct_from_spot']:+6.1f}%  上方阻力/多头共识"
                )
            if pw:
                lines.append(
                    f"      Put  Wall: ${pw['strike']:>7.1f} "
                    f"(vol {pw['volume']:>7,})  {pw['pct_from_spot']:+6.1f}%  下方支撑/空头共识"
                )
            if mp is not None:
                pct = w.get("max_pain_pct", 0)
                arrow = "→ 持平" if abs(pct) < 0.5 else ("↑ 推涨" if pct > 0 else "↓ 拉跌")
                lines.append(
                    f"      Max Pain:  ${mp:>7.1f}  "
                    f"({pct:+6.1f}%)  到期磁吸 {arrow}"
                )
            cp = w.get("cp_ratio")
            lines.append(
                f"      今日成交 call/put: {w['total_call_vol']:>7,} / {w['total_put_vol']:>7,}"
                + (f"  (C/P ratio {cp})" if cp else "")
            )
        adjusted_groups = get_underlying_adjusted_walls(tk)
        if adjusted_groups:
            lines.append(f"    本体期权墙换算 (proxy -> {tk}; 即时现价锚定):")
            for group in adjusted_groups:
                proxy = group.get("proxy", "?")
                label = group.get("label", proxy)
                if group.get("error"):
                    lines.append(f"      {proxy}: {group['error']}")
                    continue
                lines.append(
                    f"      {label}: {proxy} spot ${group.get('proxy_spot')} -> "
                    f"{tk} spot ${group.get('leveraged_spot')} ({group.get('leverage')}x)"
                )
                gex = group.get("gex") or {}
                if gex.get("direction") and gex.get("direction") != "neutral":
                    lines.append(
                        f"        proxy GEX {gex.get('direction')} C/P={gex.get('cp_oi_ratio')} "
                        f"maxPain={gex.get('weighted_max_pain_pct'):+.2f}% ({gex.get('strength')})"
                    )
                for aw in (group.get("walls") or [])[:3]:
                    if aw.get("error"):
                        lines.append(f"        {aw.get('expiry')}: {aw.get('error')}")
                        continue
                    lines.append(f"        {aw.get('expiry')} {aw.get('category')} ({aw.get('days')}d):")
                    if (aw.get("days") or 0) >= 2:
                        lines.append(
                            "          ⚠ 长期限仅作当前参考：杠杆 ETF 每日重置，"
                            "实际价会受波动折损和路径影响；触发时须按最新现价重算"
                        )
                    acw = aw.get("call_wall")
                    apw = aw.get("put_wall")
                    amp = aw.get("max_pain")
                    if acw:
                        lines.append(
                            f"          Call {proxy} ${acw['proxy_strike']:.1f} ({acw['proxy_pct']:+.1f}%) "
                            f"=> {tk}≈${acw['leveraged_level']:.2f} ({acw['leveraged_pct']:+.1f}%)"
                            + (f"  vol {acw['volume']:,}" if acw.get("volume") else "")
                        )
                    if apw:
                        lines.append(
                            f"          Put  {proxy} ${apw['proxy_strike']:.1f} ({apw['proxy_pct']:+.1f}%) "
                            f"=> {tk}≈${apw['leveraged_level']:.2f} ({apw['leveraged_pct']:+.1f}%)"
                            + (f"  vol {apw['volume']:,}" if apw.get("volume") else "")
                        )
                    if amp:
                        lines.append(
                            f"          MaxP {proxy} ${amp['proxy_strike']:.1f} ({amp['proxy_pct']:+.1f}%) "
                            f"=> {tk}≈${amp['leveraged_level']:.2f} ({amp['leveraged_pct']:+.1f}%)"
                        )
    lines.append("=" * (W))
    lines.append("  解读提示：")
    lines.append("    · Call Wall 比现价高 1-3% 且成交量集中 → 多头追高目标；超 5% → 上方较远的对冲压制")
    lines.append("    · Put Wall 比现价低 1-3% → 下方支撑较硬；超 5% → 对冲位远,对当前价支撑弱")
    lines.append("    · Max Pain 与现价方向 = 期权市场的「到期日预期价」——磁吸方向参考")
    lines.append("    · C/P ratio > 1.5 → 多头主导今日活跃度; < 0.7 → 空头/对冲主导")
    lines.append("=" * W)
    return lines


if __name__ == "__main__":
    for line in format_walls_report():
        print(line)
