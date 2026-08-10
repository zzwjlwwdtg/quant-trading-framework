"""
DecisionAgent — 10分制信号引擎 + Regime Detection。

三维度评分:
  技术面 (0-4): RSI极值 + 量价背离 + 趋势/均线
  宏观面 (0-3): VIX恐慌 + CNN贪婪指数 + 收益率曲线
  事件面 (0-3): 重大事件临近 + 突发新闻

行情状态 (Regime):
  bull_trending  — 低波动 + 上升趋势 + F&G < 80：追趋势，放宽减仓阈值
  overheated     — VIX > 20 且 F&G > 70：过热警戒，收紧买入
  recession_risk — 收益率曲线倒挂：保守，黄金看涨偏向
  crisis         — VIX > 30 + 下降趋势：防御优先
  neutral        — 其他：原趋势感知逻辑

支持两种模式:
  get_decision()      → ETF风险信号 (REDUCE / HOLD / CAUTION / WATCH_BUY)
  get_gold_decision() → 黄金方向信号 (BUY / SELL / HOLD)
无API Key时自动使用规则引擎。
"""

import json
import os
from openai import OpenAI

from config import (OPENAI_API_KEY, DECISION_MODEL, RSI_OVERBOUGHT, RSI_OVERSOLD,
                    LEVERAGE_FACTORS, is_sim_active_trading)
from trading_contracts import BULLISH_SIGNAL_ACTIONS, BUY_ACTIONS, confidence_min


def _get_hmm_meta_state() -> str | None:
    """读 signals/hmm_state.json 的 current_label。失败 → None。
    用于 _etf_rules 仅做"更保守方向"的阈值微调（不会放宽）。"""
    try:
        from hmm_regime import load as _hmm_load
        info = _hmm_load()
        if info and info.get("current_prob", 0) >= 0.6:   # 状态置信度要够
            return info.get("current_label")
    except Exception:
        pass
    return None


# ── 板块级 regime（比 SPY 大盘更贴合具体标的） ─────────────────────────────
# 例：SOXL/DRAM/MULL 跟半导体 SMH，不跟大盘 SPY
TICKER_TO_SECTOR = {
    "US.TQQQ": "QQQ",   # 3x QQQ → 参考 Nasdaq
    "US.SQQQ": "QQQ",
    "US.SOXL": "SMH",   # 3x 半导体 → 参考 SMH
    "US.SOXS": "SMH",
    "US.DRAM": "SMH",   # 内存 ETF → 参考半导体
    "US.MULL": "SMH",   # 2x MU → 参考半导体
    "US.GLD":  "GLD",   # 黄金 → 参考自己
    "US.TLT":  "TLT",   # 长期债券
}
_SECTOR_REGIME_CACHE = {"ts": 0, "data": {}}


def _get_sector_regime(ticker: str) -> str | None:
    """按 ticker → 板块基准 → 20d + 5d 涨跌算板块 regime。
    返回：'sector_bear' / 'sector_weak' / 'sector_neutral' / 'sector_strong' / None。
    仅做**单向收紧**（bear/weak → bull_thresh +1，绝不放宽）。
    """
    import time
    import yfinance as yf
    sector = TICKER_TO_SECTOR.get(ticker)
    if not sector:
        return None

    # 板块数据 15 分钟缓存
    now = time.time()
    if now - _SECTOR_REGIME_CACHE["ts"] > 900:
        _SECTOR_REGIME_CACHE["data"] = {}
        _SECTOR_REGIME_CACHE["ts"] = now
    if sector in _SECTOR_REGIME_CACHE["data"]:
        return _SECTOR_REGIME_CACHE["data"][sector]

    try:
        df = yf.Ticker(sector).history(period="30d", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 21:
            return None
        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        p5  = ((price / float(close.iloc[-6])  - 1) * 100)
        p20 = ((price / float(close.iloc[-21]) - 1) * 100)
        # 用与 dashboard /api/sectors 一致的判定
        if p20 <= -10:  regime = "sector_bear"
        elif p5 <= -5:  regime = "sector_crisis"
        elif p20 <= -5: regime = "sector_weak"
        elif p5 <= -2:  regime = "sector_pullback"
        else:           regime = None   # 不收紧
    except Exception:
        regime = None
    _SECTOR_REGIME_CACHE["data"][sector] = regime
    return regime


def _is_technical_only() -> bool:
    """技术面 only 模式：消息面信号（Trump / breaking_news / 事件日历）
    全部退化为参考，不再注入到决策。

    保留生效的是：
      · regime（VIX/F&G/yield/单日暴跌 — 都是技术/宏观指标，不是消息）
      · confluence + quant_signal（K 线技术）
      · earnings IM guard（期权市场隐含定价，不是新闻）

    默认 ON（env var TECHNICAL_ONLY=0 才关）。
    """
    return os.environ.get("TECHNICAL_ONLY", "1") != "0"


def _scaled_pct(pct: float, ticker: str | None) -> float:
    """把绝对 pct 按杠杆倍数缩放回 1x 等效 — 用于触发阈值判断。
       SOXL -19% / 3 = -6.3% (仍触发 crisis), SOXL -8% / 3 = -2.67% (不触发)。

       兼容两种 ticker 格式：
       - market_watch 返回 ticker="SOXL"（无前缀，line 595 ticker.split(".")[-1]）
       - backtest_engine.build_mkt 返回 ticker="US.SOXL"（带前缀）
       LEVERAGE_FACTORS key 全部带 "US." 前缀，所以无前缀时自动补上。"""
    if pct is None: return 0.0
    if not ticker: return float(pct)
    factor = LEVERAGE_FACTORS.get(ticker)
    if factor is None and not ticker.startswith("US."):
        factor = LEVERAGE_FACTORS.get(f"US.{ticker}")
    return float(pct) / (factor if factor else 1.0)


# ── 10分制评分组件 ─────────────────────────────────────────────────────────────

def _tech_bear(rsi, vol, trend, ma_stack, new_high=False, cci_zone="neutral",
               bb_zone="normal", psar_signal="none",
               macd_signal="none", macd_zone="neutral", adx_zone="weak") -> int:
    """
    技术面看跌分 0-6。
    修复：strong_up 模式下不再将 RSI 阈值抬高到 85——
    3x ETF 在强趋势中更容易过热，需要同等灵敏度。
    新增 Bollinger Bands 上轨突破（极度拉伸）和 PSAR 转空信号。
    """
    strong_up = (trend == "up" and ma_stack == "bull")
    # RSI：统一阈值，不因强趋势而豁免
    rsi_b = 2 if rsi > 82 else 1 if rsi > RSI_OVERBOUGHT else 0
    # 量价背离：新高缩量权重更高
    if strong_up:
        vol_b = 2 if (vol < 0.72 and new_high) else 1 if vol < 0.72 else 0
    else:
        vol_b = 1 if vol < 0.80 else 0
    # 趋势翻空：仅在非强多头时计入（避免强趋势中自相矛盾）
    trend_b = 2 if (trend == "down" and ma_stack == "bear") else 0
    if strong_up:
        trend_b = 0
    # CCI 超买：RSI 已打高分时不重复计分
    cci_b  = 1 if (cci_zone == "overbought" and rsi_b < 2) else 0
    # BB 上轨突破：价格超出均值 ±2σ，统计上有 95% 概率回归
    bb_b   = 2 if bb_zone == "above" else 0
    # PSAR 刚从下方翻到上方：趋势反转信号，比区间信号更及时
    psar_b = 1 if psar_signal == "bear_flip" else 0
    # NOTE: MACD/ADX 试加过, 300d 回测显示反而恶化 alpha 90+ pp - 暂不参与评分
    return min(rsi_b + vol_b + trend_b + cci_b + bb_b + psar_b, 6)


def _tech_bull(rsi, vol, trend, ma_stack, cci_zone="neutral",
               bb_zone="normal", psar_signal="none", pct_chg=0.0,
               macd_signal="none", macd_zone="neutral", adx_zone="weak") -> int:
    """
    技术面看涨分 0-8 (上限提高，配合用户的"追高强趋势"偏好)。
    改动 (5-12 反馈补做): 强 trend 权重加大 + 当日 pct_chg 直接加分。
    """
    strong_up = (trend == "up" and ma_stack == "bull")
    # 强趋势从 2 → 3 分（追高基线）
    trend_b = 3 if strong_up else 0
    if strong_up:
        rsi_b = 1 if (55 <= rsi <= RSI_OVERBOUGHT - 1) else 0
    else:
        rsi_b = 2 if rsi < 30 else 1 if rsi < RSI_OVERSOLD else 0
    vol_b  = 1 if (vol > 1.10 and trend == "up") else 0
    cci_b  = 1 if (cci_zone == "oversold" and rsi_b == 0) else 0
    bb_b   = 1 if bb_zone == "below" else 0
    psar_b = 1 if psar_signal == "bull_flip" else 0
    # 当日强势直接奖励：>=2% 加 1，>=5% 加 2
    p = pct_chg or 0
    pct_b = 2 if p >= 5 else 1 if p >= 2 else 0
    # NOTE: MACD/ADX 同 _tech_bear,300d 回测验证恶化 alpha,暂不参与评分
    return min(trend_b + rsi_b + vol_b + cci_b + bb_b + psar_b + pct_b, 8)


def _macro_bear(macro: dict) -> int:
    """宏观看跌分 0-3：VIX恐慌 / F&G极度贪婪 / 收益率曲线倒挂。"""
    vix    = macro.get("vix")
    fg     = macro.get("fg_score")
    t10y2y = macro.get("t10y2y")
    return min(
        (1 if vix    and vix > 25                      else 0)
        + (1 if fg   and fg  > 75                      else 0)
        + (1 if t10y2y is not None and t10y2y < 0      else 0),
        3
    )


def _macro_bull(macro: dict) -> int:
    """宏观看涨分 0-3：VIX极恐 / F&G极度恐惧 / 收益率曲线健康。"""
    vix    = macro.get("vix")
    fg     = macro.get("fg_score")
    t10y2y = macro.get("t10y2y")
    return min(
        (1 if vix    and vix > 30                      else 0)
        + (1 if fg   and fg  < 25                      else 0)
        + (1 if t10y2y is not None and t10y2y > 0.5    else 0),
        3
    )


def _event_score(days_ev: int, breaking: bool, event_impact: str = "moderate") -> int:
    """事件风险分 0-3。

    结合事件距离 + impact 等级（critical/high/moderate/normal）：
      · critical (FOMC/CPI/NFP)  days≤1 → 2；days≤3 → 1
      · high     (PCE/PPI/Retail) days≤1 → 1；其它  → 0
      · moderate/normal           → 0（不加分）
      · breaking_news 总是 +1

    与旧版差异：旧版不分 impact 等级，retail/PPI 也 days≤1 时给 2 → 触发
    _apply_uncertain_guard 把强多头降 HOLD。新版只 critical 级才给 2，
    high 给 1（不到 ≥2 降级阈值），让暴涨日强信号通过。
    """
    # TECHNICAL_ONLY 模式下仍按真实分数计算，仅在 _etf_rules 里不计入总分。
    # 这样 score_breakdown 仍能展示 event=N 作为参考。
    if event_impact == "critical":
        base = 2 if days_ev <= 1 else (1 if days_ev <= 3 else 0)
    elif event_impact == "high":
        base = 1 if days_ev <= 1 else 0
    else:
        base = 0
    return min(base + (1 if breaking else 0), 3)


def _trump_score(trump_sig: dict | None) -> int:
    """Trump 信号 → 风险分 0-3（**按方向不对称加权**）。

    关键修正（2026-06-15）：bullish extreme（如 Iran Deal 利好）原本被加 risk +2
    导致 _apply_uncertain_guard 把 WATCH_BUY 降级 HOLD —— 利好被当不确定性压制。
    修正后按方向不对称加 risk：
      · bearish + extreme → +2（保护性降级低信心多头，避开地缘冲突等）
      · bearish + large   → +1
      · neutral + extreme → +1（方向不明也是不确定）
      · bullish + extreme → +1（保留少量风险，但不致命：允许 ev=1 不触发 ≥2 降级）
      · bullish + large   → 0（利好不阻止买入）
      · tariff_alert + score<0 → +1（trump-code 历史: SHORT 70% 错，提升不确定）
    """
    # TECHNICAL_ONLY 下仍计算 trump_score 供 banner / 复盘参考，
    # 实际决策路径通过 _apply_uncertain_guard / _apply_trump_override 拦截。
    if not trump_sig or trump_sig.get("fallback"):
        return 0
    mag = trump_sig.get("magnitude", "small")
    direction = trump_sig.get("direction", "neutral")
    score = trump_sig.get("score", 0)
    tariff = trump_sig.get("tariff_alert", False)
    risk = 0
    if direction == "bearish":
        if mag == "extreme":   risk += 2
        elif mag == "large":   risk += 1
    elif direction == "neutral":
        if mag == "extreme":   risk += 1
    # bullish: 利好不加 trump_score 风险分（_apply_trump_override 给 conf+1 加成）
    # 实战示例: 2026-06-15 Iran Deal extreme bullish 时不会被 ev≥2 误降级 HOLD
    if tariff and score < 0:
        risk += 1
    return min(risk, 3)


def _compute_buy_stop_ref(market: dict) -> float | None:
    """BUY/WATCH_BUY 时算保护性 stop_ref（broker 端 SELL STOP 用）。

    取以下技术位中**最高**的（最贴近现价但仍提供保护）：
      · MA20 × 0.97（均线下方 3% buffer）
      · BB 下轨 × 0.99（布林下轨之下 1%）
      · 当前价 × (1 - leverage-scaled buffer)（兜底：杠杆 ETF buffer 更大）

    返回 None 时表示无法算（数据缺失），paper_trader 不挂 stop loss。
    设计：仅在确实有保护性支撑位时才挂；没把握就用 trailing stop 兜底。
    """
    price = market.get("price")
    if not price or price <= 0:
        return None
    candidates = []
    ma20 = market.get("ma20")
    if ma20 and ma20 > 0 and ma20 < price:
        candidates.append(ma20 * 0.97)
    bb_lo = market.get("bb_lower")
    if bb_lo and bb_lo > 0 and bb_lo < price:
        candidates.append(bb_lo * 0.99)
    # 兜底：当前价 × (1 - 缩放 buffer)（杠杆 ETF 给 12%，1x 给 5%）
    lev = LEVERAGE_FACTORS.get(market.get("ticker"))
    if lev is None and market.get("ticker"):
        lev = LEVERAGE_FACTORS.get(f"US.{market.get('ticker')}")
    lev = lev or 1.0
    fallback_buffer = 0.05 * lev  # 1x=5%, 2x=10%, 3x=15%
    candidates.append(price * (1 - fallback_buffer))
    # 取最高（最贴近现价）— 在保护和紧止损间平衡
    stop = max(candidates) if candidates else None
    if stop and stop > 0 and stop < price:
        return round(stop, 2)
    return None


def _apply_trump_override(result: dict, trump_sig: dict | None) -> dict:
    """强 Trump 信号 override：bearish large/extreme 降级低信心多头到 CAUTION；
    bullish large/extreme 给 WATCH_BUY 加 conf+1。"""
    if _is_technical_only():
        return result    # 技术面 only：Trump 不再 override 决策
    if not trump_sig or trump_sig.get("fallback"):
        return result
    if trump_sig.get("magnitude") not in ("large", "extreme"):
        return result
    td = trump_sig.get("direction")
    action = result.get("action")
    conf = result.get("confidence") or 0
    if td == "bearish" and action in BUY_ACTIONS and conf < 7:
        result["action"] = "CAUTION"
        result["trump_override"] = "bearish_strong"
    elif td == "bullish" and action == "WATCH_BUY":
        result["confidence"] = min(conf + 1, 10)
        result["trump_boost"] = True
    return result


def _normalize_confidence(raw_score: float, side: str = "bull") -> int:
    """映射原始分到置信度。

    · 完整模式 (TECHNICAL_ONLY=0)：1-10，round(raw + 1)
    · 技术面 only：
        - 有校准（signals/confidence_calibration.json 存在）→ **分位数映射**
          raw < p20=1，p20-p40=2，p40-p60=3，p60-p80=4，>p80=5
        - 无校准 → 线性 round(raw/2 + 1)
    side: bull / bear（决定查哪组分位）
    """
    if _is_technical_only():
        calib = _load_calibration()
        if calib is not None:
            pkey = f"{side}_percentiles"
            p = calib.get(pkey)
            # 兜底：分位全 0（如 bear 权重全 0 → 历史 bear_weighted 全 0）→ 线性退化
            if p and max(p["p80"], p["p60"], p["p40"], p["p20"]) > 0:
                if raw_score <  p["p20"]: return 1
                if raw_score <  p["p40"]: return 2
                if raw_score <  p["p60"]: return 3
                if raw_score <  p["p80"]: return 4
                return 5
        return max(1, min(round(raw_score / 2 + 1), 5))
    return max(1, min(round(raw_score + 1), 10))


def _conf_scale() -> int:
    """当前置信度量程上限（5 或 10）。给 notifier / UI 显示用。"""
    return 5 if _is_technical_only() else 10


_CALIB_CACHE = {"loaded": False, "data": None}


def _load_calibration() -> dict | None:
    """读校准 JSON（缓存一次，避免每次调用 IO）。"""
    if _CALIB_CACHE["loaded"]:
        return _CALIB_CACHE["data"]
    try:
        from pathlib import Path
        p = Path(__file__).parent / "signals" / "confidence_calibration.json"
        if p.exists():
            _CALIB_CACHE["data"] = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _CALIB_CACHE["data"] = None
    _CALIB_CACHE["loaded"] = True
    return _CALIB_CACHE["data"]


# ── Regime Detection ──────────────────────────────────────────────────────────

def _vol_adjusted(base_pct: float, market: dict) -> float:
    """波动率自适应阈值。base × (daily_vol / 3.0)，cap 2x base 防止极端高 vol 标的彻底失效。
    无 vol_20d 数据 → 退化用杠杆倍数代理（1x→1.0, 2x→1.5, 3x→1.83 大概）。
    daily_vol = vol_20d_annual / sqrt(252)
    """
    import math
    vol_annual = market.get("vol_20d_annual")
    if vol_annual and not math.isnan(vol_annual) and vol_annual > 0:
        daily_vol = vol_annual / (252 ** 0.5)
        thr = base_pct * (daily_vol / 3.0)
        return round(min(thr, base_pct * 2), 2)   # cap 2x base
    # fallback: 用杠杆倍数估算 daily_vol（QQQ 日波 ~1.5%，所以 1x≈1.5, 3x≈4.5）
    lev = LEVERAGE_FACTORS.get(market.get("ticker") or "", 1.0)
    if lev == 1.0 and market.get("ticker") and not market["ticker"].startswith("US."):
        lev = LEVERAGE_FACTORS.get(f"US.{market['ticker']}", 1.0)
    est_daily_vol = 1.5 * lev   # QQQ ~1.5%; TQQQ ~4.5%; MULL ~3%
    return round(base_pct * (est_daily_vol / 3.0), 2)


def _is_overheated(market: dict) -> tuple[bool, str | None]:
    """A+B 方案的"过热"检测（波动率自适应阈值，用户原始 pct 直接比较）。
    满足任一组合 → overheated：
      · 组合 1: 5d 累积 > base20 ×(vol/3) + CCI > 180
      · 组合 2: 10d 累积 > base30 ×(vol/3) + RSI > 68
      · 组合 3: 当日 +8% ×(vol/3) + 前日 +5% ×(vol/3)
    """
    cci    = market.get("cci_20") or 0
    rsi    = market.get("rsi_14") or 50
    pct_today = market.get("pct_chg", 0) or 0
    pct_prev  = market.get("prev_pct", 0) or 0
    cum5  = market.get("cum_5d_pct",  0) or 0
    cum10 = market.get("cum_10d_pct", 0) or 0

    thr5  = _vol_adjusted(20, market)
    thr10 = _vol_adjusted(30, market)
    thr_day  = _vol_adjusted(8, market)
    thr_prev = _vol_adjusted(5, market)

    if cum5 > thr5 and cci > 180:
        return True, f"5d={cum5:.1f}%>{thr5}% + CCI={cci:.0f}>180"
    if cum10 > thr10 and rsi > 68:
        return True, f"10d={cum10:.1f}%>{thr10}% + RSI={rsi:.0f}>68"
    if pct_today > thr_day and pct_prev > thr_prev:
        return True, f"连续暴涨 today={pct_today:.1f}%>{thr_day}% prev={pct_prev:.1f}%>{thr_prev}%"
    return False, None


def _caution_check(market: dict) -> tuple[bool, str | None]:
    """CAUTION 第一层（轻预警，不强制减仓）。
    满足全部：CCI>150 + 5d 累积 > vol_adj(15) + 破 BB 上轨/偏离 MA20>vol_adj(12)。
    """
    cci      = market.get("cci_20") or 0
    cum5     = market.get("cum_5d_pct", 0) or 0
    price    = market.get("price") or 0
    bb_upper = market.get("bb_upper") or 0
    dist_ma  = abs(market.get("dist_from_ma20_pct") or 0)

    thr_cum = _vol_adjusted(15, market)
    thr_dist = _vol_adjusted(12, market)
    if cci > 150 and cum5 > thr_cum and (
            (bb_upper > 0 and price > bb_upper) or dist_ma > thr_dist):
        return True, f"CCI={cci:.0f}>150 + 5d={cum5:.1f}%>{thr_cum}% + " + (
            "破 BB 上轨" if (bb_upper > 0 and price > bb_upper)
            else f"偏离 MA20={dist_ma:.1f}%>{thr_dist}%"
        )
    return False, None


def get_regime(macro: dict, market: dict) -> str:
    """
    市场状态识别。优先级: 单日暴跌 > crisis > overheated > recession_risk > bull_trending > neutral

    单日暴跌检测优先于宏观判断——VIX 与 F&G 反映滞后于价格，
    而 -5% 以上的单日跌幅本身就是即时的 crisis 信号。
    """
    vix      = macro.get("vix") or 20
    fg       = macro.get("fg_score") or 50
    t10y2y   = macro.get("t10y2y")
    trend    = market.get("trend", "neutral")
    ma_stack = market.get("ma_stack", "neutral")
    pct_chg  = market.get("pct_chg", 0) or 0
    pct_zone = market.get("pct_chg_zone", "normal")
    ticker   = market.get("ticker")
    pct_eff  = _scaled_pct(pct_chg, ticker)   # 按杠杆倍数缩放

    # 单日极端波动优先判断（即时信号，不等 VIX 反映）
    # 用缩放 pct: SOXL -19% / 3 = -6.3% 仍触发, SOXL -8% / 3 = -2.7% 不触发
    if pct_eff <= -5:
        return "crisis"
    if pct_zone == "drop" and trend == "down":
        return "recession_risk"
    # 单日 +5%（缩放）→ overheated（保留旧版仅有的触发）
    # A+B 方案的多日累积/双日暴涨 trigger 已回滚（OOS 证实在动量资产上反指标）
    if pct_eff >= 5:
        return "overheated"

    # 宏观判断
    if vix > 30 and trend == "down":
        return "crisis"
    if vix > 20 and fg > 70:
        return "overheated"
    if t10y2y is not None and t10y2y < 0:
        return "recession_risk"
    if vix < 18 and trend == "up" and ma_stack == "bull" and fg < 80:
        return "bull_trending"
    return "neutral"


# ── 市场混乱 (MARKET_UNCERTAIN) 保护 ───────────────────────────────────────
# 当重大事件迫近（event_score >= 2）且 BUY/WATCH_BUY 的 confidence < 8（非
# 高信心信号），把它降级为 HOLD 并标 uncertain=True。理由：低信心的多/空
# 信号在事件不确定的市场里大概率被打脸；同时防止"TQQQ CAUTION + SQQQ
# WATCH_BUY"这种对称矛盾（对方向 ETF 不知道自己是反向的）。
def _apply_uncertain_guard(result: dict, ev: int) -> dict:
    if _is_technical_only():
        return result    # 技术面 only：不让消息面 ev 把多头降级
    if ev < 2:
        return result
    action = result.get("action")
    conf   = result.get("confidence") or 0
    if action not in BUY_ACTIONS:
        return result    # 防御性动作 (REDUCE/SELL) 始终允许
    if conf >= 8:
        return result    # 高信心信号 override 不确定性
    return {
        "action":           "HOLD",
        "confidence":       conf,
        "reason":           f"market_uncertain (event={ev}, {action} conf={conf}<8)",
        "stop_ref":         None,
        "score_breakdown":  result.get("score_breakdown"),
        "demoted_from":     action,
        "uncertain":        True,
    }


def _apply_earnings_guard(result: dict, ticker: str, events: dict) -> dict:
    """关联个股财报临近时，按期权隐含 move 屏蔽 BUY/WATCH_BUY。

    events 期望含 "earnings_implied_move" : {<etf_ticker>: {
        stock, earnings_date, days_to_earnings,
        implied_move_pct, smoothed_implied_move_pct (optional),
        leverage  # ETF 杠杆倍数
    }}
    无该字段时函数无副作用直接返回。

    规则（用 leveraged_im = im * leverage）：
      · days_to_earnings ≤ 1 且 BUY/WATCH_BUY → 强制 HOLD（财报当日不抢仓）
      · leveraged_im > 20%  → 强制 HOLD（极端单日波动，杠杆放大）
      · leveraged_im 12-20% → conf -3，<6 降 HOLD
      · leveraged_im 6-12%  → conf -2
      · leveraged_im < 6%   → 不动
    """
    if not ticker:
        return result
    em_map = events.get("earnings_implied_move") or {}
    em = em_map.get(ticker) or em_map.get(ticker.replace("US.", ""))
    if not em or em.get("error"):
        return result
    action = result.get("action")
    if action not in BULLISH_SIGNAL_ACTIONS:
        return result

    im = em.get("smoothed_implied_move_pct") or em.get("implied_move_pct")
    if not im or im <= 0:
        return result
    lev = float(em.get("leverage") or 1.0)
    leveraged_im = im * lev
    days = em.get("days_to_earnings", 99)
    stock = em.get("stock", "?")
    conf = result.get("confidence") or 0

    # T-1 / T-0 强制屏蔽
    if days <= 1:
        return {
            **result,
            "action":       "HOLD",
            "confidence":   conf,
            "reason":       f"earnings_blackout ({stock} 财报 T-{days}, 跨日 IM={leveraged_im:.1f}%)",
            "stop_ref":     None,
            "demoted_from": action,
            "earnings_guard": True,
        }

    # 高隐含波动强制屏蔽
    if leveraged_im > 20:
        return {
            **result,
            "action":       "HOLD",
            "confidence":   conf,
            "reason":       f"earnings_high_iv ({stock} T-{days}, leveraged_IM={leveraged_im:.1f}% > 20%)",
            "stop_ref":     None,
            "demoted_from": action,
            "earnings_guard": True,
        }

    # 中高 / 中等 隐含波动 → 降信心（阈值按 scale 等比缩放）
    scale = _conf_scale()
    drop_strong = 3 if scale == 10 else 2  # /5 时 -2 等比 /10 时 -3
    drop_mild   = 2 if scale == 10 else 1
    threshold   = scale // 2 + 1           # /10→6 (mid+1), /5→3
    if leveraged_im > 12:
        new_conf = max(0, conf - drop_strong)
        new_action = "HOLD" if new_conf < threshold else action
        return {
            **result,
            "action":       new_action,
            "confidence":   new_conf,
            "reason":       (result.get("reason", "") +
                             f" + earnings_iv_high ({stock} T-{days}, lev_IM={leveraged_im:.1f}%, conf-{drop_strong})").strip(),
            "stop_ref":     result.get("stop_ref") if new_action == action else None,
            "demoted_from": action if new_action != action else result.get("demoted_from"),
            "earnings_guard": True,
        }
    if leveraged_im > 6:
        new_conf = max(0, conf - drop_mild)
        return {
            **result,
            "confidence":   new_conf,
            "reason":       (result.get("reason", "") +
                             f" + earnings_iv_mid ({stock} T-{days}, lev_IM={leveraged_im:.1f}%, conf-{drop_mild})").strip(),
            "earnings_guard": True,
        }
    return result


# ── ETF规则引擎 ───────────────────────────────────────────────────────────────

def _etf_rules(market: dict, events: dict, macro: dict, regime: str = "neutral",
               confluence: dict | None = None, quant: dict | None = None) -> dict:
    rsi      = market.get("rsi_14") or 50
    vol_rat  = market.get("vol_ratio") or 1.0
    new_high = market.get("is_new_52w_high", False)
    trend    = market.get("trend", "up")
    ma_stack = market.get("ma_stack", "bull")
    breaking = events.get("breaking_news", False)
    days_ev  = events.get("days_to_event", 99)
    risk_lvl = events.get("risk_level", "normal")

    strong_up   = (trend == "up" and ma_stack == "bull")
    cci_zone    = market.get("cci_zone",    "neutral")
    bb_zone     = market.get("bb_zone",     "normal")
    psar_signal = market.get("psar_signal", "none")
    macd_signal = market.get("macd_signal", "none")
    macd_zone   = market.get("macd_zone",   "neutral")
    adx_zone    = market.get("adx_zone",    "weak")

    # 优先用 confluence 多空计数（9+ 维信号，覆盖 RSI/CCI/BB/PSAR/MA/量/新高/盘前盘后/15min）
    # 没传则 fallback 到旧的 _tech_bear/_tech_bull（保留兼容性）
    if confluence:
        tb  = confluence.get("bear_count", 0)
        tbu = confluence.get("bull_count", 0)
        # v0.3+ 加权评分（_calibrate_confidence.py 输出），无校准时 weighted == count
        tb_w  = confluence.get("bear_weighted", tb)
        tbu_w = confluence.get("bull_weighted", tbu)
        calibrated = confluence.get("calibrated", False)
    else:
        tb  = _tech_bear(rsi, vol_rat, trend, ma_stack, new_high, cci_zone, bb_zone, psar_signal,
                         macd_signal, macd_zone, adx_zone)
        tbu = _tech_bull(rsi, vol_rat, trend, ma_stack, cci_zone, bb_zone, psar_signal,
                         pct_chg=market.get("pct_chg", 0) or 0,
                         macd_signal=macd_signal, macd_zone=macd_zone, adx_zone=adx_zone)
        tb_w, tbu_w = tb, tbu
        calibrated = False
    mb  = _macro_bear(macro)
    mbu = _macro_bull(macro)
    ev  = _event_score(days_ev, breaking, events.get("next_event_impact", "moderate"))
    qb_bear = (quant or {}).get("sell_score", 0)
    qb_bull = (quant or {}).get("buy_score",  0)

    # 追高强趋势 boost (用户 5-12 偏好补做)：
    #  A) strong trend + ma 多排时给 bull +1
    #  B) 当日 pct_chg ≥ 2% +1，≥ 5% +2
    bull_boost = 0
    if strong_up:
        bull_boost += 1
    # 按杠杆缩放 pct: SOXL +5% (1σ 普通日) 不再给满 boost, +15% 才给
    pct_eff_b = _scaled_pct(market.get("pct_chg", 0), market.get("ticker"))
    if   pct_eff_b >= 5: bull_boost += 2
    elif pct_eff_b >= 2: bull_boost += 1

    # TECHNICAL_ONLY: event 分数仍计算（用于 score_breakdown 展示），
    # 但不再计入 bear 总分；breaking_news 分支也跳过。
    tech_only = _is_technical_only()

    # v0.3+ 置信度计算输入：TECH_ONLY + 已校准时，用加权 tech raw（带分位映射）；
    # 否则沿用旧逻辑（不加权 bull/bear 总分）。
    def _conf_input(unweighted_total: float, weighted_tech: float) -> float:
        if tech_only and calibrated:
            return weighted_tech
        return unweighted_total
    ev_in_total = 0 if tech_only else ev
    bear = tb + mb + ev_in_total + qb_bear
    bull = tbu + mbu + qb_bull + bull_boost

    # 突发新闻 → 仅在技术面无明显方向时观望；极端技术信号优先于新闻
    if breaking and not tech_only:
        # 极端看跌（技术bear ≥3 + 任一极端指标）→ 仍 REDUCE
        if tb >= 3 and (rsi > RSI_OVERBOUGHT or bb_zone == "above"):
            return {"action": "REDUCE",
                    "confidence": _normalize_confidence(tb + ev + 1),
                    "reason": "breaking news + tech extreme bear", "stop_ref": None}
        # 极端看涨（技术bull ≥3 + RSI极度超卖）→ 不接刀
        if tbu >= 3 and rsi < 30:
            return {"action": "HOLD",
                    "confidence": _normalize_confidence(tbu + 1),
                    "reason": "oversold but news uncertain", "stop_ref": None}
        return {"action": "HOLD",
                "confidence": _normalize_confidence(2 + ev),
                "reason": "breaking news", "stop_ref": None}

    # ── V 型反转追买（大跌后强反弹） ──────────────────────────────────────────
    # 触发：昨日盘中暴跌 ≤ -3%（1x 缩放后）+ 今日已涨 ≥ +2% + RSI<45 + 共振 ≥ 3
    # 设计意图：MA/PSAR 还没翻多但反弹已成事实，绕开 trend=down 死锁
    # action 分流（基于杠杆 + 5d/20d 历史 hit rate）：
    #   · 1x ETF (GLD)：WATCH_BUY — paper_trader 正常下单（5d 100% / +3% avg）
    #   · ≥1.5x 杠杆 ETF：WATCH_BUY_LONG_HOLD — paper_trader **不**下单
    #     仅在 notifier/Claude 解读里显示，用户手动决定 20d+ 长持仓
    #     原因：3x ETF V 反弹 5d hit 33% / -2.95% avg（死猫弹），但 20d 58% / +28% avg
    #     这是"系统提示但不主动入场"的信号 — 由用户基于全局判断
    # 排除：breaking_news / 强 Trump 信号 / RSI 极端
    from config import LEVERAGE_FACTORS
    _lev = LEVERAGE_FACTORS.get(market.get("ticker", ""), 1.0)
    prev_pct_scaled = _scaled_pct(market.get("prev_pct", 0) or 0, market.get("ticker"))
    trump_mag = (events.get("trump_signal") or {}).get("magnitude", "small")
    # TECH_ONLY 下不让 breaking_news / trump_mag 阻断 V 反弹（消息面只参考）
    breaking_block = breaking and not tech_only
    trump_block    = trump_mag in ("large", "extreme") and not tech_only
    if (not breaking_block
        and not trump_block
        and prev_pct_scaled <= -3.0
        and pct_eff_b >= 2.0
        and rsi < 45
        and rsi > 20):
        # 评分阈值按杠杆分流：1x 要求更严（避免误抄底），杠杆 ETF 较松（让 LONG_HOLD 触发）
        # 因为杠杆 ETF V 反弹后 RSI 常回升到 35-45（不再 <35），共振分难凑齐
        bounce_score = 1  # base
        if rsi < 35:                          bounce_score += 1
        if cci_zone == "oversold":            bounce_score += 1
        if vol_rat > 1.2:                     bounce_score += 1
        if bb_zone in ("below", "lower"):     bounce_score += 1
        # 1x ETF 严格 (≥3)：避免假反弹下单；杠杆 ETF 宽松 (≥2)：触发 LONG_HOLD 给用户提示
        score_threshold = 3 if _lev <= 1.5 else 2
        if bounce_score >= score_threshold:
            # 1x ETF → WATCH_BUY；杠杆 ETF → WATCH_BUY_LONG_HOLD（不下单）
            action = "WATCH_BUY" if _lev <= 1.5 else "WATCH_BUY_LONG_HOLD"
            lev_tag = "1x" if _lev <= 1.5 else f"{_lev:.0f}x"
            hold_hint = "" if _lev <= 1.5 else " | 建议持 20d+（5d 历史 hit 33%，20d 58%）"
            # V 反弹 stop_ref：紧贴前日 low（如有）或当前价 -5%×杠杆
            v_stop = _compute_buy_stop_ref(market)
            return {"action": action,
                    "confidence": _normalize_confidence(bull + bounce_score + 2),
                    "reason": (f"V-bounce({lev_tag}): prev={prev_pct_scaled:+.1f}% "
                               f"today={pct_eff_b:+.1f}% RSI={rsi:.0f} score={bounce_score}"
                               f"{hold_hint}"),
                    "stop_ref": v_stop,
                    "score_breakdown": {"bounce": bounce_score,
                                        "prev_pct_eff": prev_pct_scaled,
                                        "today_pct_eff": pct_eff_b,
                                        "rsi": rsi,
                                        "bull_raw": bull,
                                        "leverage": _lev}}

    # 极强量价背离（趋势无关，分发信号）
    if rsi > 82 and vol_rat < 0.72 and new_high:
        return {"action": "REDUCE", "confidence": _normalize_confidence(bear + 2),
                "reason": "new high + vol diverge + RSI extreme", "stop_ref": None}

    cum_5d = market.get("cum_5d_pct", 0) or 0   # 5 天累计涨跌 %

    # ── Crisis V-bounce Probe（extreme oversold + strong technical confluence 例外）──
    # 昨日 2026-07-30 遗漏 SOXL +24% / MULL +28% 反弹的直接原因：crisis regime
    # bull_thresh=6 屏蔽了 bull_count=5 的强多头共振。
    #
    # 例外条件（严格 AND）：
    #   1. regime = crisis (被极端保护屏蔽了)
    #   2. RSI < 32 （极端超卖）
    #   3. bull_count >= 4 （强技术共振，即使不满 bull_thresh=6）
    #   4. cum_5d <= -12% （确认深跌，不是伪跌破）
    #   5. pct_eff_b >= -1% (今日至少不再深跌)
    #   6. vol_rat < 1.5 （不是恐慌卖出中，缩量止跌 / 温和拉起）
    #   7. env var CRISIS_VBOUNCE_ENABLED=1（feature flag；run/watchdog 默认开启）
    # 输出：WATCH_BUY_PROBE (paper_trader 按 30% 常规仓位; 严格 stop)
    # 历史回测见 _backtest_crisis_vbounce.py；样本较小，因此保持 probe 极小仓位。
    if (os.environ.get("CRISIS_VBOUNCE_ENABLED") == "1"
            and regime == "crisis"
            and rsi < 32
            and bull >= 4
            and cum_5d <= -12
            and pct_eff_b >= -1
            and vol_rat < 1.5):
        v_stop = _compute_buy_stop_ref(market)
        return {"action": "WATCH_BUY_PROBE",
                # Probe 必须正好落在常规交易窗口门槛：默认 /5 为 3，完整 /10 为 6。
                # 不走 _normalize_confidence(3)，否则无校准的 /5 模式会被 round 成 2。
                "confidence": int(confidence_min("post-open", _conf_scale())),
                "reason": (f"crisis-vbounce probe: RSI={rsi:.0f} bull={bull} "
                           f"cum_5d={cum_5d:+.1f}% today={pct_eff_b:+.1f}% "
                           f"vol={vol_rat:.2f} (未回测，probe 30% 仓位)"),
                "stop_ref": v_stop,
                "score_breakdown": {"crisis_vbounce": True, "rsi": rsi, "bull": bull,
                                    "cum_5d": cum_5d, "pct_today": pct_eff_b}}

    # 阈值由 regime 决定（基于新的 confluence 计数范围调整）
    # bull_trending 适度放宽；crisis 极敏感
    _RT = {
        "bull_trending":  (6, 4, 3),   # 牛市延续：追趋势，bull≥3 即可关注买入
        "bull_extended":  (99, 99, 2), # 子类: 强动量延续 → REDUCE/CAUTION 全 disable，bull≥2 即买
        "bull_pulling":   (99, 99, 2), # 子类: 回调买入 bull → REDUCE 也 disable，dip 都是 BUY 机会
        "bull_chop":      (5, 3, 5),   # 子类: 波动放大 → 稍严，要求更高 bull conf
        "overheated":     (4, 3, 5),   # 过热：bear≥4 减仓，bull≥5 才买
        "recession_risk": (3, 2, 5),   # 衰退风险：快速减仓
        "crisis":         (2, 1, 6),   # 危机：极敏感，几乎不买
    }
    if regime in _RT:
        reduce_thresh, caution_thresh, bull_thresh = _RT[regime]
    else:  # neutral
        reduce_thresh  = 5 if strong_up else 4
        caution_thresh = 4 if strong_up else 2
        bull_thresh    = 3

    # ── HMM meta-regime（P4.3）：单向收紧 bull_thresh，不放宽 ────────────────
    # 仅在 HMM 显示"波动/危机/熊"状态时让 BUY 更难触发；不会因 HMM 而放宽。
    hmm_state = _get_hmm_meta_state()
    if hmm_state in ("volatile_uncertain", "crisis", "bear_or_correction"):
        bull_thresh += 1
        caution_thresh = max(1, caution_thresh - 1)  # 同时让 CAUTION 更敏感

    # ── 板块级 regime（比大盘更贴合具体标的）：单向收紧 bull_thresh ─────
    # 例：SOXL/DRAM/MULL 跟 SMH 半导体，半导体技术熊时 → bull_thresh +1
    sector_regime = _get_sector_regime(market.get("ticker", ""))
    if sector_regime in ("sector_bear", "sector_crisis", "sector_weak"):
        bull_thresh += 1
        caution_thresh = max(1, caution_thresh - 1)

    # 模拟仓积极模式：抵消 HMM + sector 的重复收紧，让 2 个以上多头共振先产生
    # WATCH_BUY 小试探信号。事件/财报 guard 仍在 get_decision() 末尾执行。
    if is_sim_active_trading() and regime != "crisis":
        bull_thresh = max(2, bull_thresh - 2)

    if bear >= reduce_thresh:
        # 修复 #1: bull_trending 下 REDUCE 信号回测 14-25% 胜率（反指标）。
        # 只在【真正极端】条件下放行 REDUCE，否则降级为 CAUTION：
        #   · RSI > 85 (强超买)
        #   · 突破布林上轨 ±2σ
        #   · 当日暴跌 ≤ -5%（crash）
        #   · 突发新闻
        # 这 4 个里任一满足才允许 REDUCE。
        if regime in ("bull_trending", "bull_chop"):
            extreme_ok = (rsi > 85 or bb_zone == "above"
                          or _scaled_pct(market.get("pct_chg", 0), market.get("ticker")) <= -5
                          or events.get("breaking_news", False))
            if not extreme_ok:
                return {"action": "CAUTION",
                        "confidence": _normalize_confidence(_conf_input(bear, tb_w), side="bear"),
                        "reason": "bull_trending: REDUCE 信号但非极端，降级 CAUTION (反指标修复)",
                        "stop_ref": None,
                        "score_breakdown": {"tech": tb, "tech_weighted": tb_w,
                                            "macro": mb, "event": ev,
                                            "quant": qb_bear, "raw": bear,
                                            "downgraded_from_REDUCE": True,
                                            "calibrated": calibrated}}
        reason = ("RSI extreme + vol shrink" if vol_rat < 0.80 and rsi > 82
                  else "RSI extreme + vol divergence" if rsi > 82
                  else "RSI overbought" if rsi > RSI_OVERBOUGHT
                  else "downtrend confirmed")
        return {"action": "REDUCE",
                "confidence": _normalize_confidence(_conf_input(bear, tb_w), side="bear"),
                "reason": reason, "stop_ref": None,
                "score_breakdown": {"tech": tb, "tech_weighted": tb_w, "macro": mb,
                                    "event": ev, "quant": qb_bear, "raw": bear,
                                    "event_in_total": ev_in_total, "calibrated": calibrated}}

    if bear >= caution_thresh and bear > bull:
        if rsi > 82 and vol_rat < 0.80:
            reason = "RSI extreme + vol shrink"
        elif rsi > RSI_OVERBOUGHT:
            reason = "RSI overbought"
        elif days_ev <= 1 and risk_lvl == "high":
            reason = "major event tomorrow"
        else:
            reason = "downtrend confirmed"
        return {"action": "CAUTION",
                "confidence": _normalize_confidence(_conf_input(bear, tb_w), side="bear"),
                "reason": reason, "stop_ref": None,
                "score_breakdown": {"tech": tb, "tech_weighted": tb_w, "macro": mb,
                                    "event": ev, "quant": qb_bear, "raw": bear,
                                    "event_in_total": ev_in_total, "calibrated": calibrated}}

    if days_ev <= 1 and risk_lvl == "high" and not tech_only:
        return {"action": "HOLD",
                "confidence": _normalize_confidence(max(bear, 2)),
                "reason": "major event tomorrow", "stop_ref": None}

    # 看涨评分
    if bull >= bull_thresh:
        if strong_up and rsi >= 55:
            reason = "bullish trend + momentum"
        elif rsi < RSI_OVERSOLD:
            reason = "oversold + uptrend"
        else:
            reason = "uptrend + positive confluence"
        return {"action": "WATCH_BUY",
                "confidence": _normalize_confidence(_conf_input(bull, tbu_w), side="bull"),
                "reason": reason,
                "stop_ref": _compute_buy_stop_ref(market),
                "score_breakdown": {"tech": tbu, "tech_weighted": tbu_w,
                                    "macro": mbu, "event": ev, "quant": qb_bull,
                                    "boost": bull_boost, "raw": bull,
                                    "event_in_total": ev_in_total,
                                    "calibrated": calibrated}}

    return {"action": "HOLD",
            "confidence": _normalize_confidence(_conf_input(max(bear, bull), max(tb_w, tbu_w)),
                                                 side="bull" if tbu_w >= tb_w else "bear"),
            "reason": "no clear signal", "stop_ref": None,
            "score_breakdown": {"bear_raw": bear, "bull_raw": bull,
                                "tech_weighted_bull": tbu_w, "tech_weighted_bear": tb_w,
                                "quant_bear": qb_bear, "quant_bull": qb_bull,
                                "calibrated": calibrated}}


# ── 黄金规则引擎 ──────────────────────────────────────────────────────────────

def _gold_rules(market: dict, events: dict, macro: dict, regime: str = "neutral",
                quant: dict | None = None) -> dict:
    rsi      = market.get("rsi_14") or 50
    vol_rat  = market.get("vol_ratio") or 1.0
    new_high = market.get("is_new_52w_high", False)
    trend    = market.get("trend", "up")
    ma_stack = market.get("ma_stack", "bull")
    breaking = events.get("breaking_news", False)
    bias     = events.get("gold_bias", "neutral")
    days_ev  = events.get("days_to_event", 99)
    risk_lvl = events.get("risk_level", "normal")
    resist   = market.get("resistance")
    support  = market.get("support")
    price    = market.get("price", 0)
    cci_zone    = market.get("cci_zone",    "neutral")
    bb_zone     = market.get("bb_zone",     "normal")
    psar_signal = market.get("psar_signal", "none")
    macd_signal = market.get("macd_signal", "none")
    macd_zone   = market.get("macd_zone",   "neutral")
    adx_zone    = market.get("adx_zone",    "weak")

    tb  = _tech_bear(rsi, vol_rat, trend, ma_stack, new_high, cci_zone, bb_zone, psar_signal,
                     macd_signal, macd_zone, adx_zone)
    tbu = _tech_bull(rsi, vol_rat, trend, ma_stack, cci_zone, bb_zone, psar_signal,
                     macd_signal=macd_signal, macd_zone=macd_zone, adx_zone=adx_zone)
    mb  = _macro_bear(macro)
    mbu = _macro_bull(macro)
    ev  = _event_score(days_ev, breaking, events.get("next_event_impact", "moderate"))
    qb_bear = (quant or {}).get("sell_score", 0)
    qb_bull = (quant or {}).get("buy_score",  0)

    # 危机/衰退期黄金避险需求增加；风险偏好高时黄金承压
    regime_bull = 1 if regime in ("crisis", "recession_risk") else 0
    regime_bear = 1 if regime == "overheated" else 0
    bear = tb + mb + ev + regime_bear + qb_bear
    bull = tbu + mbu + regime_bull + qb_bull
    # 注：gold_macro 评分注入已尝试 + 回测退化 -6~9%（_backtest_gold_macro.py）
    # 保留 events.gold_bias 来源升级（驱动自宏观），但不直接加 bull/bear 分

    # 突发新闻 → 偏向优先
    if breaking and bias == "bullish":
        return {"action": "BUY",  "confidence": min(bull + ev + 2, 10),
                "reason": "breaking bullish catalyst for gold",
                "entry_ref": price, "stop_ref": support}
    if breaking and bias == "bearish":
        return {"action": "SELL", "confidence": min(bear + 1, 10),
                "reason": "breaking bearish catalyst for gold",
                "entry_ref": price, "stop_ref": resist}
    if breaking:
        return {"action": "HOLD", "confidence": min(5 + ev, 10),
                "reason": "breaking news — wait for direction",
                "entry_ref": None, "stop_ref": None}

    # 极强卖出
    if rsi > 80 and vol_rat < 0.75:
        return {"action": "SELL", "confidence": min(bear + 1, 10),
                "reason": "RSI extreme + vol divergence",
                "entry_ref": price, "stop_ref": resist}
    if rsi > 76 and new_high and bias != "bullish":
        return {"action": "SELL", "confidence": bear,
                "reason": "new high overbought, no bullish catalyst",
                "entry_ref": price, "stop_ref": resist}

    # 看跌评分 (SELL>=5, CAUTION_SELL>=2)
    if bear >= 5:
        return {"action": "SELL", "confidence": bear,
                "reason": "RSI overbought" if rsi > 74 else "bearish news + downtrend",
                "entry_ref": price, "stop_ref": resist}
    if bear >= 2:
        return {"action": "SELL", "confidence": bear,
                "reason": "RSI overbought" if rsi > 74 else "downtrend confirmed",
                "entry_ref": price, "stop_ref": resist}
    if days_ev <= 1 and risk_lvl == "high":
        return {"action": "HOLD", "confidence": max(bear + 1, 2),
                "reason": "major event tomorrow",
                "entry_ref": None, "stop_ref": None}

    # 看涨评分 (BUY>=3)
    if rsi < 28 and bias == "bullish":
        return {"action": "BUY",  "confidence": min(bull + 2, 10),
                "reason": "extreme oversold + bullish catalyst",
                "entry_ref": price, "stop_ref": support}
    if bull >= 4:
        return {"action": "BUY",  "confidence": bull,
                "reason": "RSI extreme oversold" if rsi < 30 else "oversold + uptrend",
                "entry_ref": price, "stop_ref": support}
    if bull >= 3:
        return {"action": "BUY",  "confidence": bull,
                "reason": "oversold + uptrend",
                "entry_ref": price, "stop_ref": support}

    # 新闻偏向
    if bias == "bearish" and trend == "down":
        return {"action": "SELL", "confidence": max(bear, 3),
                "reason": "bearish news + downtrend",
                "entry_ref": price, "stop_ref": resist}
    if bias == "bullish" and trend == "up":
        return {"action": "BUY",  "confidence": max(bull, 3),
                "reason": "bullish news + uptrend",
                "entry_ref": price, "stop_ref": support}

    return {"action": "HOLD", "confidence": max(bear, bull, 1),
            "reason": "no clear signal",
            "entry_ref": None, "stop_ref": None}


# ── LLM调用（有API Key时使用）──────────────────────────────────────────────────

_ETF_SYSTEM = """You are a 3x leveraged ETF risk signal generator. Output ONLY valid JSON, no prose.
Confidence is now 1-10 (not 1-5). Use the full range.
Signal: REDUCE(bear>=7), CAUTION(bear>=5), WATCH_BUY(bull>=5), HOLD(default).
Output: {"action":"...","confidence":1-10,"reason":"max 10 words","stop_ref":null}"""

_GOLD_SYSTEM = """You are a COMEX Gold signal generator. Output ONLY valid JSON, no prose.
Confidence is now 1-10. Gold: rises on inflation/geopolitical/weak USD/rate cuts.
Output: {"action":"BUY|SELL|HOLD","confidence":1-10,"reason":"max 10 words","entry_ref":null,"stop_ref":null}"""


def _llm_call(system: str, market: dict, events: dict, macro: dict,
              m_keys: tuple, e_keys: tuple) -> dict | None:
    if not OPENAI_API_KEY:
        return None
    payload = json.dumps({
        "m": {k: market[k] for k in m_keys if k in market},
        "e": {k: events[k] for k in e_keys if k in events},
        "macro": {k: v for k, v in macro.items() if v is not None},
    }, separators=(",", ":"))
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=DECISION_MODEL,
            max_tokens=100,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": payload},
            ],
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return None


# ── 公开API ───────────────────────────────────────────────────────────────────

def get_decision(market: dict, events: dict, macro: dict | None = None,
                 confluence: dict | None = None, quant: dict | None = None,
                 board_regime: str | None = None) -> dict:
    """
    ETF风险信号: REDUCE / HOLD / CAUTION / WATCH_BUY。置信度 1-10。
    confluence:   共振模块输出（含 bull_count/bear_count），传入后作为主技术评分源。
    quant:        进化规则共振（buy_score/sell_score 0-3），自动加载，可显式传入。
    board_regime: 当日板块级 regime（由 regime_today 算定）。**单一源**：
                  始终使用 board_regime，**唯一允许的 override** 是单股当日暴跌
                  ≤ -5%（杠杆缩放后）→ 该 ticker override 成 "crisis"，全局 board
                  不变。board_regime=None（真未拿到）才 fallback per-ticker。
    """
    macro = macro or {}
    if board_regime is None:
        try:
            from regime_today import get_today_regime
            board_regime = get_today_regime()
        except Exception:
            board_regime = None
    if board_regime:
        pct_eff_t = _scaled_pct(market.get("pct_chg", 0) or 0, market.get("ticker"))
        regime = "crisis" if pct_eff_t <= -5 else board_regime
    else:
        regime = get_regime(macro, market)
    # 若调用方没传 confluence，本地算一次（避免显示错位）
    if confluence is None:
        try:
            from confluence import get_confluence
            confluence = get_confluence(market)
        except Exception:
            confluence = None
    if quant is None:
        try:
            from quant_signal import evaluate as _eval_quant
            quant = _eval_quant(market.get("ticker", "?"), market)
        except Exception:
            quant = None
    result = _llm_call(
        _ETF_SYSTEM, market, events, macro,
        m_keys=("ticker", "price", "pct_chg", "rsi_14", "vol_ratio",
                "is_new_52w_high", "trend", "ma_stack"),
        e_keys=("next_event", "days_to_event", "breaking_news", "risk_level"),
    )
    if result is None:
        result = _etf_rules(market, events, macro, regime, confluence, quant)
        result["engine"] = "rules"
    else:
        result["engine"] = "llm"
    result["regime"] = regime
    result["quant"]  = quant
    # MARKET_UNCERTAIN 保护：高事件不确定性下，低信心 BUY/WATCH_BUY 降级为 HOLD
    # Trump 信号也通过 ev 通道注入（_trump_score 加到 0-3 范围内）
    trump_sig = events.get("trump_signal")
    ev = min(
        _event_score(events.get("days_to_event", 99), events.get("breaking_news", False),
                     events.get("next_event_impact", "moderate"))
        + _trump_score(trump_sig),
        3,
    )
    result = _apply_uncertain_guard(result, ev)
    result = _apply_earnings_guard(result, market.get("ticker", ""), events)
    result = _apply_trump_override(result, trump_sig)
    return result


def get_gold_decision(market: dict, events: dict, macro: dict | None = None,
                      quant: dict | None = None, board_regime: str | None = None) -> dict:
    """黄金方向信号: BUY / SELL / HOLD。置信度1-10。
    board_regime: 同 get_decision；**单一源**，board≠None 时始终采用，唯一 override
                  是单股 ≤-5% 时 → crisis。"""
    macro = macro or {}
    if board_regime is None:
        try:
            from regime_today import get_today_regime
            board_regime = get_today_regime()
        except Exception:
            board_regime = None
    if board_regime:
        pct_eff_t = _scaled_pct(market.get("pct_chg", 0) or 0, market.get("ticker"))
        regime = "crisis" if pct_eff_t <= -5 else board_regime
    else:
        regime = get_regime(macro, market)
    if quant is None:
        try:
            from quant_signal import evaluate as _eval_quant
            quant = _eval_quant(market.get("ticker", "?"), market)
        except Exception:
            quant = None
    result = _llm_call(
        _GOLD_SYSTEM, market, events, macro,
        m_keys=("ticker", "price", "pct_chg", "rsi_14", "vol_ratio", "is_new_52w_high",
                "trend", "ma_stack", "atr_14", "resistance", "support"),
        e_keys=("next_event", "days_to_event", "breaking_news", "risk_level", "gold_bias"),
    )
    if result is None:
        result = _gold_rules(market, events, macro, regime, quant)
        result["engine"] = "rules"
    else:
        result["engine"] = "llm"
    result["regime"] = regime
    result["quant"]  = quant
    # MARKET_UNCERTAIN 同样适用于黄金 BUY 信号；Trump 信号也注入 ev
    trump_sig = events.get("trump_signal")
    ev = min(
        _event_score(events.get("days_to_event", 99), events.get("breaking_news", False),
                     events.get("next_event_impact", "moderate"))
        + _trump_score(trump_sig),
        3,
    )
    result = _apply_uncertain_guard(result, ev)
    result = _apply_trump_override(result, trump_sig)
    return result
