"""
MarketWatchAgent — Pure Python, zero LLM tokens.
Pulls moomoo OpenD snapshot + daily K-line, computes RSI/MA/volume indicators.
Inspired by QuantConnect's Alpha Model: data feed → raw signals.
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from moomoo import KLType, AuType, RET_OK

from moomoo_pool import get_quote_ctx

# GLD/USO 覆盖 overnight_pct 为 GC=F / CL=F 24h 期货真实动量
# （NYSE extended-hours 太稀薄，抓不到 Asia/London 商品期货移动）
try:
    from commodity_overlay import enrich_market_snapshot as _enrich_commodity
except Exception:
    _enrich_commodity = None

_ET = ZoneInfo("America/New_York")


def _classify_extended_sessions() -> dict:
    """
    根据当前 ET 时间，给 pre/after/overnight 三个字段标注实际指代的日期 + 状态。

    核心原则：moomoo 这三个字段返回的是「最近一次有数据的会话」——
      - 会话进行中 → 今日该会话
      - 会话已结束 → 该会话所属的那一天（可能是今日，也可能是昨日）
    例如 ET 01:40，今日盘前(4:00 开始)还没开，moomoo 返回的 pre_* 是
    **昨日已完成的盘前**——之前的代码错误标成「今日盘前(待开始)」误导 AI。

    会话时段:
      pre-market   : 04:00-09:30 ET
      regular      : 09:30-16:00 ET
      after-market : 16:00-20:00 ET
      overnight    : 20:00-04:00 ET (跨日，归属"该 session 开始的那天")
    """
    now_et = datetime.now(_ET)
    today = now_et.date()
    today_s = today.isoformat()
    yest_s  = (today - timedelta(days=1)).isoformat()
    t = now_et.hour * 60 + now_et.minute  # 当日分钟数
    now_str = now_et.strftime("%Y-%m-%d %H:%M ET")

    # 周末：所有时段都是上周五（最近交易日）的数据
    if now_et.weekday() == 5:    # 周六
        last_fri = (today - timedelta(days=1)).isoformat()
        return {
            "pre":  (last_fri, "上周五盘前(已结束)"),
            "after": (last_fri, "上周五盘后(已结束)"),
            "on":   (last_fri, "上周五夜盘(已结束)"),
            "now_et": now_str,
        }
    if now_et.weekday() == 6:    # 周日
        last_fri = (today - timedelta(days=2)).isoformat()
        # 周日 18:00 后期货开始夜盘——但股票 ETF 没有此概念，仍标已结束
        return {
            "pre":  (last_fri, "上周五盘前(已结束)"),
            "after": (last_fri, "上周五盘后(已结束)"),
            "on":   (last_fri, "上周五夜盘(已结束)"),
            "now_et": now_str,
        }

    # 工作日：按三段会话各自判断
    # ── pre-market (04:00-09:30 ET) ──
    if 240 <= t < 570:
        pre = (today_s, "今日盘前(进行中)")
    elif t >= 570:
        # 09:30 之后：今日盘前已结束
        pre = (today_s, "今日盘前(已结束)")
    else:
        # 00:00-04:00 ET：今日盘前还没开，moomoo 返回的是昨日已结束的盘前
        pre = (yest_s, "昨日盘前(已结束)")

    # ── after-market (16:00-20:00 ET) ──
    if 960 <= t < 1200:
        after = (today_s, "今日盘后(进行中)")
    elif t >= 1200:
        after = (today_s, "今日盘后(已结束)")
    else:
        # 00:00-16:00 ET：今日盘后还没开，moomoo 返回的是昨日已结束的盘后
        after = (yest_s, "昨日盘后(已结束)")

    # ── overnight (20:00-04:00 ET, 跨日，归属 session 开始的那天) ──
    if t >= 1200:
        # 20:00 之后：今晚的夜盘已经开始，session 归属今日
        on = (today_s, "今晚夜盘(进行中)")
    elif t < 240:
        # 00:00-04:00 ET：昨晚开始的夜盘进行中，session 归属昨日
        on = (yest_s, "昨夜夜盘(进行中)")
    else:
        # 04:00-20:00 ET：最近一次夜盘是「昨晚-今早」(昨日 20:00 → 今日 04:00)，已结束
        on = (yest_s, "昨夜夜盘(已结束)")

    return {"pre": pre, "after": after, "on": on, "now_et": now_str}


def _psar_trend(high: np.ndarray, low: np.ndarray,
                af_step: float = 0.02, af_max: float = 0.20) -> np.ndarray:
    """Parabolic SAR。返回 trend 数组：1=多头，-1=空头。"""
    n = len(high)
    trend = np.ones(n, dtype=int)
    sar = np.zeros(n); ep = np.zeros(n); af = np.full(n, af_step)
    trend[0] = 1 if (n > 1 and high[1] >= high[0]) else -1
    sar[0]   = low[0]  if trend[0] == 1 else high[0]
    ep[0]    = high[0] if trend[0] == 1 else low[0]
    for i in range(1, n):
        t = trend[i-1]; s = sar[i-1]; e = ep[i-1]; a = af[i-1]
        if t == 1:
            ns = min(s + a*(e-s), low[i-1], low[i-2] if i>=2 else low[i-1])
            if low[i] < ns:
                trend[i]=-1; sar[i]=e; ep[i]=low[i]; af[i]=af_step
            else:
                trend[i]=1; sar[i]=ns
                if high[i]>e: ep[i]=high[i]; af[i]=min(a+af_step, af_max)
                else:         ep[i]=e;        af[i]=a
        else:
            ns = max(s + a*(e-s), high[i-1], high[i-2] if i>=2 else high[i-1])
            if high[i] > ns:
                trend[i]=1;  sar[i]=e; ep[i]=high[i]; af[i]=af_step
            else:
                trend[i]=-1; sar[i]=ns
                if low[i]<e: ep[i]=low[i]; af[i]=min(a+af_step, af_max)
                else:        ep[i]=e;       af[i]=a
    return trend


def _compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - 100 / (1 + rs)


def _compute_cci(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                 period: int = 20) -> float:
    tp     = (highs + lows + closes) / 3
    sma    = tp.rolling(period).mean()
    mad    = tp.rolling(period).apply(lambda x: abs(x - x.mean()).mean(), raw=True)
    cci    = (tp - sma) / (0.015 * mad.replace(0, float("nan")))
    return float(cci.iloc[-1])


def _cci_zone(cci: float | None) -> str:
    if cci is None or cci != cci:  # NaN check
        return "neutral"
    if cci > 100:  return "overbought"
    if cci < -100: return "oversold"
    return "neutral"


def _crossover(a: pd.Series, b: pd.Series, lookback: int = 3) -> str:
    """Returns 'golden', 'death', or 'none' based on recent `lookback` bars."""
    for i in range(-lookback, 0):
        try:
            if a.iloc[i-1] < b.iloc[i-1] and a.iloc[i] >= b.iloc[i]:
                return "golden"
            if a.iloc[i-1] > b.iloc[i-1] and a.iloc[i] <= b.iloc[i]:
                return "death"
        except (IndexError, ValueError):
            break
    return "none"


def _level_cross(series: pd.Series, level: float, lookback: int = 3) -> str:
    """Returns 'up', 'dn', or 'none' if series crossed `level` in recent bars."""
    for i in range(-lookback, 0):
        try:
            if series.iloc[i-1] < level <= series.iloc[i]:
                return "up"
            if series.iloc[i-1] > level >= series.iloc[i]:
                return "dn"
        except (IndexError, ValueError):
            break
    return "none"


def get_intraday_context(ticker: str) -> dict | None:
    """
    60分钟K辅助入场判断（日K交易者）。
    主信号来自日K；此函数仅用于判断当日入场时机。
    返回: h1_trend, h1_rsi, h1_ma_cross, h1_note
    """
    try:
        ctx = get_quote_ctx()
    except Exception:
        return None
    try:
        end   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        # extended_time=True 把盘前(4:00-9:30)、盘后(16:00-20:00)、夜盘 K 线一起拉
        # 没这个参数的话，盘前盘中都看不到最新分钟数据，只能靠 snapshot 一个点
        ret, kl, _ = ctx.request_history_kline(
            ticker, start=start, end=end,
            ktype=KLType.K_60M, autype=AuType.QFQ, max_count=48,
            extended_time=True,
        )
        if ret != RET_OK or kl is None or len(kl) < 10:
            return None

        closes  = kl["close"].astype(float)
        ma5_s   = closes.rolling(5).mean()
        ma10_s  = closes.rolling(10).mean()
        rsi_s   = _compute_rsi(closes)

        # 最近 K 线时间戳（让下游知道数据多新）
        latest_bar_time = str(kl.iloc[-1]["time_key"]) if "time_key" in kl.columns else ""

        h1_rsi   = round(float(rsi_s.iloc[-1]), 1)
        h1_trend = "up" if float(closes.iloc[-1]) > float(ma10_s.iloc[-1]) else "down"
        h1_cross = _crossover(ma5_s, ma10_s)

        parts = [f"最新K线@{latest_bar_time[-8:-3] if latest_bar_time else '?'}  "
                 f"趋势:{'向上' if h1_trend == 'up' else '向下'}  RSI:{h1_rsi}"]
        if h1_cross == "golden":
            parts.append("近期金叉")
        elif h1_cross == "death":
            parts.append("近期死叉")

        return {
            "h1_trend":    h1_trend,
            "h1_rsi":      h1_rsi,
            "h1_ma_cross": h1_cross,
            "h1_note":     "  ".join(parts),
        }
    except Exception:
        return None


def get_shortterm_context(ticker: str) -> dict | None:
    """
    15分钟K短线机会扫描（辅助层，独立于日K主信号）。
    扫描 4 个短线信号：
      1. RSI 极值（< 25 / > 75）
      2. MA5/MA20 金叉/死叉（近 3 根 K）
      3. 突破近 6h 高低点
      4. 放量启动（量比 > 2 + 价格同向）
    满足 ≥2 个 → 输出"短线机会"标签。

    返回: m15_signals (list), m15_score (int), m15_direction (str), m15_note (str)
    """
    try:
        ctx = get_quote_ctx()
    except Exception:
        return None
    try:
        end   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 拉 5 个交易日的 15min K（含盘前盘后），约 200 根
        start = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        # extended_time=True 让 15min K 包含盘前 4:00-9:30 + 盘后 16:00-20:00 的 bar
        # 否则盘前跑系统时，最新 K 还是上一个交易日 16:00 收盘那根，完全错过盘前波动
        ret, kl, _ = ctx.request_history_kline(
            ticker, start=start, end=end,
            ktype=KLType.K_15M, autype=AuType.QFQ, max_count=200,
            extended_time=True,
        )
        if ret != RET_OK or kl is None or len(kl) < 25:
            return None

        closes  = kl["close"].astype(float)
        highs   = kl["high"].astype(float)
        lows    = kl["low"].astype(float)
        volumes = kl["volume"].astype(float)
        last_close = float(closes.iloc[-1])
        latest_bar_time = str(kl.iloc[-1]["time_key"]) if "time_key" in kl.columns else ""

        ma5_s   = closes.rolling(5).mean()
        ma20_s  = closes.rolling(20).mean()
        rsi_s   = _compute_rsi(closes)
        rsi     = float(rsi_s.iloc[-1])

        bull_signals: list[str] = []
        bear_signals: list[str] = []

        # 1. RSI 极值
        if rsi <= 25:
            bull_signals.append(f"RSI极度超卖(15m RSI={rsi:.0f})")
        elif rsi >= 75:
            bear_signals.append(f"RSI极度超买(15m RSI={rsi:.0f})")

        # 2. MA 金叉/死叉（近3根）
        ma_x = _crossover(ma5_s, ma20_s, lookback=3)
        if ma_x == "golden":
            bull_signals.append("15m 均线金叉(MA5上穿MA20)")
        elif ma_x == "death":
            bear_signals.append("15m 均线死叉(MA5下穿MA20)")

        # 3. 突破近 6h 高低点（24 根 15m K = 6h）
        if len(closes) >= 25:
            recent_high = float(highs.iloc[-25:-1].max())  # 不含当前根
            recent_low  = float(lows .iloc[-25:-1].min())
            if last_close > recent_high:
                bull_signals.append(f"突破6h高点 ${recent_high:.2f}")
            elif last_close < recent_low:
                bear_signals.append(f"跌破6h低点 ${recent_low:.2f}")

        # 4. 放量启动（量比 > 2 + 价格同向）
        if len(volumes) >= 21:
            vol_avg20 = float(volumes.iloc[-21:-1].mean())
            cur_vol   = float(volumes.iloc[-1])
            vol_ratio = cur_vol / vol_avg20 if vol_avg20 > 0 else 1.0
            prev_close = float(closes.iloc[-2])
            bar_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0
            if vol_ratio >= 2.0 and bar_pct > 0.3:
                bull_signals.append(f"放量上涨(15m 量比{vol_ratio:.1f})")
            elif vol_ratio >= 2.0 and bar_pct < -0.3:
                bear_signals.append(f"放量下跌(15m 量比{vol_ratio:.1f})")

        bull_n = len(bull_signals)
        bear_n = len(bear_signals)

        # 综合判断：满足 ≥2 个同向信号 → 短线机会
        if bull_n >= 2 and bull_n > bear_n:
            direction = "long"
            note = f"短线机会(做多): {' · '.join(bull_signals)}  建议持仓 1-4 小时"
        elif bear_n >= 2 and bear_n > bull_n:
            direction = "short"
            note = f"短线机会(做空/减仓): {' · '.join(bear_signals)}  建议持仓 1-4 小时"
        elif bull_n + bear_n > 0:
            direction = "mixed"
            sigs = bull_signals + bear_signals
            note = f"短线信号弱(信号数 {bull_n + bear_n}，方向不明): {' · '.join(sigs)}"
        else:
            direction = "neutral"
            note = "短线无信号"

        # 在 note 前缀加上最新 K 时间戳，让下游知道这个判断基于多新的数据
        if latest_bar_time:
            note = f"[最新15m K线@{latest_bar_time}] {note}"

        return {
            "m15_rsi":       round(rsi, 1),
            "m15_direction": direction,
            "m15_score":     max(bull_n, bear_n),
            "m15_bull":      bull_signals,
            "m15_bear":      bear_signals,
            "m15_note":      note,
            "m15_latest_bar": latest_bar_time,
        }
    except Exception:
        return None


def get_market_signal(ticker: str) -> dict:
    """
    Returns compact signal dict for one ticker.
    Raises on connection failure so orchestrator can catch and log.
    """
    ctx = get_quote_ctx()
    try:
        # 1. Real-time snapshot
        ret_s, snap = ctx.get_market_snapshot([ticker])
        if ret_s != RET_OK:
            return {"ticker": ticker, "error": f"snapshot: {snap}"}

        row = snap.iloc[0]
        price      = float(row["last_price"])
        prev_close = float(row["prev_close_price"])
        vol_today  = float(row["volume"])
        high_52w   = float(row["highest52weeks_price"])
        low_52w    = float(row["lowest52weeks_price"])

        # 盘前 / 盘后 / 夜盘（moomoo 已经在 snapshot 里给好了）
        def _f(col):
            v = row.get(col)
            try:
                v = float(v)
                return v if v != 0 else None
            except (TypeError, ValueError):
                return None
        bid_price    = _f("bid_price")
        ask_price    = _f("ask_price")
        pre_price    = _f("pre_price")
        pre_pct      = _f("pre_change_rate")
        pre_vol      = _f("pre_volume")
        after_price  = _f("after_price")
        after_pct    = _f("after_change_rate")
        after_vol    = _f("after_volume")
        on_price     = _f("overnight_price")
        on_pct       = _f("overnight_change_rate")
        on_vol       = _f("overnight_volume")

        # 给三个扩展时段贴上明确的「日期 + 状态」标签，避免下游 AI 混淆
        _sess = _classify_extended_sessions()
        pre_date,   pre_label   = _sess["pre"]
        after_date, after_label = _sess["after"]
        on_date,    on_label    = _sess["on"]

        # 2. Historical daily K-line (last 60 trading days)
        end_date   = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        ret_k, kl, _ = ctx.request_history_kline(
            ticker, start=start_date, end=end_date,
            ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=60
        )

        if ret_k != RET_OK or kl is None or len(kl) < 15:
            return {
                "ticker": ticker.split(".")[-1],
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "price": round(price, 2),
                "pct_chg": round((price - prev_close) / prev_close * 100, 2),
                "rsi_14": None, "ma20": None, "vol_ratio": None,
                "trend": "unknown", "ma_stack": "unknown",
                "is_new_52w_high": False,
                "error": "insufficient kline data",
            }

        closes  = kl["close"].astype(float)
        highs   = kl["high"].astype(float)
        lows    = kl["low"].astype(float)
        volumes = kl["volume"].astype(float)
        opens   = kl["open"].astype(float)

        # ── 上一个已完成的常规交易时段（"昨日盘中"，连接盘前盘后的核心数据）─────
        # 之前的输出只显示盘前/盘后/夜盘，漏掉了最重要的盘中——AI 看不到完整链路。
        prev_session_data = None
        try:
            today_et = datetime.now(_ET).date()
            for idx in range(len(kl) - 1, -1, -1):
                k_date = pd.to_datetime(kl.iloc[idx]["time_key"]).date()
                if k_date < today_et:
                    p_open  = float(opens.iloc[idx])
                    p_close = float(closes.iloc[idx])
                    p_high  = float(highs.iloc[idx])
                    p_low   = float(lows.iloc[idx])
                    p_pct   = (p_close - p_open) / p_open * 100 if p_open else 0
                    prev_session_data = {
                        "date":  k_date.isoformat(),
                        "open":  round(p_open, 2),
                        "close": round(p_close, 2),
                        "high":  round(p_high, 2),
                        "low":   round(p_low, 2),
                        "pct":   round(p_pct, 2),
                    }
                    break
        except Exception:
            prev_session_data = None

        # ── 今日盘中（仅在 ET >= 9:30 即正式开盘后才输出，避免误把昨日开盘价当今日）─
        # moomoo 的 open_price 在今日开盘前返回的是上一交易日的开盘价（无日期标识）
        # 必须用 ET 时间门控，否则下游 AI 会把"昨日的开盘价"误读成"今日已开盘"。
        today_session_data = None
        not_yet_opened_note = None
        try:
            now_et = datetime.now(_ET)
            t_min = now_et.hour * 60 + now_et.minute
            is_weekday = now_et.weekday() < 5

            if is_weekday and t_min >= 570:
                # 9:30 ET 之后才认为今日开盘有效
                today_open = float(row.get("open_price", 0) or 0)
                if today_open > 0:
                    today_pct = (price - today_open) / today_open * 100
                    if t_min < 960:        status = "盘中进行中"
                    elif t_min < 1200:     status = "盘中已收"
                    else:                  status = "盘中已收"
                    today_session_data = {
                        "date":  now_et.date().isoformat(),
                        "open":  round(today_open, 2),
                        "current": round(price, 2),
                        "pct":   round(today_pct, 2),
                        "status": status,
                    }
            else:
                # 今日尚未开盘，显式标注
                if not is_weekday:
                    not_yet_opened_note = (
                        f"{now_et.date().isoformat()} 周末，无盘中数据"
                    )
                else:
                    mins_until_open = 570 - t_min
                    hrs = mins_until_open // 60
                    mins = mins_until_open % 60
                    not_yet_opened_note = (
                        f"{now_et.date().isoformat()} 今日尚未开盘"
                        f"（距 9:30 ET 还有 {hrs}h{mins}m）"
                    )
        except Exception:
            today_session_data = None
            not_yet_opened_note = None

        # Append today's live price so ALL indicators reflect current session.
        # Use price as proxy for today's high/low (intraday extremes not always reliable).
        all_closes = pd.concat([closes, pd.Series([price])], ignore_index=True)
        all_highs  = pd.concat([highs,  pd.Series([price])], ignore_index=True)
        all_lows   = pd.concat([lows,   pd.Series([price])], ignore_index=True)
        rsi = float(_compute_rsi(all_closes).iloc[-1])
        cci = _compute_cci(all_highs, all_lows, all_closes)

        # MA series include live price → MA20/MA50/MA5 都反映当前价
        n = len(all_closes)
        ma5_s   = all_closes.rolling(5).mean()
        ma20_s  = all_closes.rolling(20).mean()
        ma50_s  = all_closes.rolling(min(50, n)).mean()
        ma20    = float(ma20_s.iloc[-1])
        ma50    = float(ma50_s.iloc[-1])
        vol_avg20 = float(volumes.tail(20).mean())

        # ── 顶/底背离检测（C 方案）：OOS 回测显示是反指标，已停用 ─────────────
        # 保留检测函数供未来研究，但 _etf_rules 不再消费这些字段。
        def _detect_div(price_series, ind_series, side: str,
                          lookback: int = 25, min_gap: int = 4, ind_delta_min: float = 20) -> str | None:
            """side: 'top' 看顶背离, 'bot' 看底背离."""
            if len(price_series) < lookback + 5:
                return None
            p = price_series.iloc[-lookback:].reset_index(drop=True)
            ind = ind_series.iloc[-lookback:].reset_index(drop=True)
            # 找局部峰/谷（前后 2 根都更低/高）
            peaks = []
            for i in range(2, len(p) - 2):
                if side == "top":
                    if p.iloc[i] == p.iloc[i-2:i+3].max():
                        peaks.append(i)
                else:
                    if p.iloc[i] == p.iloc[i-2:i+3].min():
                        peaks.append(i)
            # 间距过滤
            filtered = []
            for idx in peaks:
                if not filtered or idx - filtered[-1] >= min_gap:
                    filtered.append(idx)
            if len(filtered) < 2:
                return None
            p1_i, p2_i = filtered[-2], filtered[-1]
            p1_price, p2_price = p.iloc[p1_i], p.iloc[p2_i]
            p1_ind, p2_ind = ind.iloc[p1_i], ind.iloc[p2_i]
            if side == "top":
                # 顶背离：价格新高 + 指标更低
                if p2_price > p1_price * 1.005 and p2_ind < p1_ind - ind_delta_min:
                    bars_since = lookback - 1 - p2_i
                    return "confirmed" if bars_since >= 2 else "forming"
            else:
                # 底背离：价格新低 + 指标更高
                if p2_price < p1_price * 0.995 and p2_ind > p1_ind + ind_delta_min:
                    bars_since = lookback - 1 - p2_i
                    return "confirmed" if bars_since >= 2 else "forming"
            return None

        try:
            cci_series = pd.Series(cci)  # cci 已是 series（_compute_cci 返回）
            cci_top_div = _detect_div(all_closes, cci_series, "top")
            cci_bot_div = _detect_div(all_closes, cci_series, "bot")
            rsi_series = _compute_rsi(all_closes)
            rsi_top_div = _detect_div(all_closes, rsi_series, "top", ind_delta_min=8)
            rsi_bot_div = _detect_div(all_closes, rsi_series, "bot", ind_delta_min=8)
        except Exception:
            cci_top_div = cci_bot_div = rsi_top_div = rsi_bot_div = None

        # ── 多日累积涨幅 + 偏离均线 + 历史波动率（overheated regime / CAUTION 用） ──
        def _cum_pct(n_days: int) -> float | None:
            if len(all_closes) <= n_days:
                return None
            base = float(all_closes.iloc[-n_days - 1])
            return round((price - base) / base * 100, 2) if base else None
        cum_5d_pct  = _cum_pct(5)
        cum_10d_pct = _cum_pct(10)
        dist_from_ma20_pct = round((price - ma20) / ma20 * 100, 2) if ma20 else None
        # 20 日日内收益率年化波动率（240 交易日）—— 给自适应阈值用
        daily_ret = all_closes.pct_change().tail(20).dropna()
        vol_20d_annual = round(float(daily_ret.std() * (252 ** 0.5) * 100), 2) if len(daily_ret) >= 10 else None
        vol_ratio = vol_today / vol_avg20 if vol_avg20 > 0 else 1.0

        is_new_high = price >= high_52w * 0.998
        pct_chg     = (price - prev_close) / prev_close * 100
        trend       = "up" if price > ma20 else "down"
        ma_stack    = "bull" if ma20 > ma50 else "bear"
        dist_high   = (price - high_52w) / high_52w * 100

        # 语义区间（与 strategy_engine _compute_indicators 保持一致）
        rsi_zone = ("oversold" if rsi < 35 else "overbought" if rsi > 70 else "neutral")
        vol_zone = ("shrink"   if vol_ratio < 0.8 else "expand" if vol_ratio > 1.25 else "normal")
        # 当日涨跌幅区间（针对 3x ETF 灵敏度调高，±1% 已是显著方向信号）
        if   pct_chg <= -5:   pct_chg_zone = "crash"
        elif pct_chg <= -2:   pct_chg_zone = "drop"
        elif pct_chg <= -1:   pct_chg_zone = "mild_drop"
        elif pct_chg >=  5:   pct_chg_zone = "surge"
        elif pct_chg >=  2:   pct_chg_zone = "pop"
        elif pct_chg >=  1:   pct_chg_zone = "mild_pop"
        else:                 pct_chg_zone = "normal"

        # Crossover signals (use live-price-augmented series)
        ma_cross  = _crossover(ma5_s, ma20_s)
        rsi_s     = _compute_rsi(all_closes)
        rsi_cross = ("up_70" if _level_cross(rsi_s, 70) == "up"
                     else "dn_30" if _level_cross(rsi_s, 30) == "dn"
                     else "none")
        tp_s      = (all_highs + all_lows + all_closes) / 3
        sma_tp    = tp_s.rolling(20).mean()
        mad_s     = tp_s.rolling(20).apply(lambda x: abs(x - x.mean()).mean(), raw=True)
        cci_s     = (tp_s - sma_tp) / (0.015 * mad_s.replace(0, float("nan")))
        cci_cross = ("up_100" if _level_cross(cci_s, 100) == "up"
                     else "dn_100" if _level_cross(cci_s, -100) == "dn"
                     else "none")

        # Bollinger Bands (20, 2σ) — 用包含实时价的 all_closes
        bb_std   = all_closes.rolling(20).std(ddof=1)
        bb_upper = ma20_s + 2 * bb_std
        bb_lower = ma20_s - 2 * bb_std
        bb_width = (bb_upper - bb_lower).replace(0, float("nan"))
        bb_pct_s = (all_closes - bb_lower) / bb_width
        bb_pct   = float(bb_pct_s.iloc[-1]) if not bb_pct_s.empty else float("nan")
        bb_zone  = ("above" if bb_pct > 1.0 else "below" if bb_pct < 0.0 else "normal")
        if bb_pct != bb_pct: bb_zone = "normal"

        # MACD (12,26,9) — 仅观察, 不参与决策
        ema12_s = all_closes.ewm(span=12, adjust=False).mean()
        ema26_s = all_closes.ewm(span=26, adjust=False).mean()
        macd_dif_s = ema12_s - ema26_s
        macd_dea_s = macd_dif_s.ewm(span=9, adjust=False).mean()
        macd_dif = float(macd_dif_s.iloc[-1]) if not macd_dif_s.empty else 0.0
        macd_dea = float(macd_dea_s.iloc[-1]) if not macd_dea_s.empty else 0.0
        macd_hist = macd_dif - macd_dea
        macd_zone = ("bull" if macd_dif > 0 and macd_dea > 0
                     else "bear" if macd_dif < 0 and macd_dea < 0 else "neutral")
        if len(macd_dif_s) >= 2:
            prev_dif = float(macd_dif_s.iloc[-2]); prev_dea = float(macd_dea_s.iloc[-2])
            macd_signal = ("golden" if prev_dif < prev_dea and macd_dif >= macd_dea
                           else "death" if prev_dif > prev_dea and macd_dif <= macd_dea
                           else "none")
        else:
            macd_signal = "none"

        # ADX(14) — 仅观察, 不参与决策
        if n >= 28:
            import numpy as np
            up_move   = all_highs.diff()
            down_move = -all_lows.diff()
            plus_dm  = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=all_highs.index)
            minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=all_highs.index)
            tr = pd.concat([all_highs - all_lows,
                            (all_highs - all_closes.shift()).abs(),
                            (all_lows  - all_closes.shift()).abs()], axis=1).max(axis=1)
            atr14_s  = tr.rolling(14).mean()
            plus_di  = 100 * plus_dm.rolling(14).mean() / atr14_s
            minus_di = 100 * minus_dm.rolling(14).mean() / atr14_s
            dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
            adx_s    = dx.rolling(14).mean()
            adx_14   = float(adx_s.iloc[-1]) if not adx_s.empty and adx_s.iloc[-1] == adx_s.iloc[-1] else 0.0
        else:
            adx_14 = 0.0
        adx_zone = ("extreme" if adx_14 >= 40 else "strong" if adx_14 >= 25
                    else "moderate" if adx_14 >= 20 else "weak")

        # Parabolic SAR — 用包含实时价的 all_highs/all_lows
        psar_arr    = _psar_trend(all_highs.values, all_lows.values) if len(all_highs) >= 5 else None
        if psar_arr is not None:
            psar_cur  = int(psar_arr[-1])
            psar_prev = int(psar_arr[-2]) if len(psar_arr) >= 2 else psar_cur
            psar_signal = ("bear_flip" if psar_prev == 1  and psar_cur == -1 else
                           "bull_flip" if psar_prev == -1 and psar_cur ==  1 else "none")
        else:
            psar_cur = 1; psar_signal = "none"

        result = {
            "ticker":          ticker.split(".")[-1],
            "ts":              datetime.now().strftime("%Y-%m-%d %H:%M"),
            "price":           round(price, 2),
            "bid_price":       bid_price,
            "ask_price":       ask_price,
            "volume":          vol_today,
            "avg_volume_20":   round(vol_avg20, 0),
            "pct_chg":         round(pct_chg, 2),
            "rsi_14":          round(rsi, 1),
            "cci_20":          round(cci, 1) if cci == cci else None,
            "cci_zone":        _cci_zone(cci),
            "ma20":            round(ma20, 2),
            "ma50":            round(ma50, 2),
            "vol_ratio":       round(vol_ratio, 2),
            "is_new_52w_high": is_new_high,
            "dist_from_high":  round(dist_high, 2),
            "trend":           trend,
            "ma_stack":        ma_stack,
            "rsi_zone":        rsi_zone,
            "vol_zone":        vol_zone,
            "pct_chg_zone":    pct_chg_zone,
            # 盘前/盘后/夜盘扩展数据 + 明确的日期/状态标签
            "pre_price":       pre_price,
            "pre_pct":         pre_pct,
            "pre_volume":      pre_vol,
            "pre_date":        pre_date,
            "pre_label":       pre_label,
            "after_price":     after_price,
            "after_pct":       after_pct,
            "after_volume":    after_vol,
            "after_date":      after_date,
            "after_label":     after_label,
            "overnight_price": on_price,
            "overnight_pct":   on_pct,
            "overnight_volume": on_vol,
            "overnight_date":  on_date,
            "overnight_label": on_label,
            "now_et":          _sess["now_et"],
            # 完整时段链路：盘前 → 盘中 → 盘后 → 夜盘
            "prev_session":    prev_session_data,
            "prev_pct":        prev_session_data.get("pct", 0) if prev_session_data else 0,
            "today_session":   today_session_data,
            "today_not_yet_opened": not_yet_opened_note,
            "ma_cross":        ma_cross,
            "rsi_cross":       rsi_cross,
            "cci_cross":       cci_cross,
            # 多日累积 + 偏离 + 波动率
            "cum_5d_pct":      cum_5d_pct,
            "cum_10d_pct":     cum_10d_pct,
            "dist_from_ma20_pct": dist_from_ma20_pct,
            "vol_20d_annual":  vol_20d_annual,
            # 顶/底背离（C 方案 — 价格新高/低 + 指标反向）
            "cci_top_divergence": cci_top_div,
            "cci_bot_divergence": cci_bot_div,
            "rsi_top_divergence": rsi_top_div,
            "rsi_bot_divergence": rsi_bot_div,
            "bb_pct":          round(bb_pct, 3) if bb_pct == bb_pct else None,
            "bb_zone":         bb_zone,
            "bb_upper":        round(float(bb_upper.iloc[-1]), 2),
            "bb_lower":        round(float(bb_lower.iloc[-1]), 2),
            "psar_trend":      psar_cur,
            "psar_signal":     psar_signal,
            # MACD / ADX — 仅观察展示, 当前不参与决策评分
            "macd_dif":        round(macd_dif, 3),
            "macd_dea":        round(macd_dea, 3),
            "macd_hist":       round(macd_hist, 3),
            "macd_zone":       macd_zone,
            "macd_signal":     macd_signal,
            "adx_14":          round(adx_14, 1),
            "adx_zone":        adx_zone,
        }
        # GLD/USO：用 GC=F / CL=F 24h 期货覆盖 overnight_pct（幂等，非商品 ticker 直接返回原 dict）
        if _enrich_commodity:
            try:
                result = _enrich_commodity(result)
            except Exception:
                pass
        return result
    except Exception as e:
        return {"ticker": ticker.split(".")[-1], "error": str(e)}
