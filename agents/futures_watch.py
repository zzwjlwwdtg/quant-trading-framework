"""
FuturesWatchAgent — Pure Python, zero LLM tokens.
Polls moomoo OpenD for COMEX Gold (GCmain) snapshot + daily K-line.
Computes RSI, ATR, MA, open-interest change — futures-specific metrics.
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from moomoo import KLType, AuType, RET_OK

from moomoo_pool import get_quote_ctx
from data_quality import quote_ts_from_update_time


def _psar_trend(high: np.ndarray, low: np.ndarray,
                af_step: float = 0.02, af_max: float = 0.20) -> np.ndarray:
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


def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _cci(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 20) -> float:
    tp  = (highs + lows + closes) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: abs(x - x.mean()).mean(), raw=True)
    val = (tp - sma) / (0.015 * mad.replace(0, float("nan")))
    return float(val.iloc[-1])


def _cci_zone(v: float | None) -> str:
    if v is None or v != v: return "neutral"
    if v > 100:  return "overbought"
    if v < -100: return "oversold"
    return "neutral"


def _crossover(a: pd.Series, b: pd.Series, lookback: int = 3) -> str:
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
    for i in range(-lookback, 0):
        try:
            if series.iloc[i-1] < level <= series.iloc[i]:
                return "up"
            if series.iloc[i-1] > level >= series.iloc[i]:
                return "dn"
        except (IndexError, ValueError):
            break
    return "none"


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> float:
    hl  = highs - lows
    hpc = (highs - closes.shift(1)).abs()
    lpc = (lows  - closes.shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def get_futures_signal(ticker: str = "US.GCmain") -> dict:
    ctx = get_quote_ctx()
    try:
        # Real-time snapshot
        ret_s, snap = ctx.get_market_snapshot([ticker])
        if ret_s != RET_OK:
            return {"ticker": ticker, "error": f"snapshot: {snap}"}

        row        = snap.iloc[0]
        quote_ts   = quote_ts_from_update_time(row.get("update_time"))
        price      = float(row["last_price"])
        prev_close = float(row["prev_close_price"])
        vol_today  = float(row["volume"])
        settle     = float(row.get("future_last_settle_price") or prev_close)
        _p = row.get("future_position");     open_int = int(_p) if pd.notna(_p) and _p else 0
        _c = row.get("future_position_change"); oi_chg = int(_c) if pd.notna(_c) and _c else 0
        high_52w   = float(row["highest52weeks_price"])
        low_52w    = float(row["lowest52weeks_price"])

        # Daily K-line for indicators
        end_date   = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        ret_k, kl, _ = ctx.request_history_kline(
            ticker, start=start_date, end=end_date,
            ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=60
        )

        if ret_k != RET_OK or kl is None or len(kl) < 15:
            pct_chg = (price - prev_close) / prev_close * 100
            return {
                "ticker":     ticker.split(".")[-1],
                "ts":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                "quote_ts":   quote_ts,
                "price":      round(price, 2),
                "settle":     round(settle, 2),
                "pct_chg":    round(pct_chg, 2),
                "open_int":   int(open_int),
                "oi_chg":     int(oi_chg),
                "rsi_14":     None, "atr_14": None,
                "ma20":       None, "ma50":   None,
                "trend":      "unknown", "ma_stack": "unknown",
                "is_new_52w_high": False,
                "vol_ratio":  None,
                "error":      "insufficient kline",
            }

        closes  = kl["close"].astype(float)
        highs   = kl["high"].astype(float)
        lows    = kl["low"].astype(float)
        volumes = kl["volume"].astype(float)

        all_closes = pd.concat([closes, pd.Series([price])], ignore_index=True)
        all_highs  = pd.concat([highs,  pd.Series([price])], ignore_index=True)
        all_lows   = pd.concat([lows,   pd.Series([price])], ignore_index=True)
        rsi = _rsi(all_closes)
        atr = _atr(all_highs, all_lows, all_closes)
        cci = _cci(all_highs, all_lows, all_closes)

        n       = len(all_closes)
        ma5_s   = all_closes.rolling(5).mean()
        ma20_s  = all_closes.rolling(20).mean()
        ma50_s  = all_closes.rolling(min(50, n)).mean()
        ma20    = float(ma20_s.iloc[-1])
        ma50    = float(ma50_s.iloc[-1])
        vol_avg = float(volumes.tail(20).mean())
        vol_rat = vol_today / vol_avg if vol_avg > 0 else 1.0

        is_new_high = price >= high_52w * 0.998
        pct_chg     = (price - prev_close) / prev_close * 100
        trend       = "up" if price > ma20 else "down"
        ma_stack    = "bull" if ma20 > ma50 else "bear"
        dist_high   = (price - high_52w) / high_52w * 100

        rsi_zone = ("oversold" if rsi < 35 else "overbought" if rsi > 70 else "neutral")
        vol_zone = ("shrink"   if vol_rat < 0.8 else "expand" if vol_rat > 1.25 else "normal")
        if   pct_chg <= -5:   pct_chg_zone = "crash"
        elif pct_chg <= -2:   pct_chg_zone = "drop"
        elif pct_chg <= -1:   pct_chg_zone = "mild_drop"
        elif pct_chg >=  5:   pct_chg_zone = "surge"
        elif pct_chg >=  2:   pct_chg_zone = "pop"
        elif pct_chg >=  1:   pct_chg_zone = "mild_pop"
        else:                 pct_chg_zone = "normal"

        # Key round-number levels for gold (psychological support/resistance)
        levels = [2900, 3000, 3100, 3200, 3300, 3400, 3500]
        nearest_above = next((l for l in sorted(levels) if l > price), None)
        nearest_below = next((l for l in sorted(levels, reverse=True) if l < price), None)

        # Crossover signals (use live-price-augmented series)
        ma_cross  = _crossover(ma5_s, ma20_s)
        tp_s      = (all_highs + all_lows + all_closes) / 3
        sma_tp    = tp_s.rolling(20).mean()
        mad_s     = tp_s.rolling(20).apply(lambda x: abs(x - x.mean()).mean(), raw=True)
        cci_s     = (tp_s - sma_tp) / (0.015 * mad_s.replace(0, float("nan")))
        delta_h   = all_closes.diff()
        gain_h    = delta_h.clip(lower=0).rolling(14).mean()
        loss_h    = (-delta_h.clip(upper=0)).rolling(14).mean()
        rsi_s     = 100 - 100 / (1 + gain_h / (loss_h + 1e-10))
        rsi_cross = ("up_70" if _level_cross(rsi_s, 70) == "up"
                     else "dn_30" if _level_cross(rsi_s, 30) == "dn"
                     else "none")
        cci_cross = ("up_100" if _level_cross(cci_s, 100) == "up"
                     else "dn_100" if _level_cross(cci_s, -100) == "dn"
                     else "none")

        # Bollinger Bands (20, 2σ) — 用 all_closes
        bb_std   = all_closes.rolling(20).std(ddof=1)
        bb_upper = ma20_s + 2 * bb_std
        bb_lower = ma20_s - 2 * bb_std
        bb_width = (bb_upper - bb_lower).replace(0, float("nan"))
        bb_pct_s = (all_closes - bb_lower) / bb_width
        bb_pct   = float(bb_pct_s.iloc[-1]) if not bb_pct_s.empty else float("nan")
        bb_zone  = ("above" if bb_pct > 1.0 else "below" if bb_pct < 0.0 else "normal")
        if bb_pct != bb_pct: bb_zone = "normal"

        # Parabolic SAR — 用 all_highs/all_lows
        psar_arr = _psar_trend(all_highs.values, all_lows.values) if len(all_highs) >= 5 else None
        if psar_arr is not None:
            psar_cur  = int(psar_arr[-1])
            psar_prev = int(psar_arr[-2]) if len(psar_arr) >= 2 else psar_cur
            psar_signal = ("bear_flip" if psar_prev == 1  and psar_cur == -1 else
                           "bull_flip" if psar_prev == -1 and psar_cur ==  1 else "none")
        else:
            psar_cur = 1; psar_signal = "none"

        return {
            "ticker":          ticker.split(".")[-1],
            "ts":              datetime.now().strftime("%Y-%m-%d %H:%M"),
            "quote_ts":        quote_ts,
            "price":           round(price, 2),
            "settle":          round(settle, 2),
            "pct_chg":         round(pct_chg, 2),
            "rsi_14":          round(rsi, 1),
            "cci_20":          round(cci, 1) if cci == cci else None,
            "cci_zone":        _cci_zone(cci),
            "atr_14":          round(atr, 2),
            "ma20":            round(ma20, 2),
            "ma50":            round(ma50, 2),
            "vol_ratio":       round(vol_rat, 2),
            "open_int":        int(open_int),
            "oi_chg":          int(oi_chg),
            "is_new_52w_high": is_new_high,
            "dist_from_high":  round(dist_high, 2),
            "trend":           trend,
            "ma_stack":        ma_stack,
            "rsi_zone":        rsi_zone,
            "vol_zone":        vol_zone,
            "pct_chg_zone":    pct_chg_zone,
            "resistance":      nearest_above,
            "support":         nearest_below,
            "ma_cross":        ma_cross,
            "rsi_cross":       rsi_cross,
            "cci_cross":       cci_cross,
            "bb_pct":          round(bb_pct, 3) if bb_pct == bb_pct else None,
            "bb_zone":         bb_zone,
            "bb_upper":        round(float(bb_upper.iloc[-1]), 2),
            "bb_lower":        round(float(bb_lower.iloc[-1]), 2),
            "psar_trend":      psar_cur,
            "psar_signal":     psar_signal,
        }
    except Exception as e:
        return {"ticker": ticker.split(".")[-1], "error": str(e)}
