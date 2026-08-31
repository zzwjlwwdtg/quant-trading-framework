"""
RegimeToday — 系统级"今日 regime"单一源。

设计原则（5-29 用户原话："重点是决策矩阵吧，知道现在是那种 regime，然后
提示，根据这个为前提然后给决策"）：
  · 每天 pre-open **算一次**，写入 regime_state.json
  · 所有下游（universe_picker / decision_agent / pca_sox / notifier）都读它
  · 决策的"前提" — 先告诉用户今天什么 regime，再讲规则

输入用**板块级**而不是单股：
  · SOX 28 只成分股的 MKT 等权收益（5/20 日均 + 今日）
  · 宏观: VIX, F&G, T10Y2Y
  · SPY 当日涨跌（broad-market sanity）

输出同时保留三层含义：
  · HMM（另一个文件）= 慢周期宏观背景，不是买入信号
  · base_regime = 日线方向前提
  · regime = 叠加短线风格后的实际交易前提

decision_agent.get_decision 接 board_regime=... 后用本日 regime；
只有单股**当日暴跌 ≤ -5%** 才被允许 override 成 crisis（保护该单股）。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from notifier import logger
from atomic_io import atomic_write_json
from market_style import analyze_price_style, effective_board_regime


REGIME_STATE_PATH = Path(__file__).parent / "regime_state.json"
ET = ZoneInfo("America/New_York")
_STALE_WARNED_FOR: str | None = None


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _market_date(now: datetime | None = None) -> str:
    """Return the trading date this process should reason about, in ET.

    After the US after-hours session ends, the next actionable session is the
    next weekday. This prevents a late-night restart from silently reusing
    yesterday's regime as tomorrow's premise.
    """
    now_et = now.astimezone(ET) if now else datetime.now(ET)
    d = now_et.date()
    if now_et.weekday() >= 5:
        d = _next_weekday(d)
    elif now_et.hour >= 20:
        d = _next_weekday(d + timedelta(days=1))
    return d.isoformat()

# 与 decision_agent.get_regime 的判定一致，把板块涨跌映射到 zone
def _pct_zone(p: float) -> str:
    if p <= -5: return "crash"
    if p <= -2: return "drop"
    if p <= -1: return "mild_drop"
    if p >=  5: return "surge"
    if p >=  2: return "pop"
    if p >=  1: return "mild_pop"
    return "normal"


def _fetch_spy_today_pct() -> Optional[float]:
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(period="5d", interval="1d")
        if len(h) < 2: return None
        return float((h["Close"].iloc[-1] - h["Close"].iloc[-2]) / h["Close"].iloc[-2] * 100)
    except Exception:
        return None


def _fetch_spy_context() -> dict:
    """一次拉取 SPY，返回位置、ATR 与独立短线风格。"""
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(period="80d", interval="1d")
        if len(h) < 21:
            return {}
        style = analyze_price_style(h["Close"], h["High"], h["Low"])
        metrics = style.get("metrics") or {}
        return {
            "spy_extension_pct": metrics.get("dist_ma50_pct"),
            "spy_dist_ma20_pct": metrics.get("dist_ma20_pct"),
            "spy_atr_ratio": metrics.get("atr_5_20_ratio"),
            "short_style": style,
        }
    except Exception as e:
        logger.warning(f"[regime] SPY 短线风格拉取失败: {e}")
        return {}


def _build_board_inputs() -> dict:
    """聚合 SOX 板块因子 + 宏观 + SPY。失败的字段填 None。"""
    out = {
        "sox_mkt_today_pct": None, "sox_mkt_5d_pct": None, "sox_mkt_20d_pct": None,
        "spy_today_pct":     None,
        "vix": None, "fg": None, "t10y2y": None,
    }
    # SOX 板块因子
    try:
        from pca_sox import fetch_returns, compute_factors
        returns = fetch_returns()
        if returns is not None and not returns.empty:
            factors = compute_factors(returns)
            mkt = factors["MKT"]
            out["sox_mkt_today_pct"] = float(mkt.iloc[-1] * 100)
            out["sox_mkt_5d_pct"]    = float(mkt.tail(5).mean() * 100)
            out["sox_mkt_20d_pct"]   = float(mkt.tail(20).mean() * 100)
    except Exception as e:
        logger.warning(f"[regime] SOX 因子拉取失败: {e}")
    # SPY 当日 + 子类指标 + 短线风格（与 HMM 慢周期严格分开）
    out["spy_today_pct"] = _fetch_spy_today_pct()
    out.update(_fetch_spy_context())
    # 宏观
    try:
        from data_feeds import fetch_vix, fetch_fear_greed
        out["vix"] = fetch_vix()
        out["fg"]  = (fetch_fear_greed() or {}).get("score")
    except Exception as e:
        logger.warning(f"[regime] VIX/F&G 拉取失败: {e}")
    try:
        from fred_feeds import fetch_fred
        out["t10y2y"] = (fetch_fred().get("T10Y2Y") or {}).get("value")
    except Exception as e:
        logger.warning(f"[regime] FRED 拉取失败: {e}")
    return out


def _classify(inp: dict) -> str:
    """
    板块级输入 → 交易 regime。
    base: bull_trending / overheated / recession_risk / risk_off / crisis / neutral
    Layer 1 新增 bull 3 子类:
      bull_extended : 现价远高于 MA50 (>10% extension)，强动量延续，**完全 disable REDUCE**
      bull_pulling  : 现价靠近 MA20 (回调买入区)，dip = BUY 机会
      bull_chop     : 宏观/日线偏多，但短线多项震荡指标共振
      neutral_chop  : 日线中性且短线震荡，收紧加仓并降低目标波动
    """
    sox_today = inp.get("sox_mkt_today_pct") or 0
    spy_today = inp.get("spy_today_pct")     or 0
    sox_5d    = inp.get("sox_mkt_5d_pct")    or 0
    sox_20d   = inp.get("sox_mkt_20d_pct")   or 0

    board_today = min(sox_today, spy_today) if spy_today else sox_today
    macro = {"vix": inp.get("vix"), "fg_score": inp.get("fg"),
             "t10y2y": inp.get("t10y2y")}
    board_mkt = {
        "trend":    "up"   if sox_20d > 0      else "down",
        "ma_stack": "bull" if sox_5d  > sox_20d else "bear",
        "pct_chg":  board_today,
        "pct_chg_zone": _pct_zone(board_today),
    }
    try:
        from decision_agent import get_regime
        base = get_regime(macro, board_mkt)
    except Exception as e:
        logger.warning(f"[regime] classify 失败: {e}")
        return "neutral"

    # ``get_regime`` 的历史规则会把“单日跌幅<-2% + 趋势向下”也命名成
    # recession_risk。这里是系统级市场标签，必须区分价格风险与宏观衰退：
    # 曲线未倒挂时，只能称为 risk_off，不能误导成宏观衰退判断。
    if (base == "recession_risk" and (inp.get("t10y2y") is None or inp.get("t10y2y") >= 0)
            and board_mkt["pct_chg_zone"] == "drop" and board_mkt["trend"] == "down"):
        base = "risk_off"

    # 短线震荡是交易层 overlay，不改写 HMM 的慢周期背景。
    short_style = inp.get("short_style") or {}
    effective = effective_board_regime(base, short_style)
    if effective != base:
        return effective

    # 只有非震荡 bull_trending 才继续拆趋势位置子类。
    if base != "bull_trending":
        return base

    # 用 SPY 的位置 + 波动估算子类
    try:
        spy_extension = inp.get("spy_extension_pct")  # 现价 vs MA50
        if spy_extension is not None and spy_extension > 10:
            return "bull_extended"   # 强动量延续
        dist_ma20 = inp.get("spy_dist_ma20_pct")
        sox_20d   = inp.get("sox_mkt_20d_pct") or 0
        if dist_ma20 is not None and abs(dist_ma20) < 3:
            # 只有 20 天动量仍正（SOX 20d > 0），才算真回调而非崩盘穿越 MA20。
            # 回测证据: 2026-06-08 期间 17 个 bull_pulling 样本 avg -36.56%，
            # 因为 SOX 从 ATH 回落时会穿过 MA20，那一瞬被误标"回调低吸区"。
            if sox_20d > 0:
                return "bull_pulling"    # 接近 MA20 且慢周期上行 = 真回调
    except Exception:
        pass
    return "bull_trending"           # 默认还是粗 bull


def detect_and_save_regime() -> dict:
    """pre-open 调用：算一次 → 写文件 → 返回 dict。"""
    inputs = _build_board_inputs()
    regime = _classify(inputs)
    now_utc = datetime.now(timezone.utc)
    today  = _market_date(now_utc)
    info = {
        "date":   today,
        "regime": regime,
        "base_regime": _classify({**inputs, "short_style": {}}),
        "short_style": inputs.get("short_style") or {},
        "ts":     now_utc.isoformat(),
        "ts_et":  now_utc.astimezone(ET).isoformat(),
        "inputs": inputs,
    }
    atomic_write_json(REGIME_STATE_PATH, info)
    return info


def _warn_stale(data: dict, expected: str) -> None:
    global _STALE_WARNED_FOR
    actual = data.get("date")
    key = f"{actual}->{expected}"
    if _STALE_WARNED_FOR == key:
        return
    _STALE_WARNED_FOR = key
    logger.warning(
        f"[regime] regime_state.json 已过期: date={actual!r}, "
        f"expected_market_date={expected!r}; fallback neutral"
    )


def _load_state(*, allow_stale: bool = False) -> dict | None:
    if not REGIME_STATE_PATH.exists():
        return None
    try:
        data = json.loads(REGIME_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if allow_stale:
        return data
    expected = _market_date()
    if data.get("date") != expected:
        _warn_stale(data, expected)
        return None
    return data


def get_today_regime() -> str:
    """轻量调用：读市场日期匹配的 regime；缺失/失效 fallback 'neutral'。"""
    data = _load_state()
    if not data:
        return "neutral"
    return data.get("regime", "neutral") or "neutral"


def get_today_info(*, allow_stale: bool = False) -> dict:
    """完整的 regime info（含 inputs）。默认拒绝过期状态。"""
    return _load_state(allow_stale=allow_stale) or {}


_REGIME_LABEL = {
    "bull_trending":  "牛市延续 (追趋势, 偏多)",
    "bull_extended":  "牛市延伸 (强趋势, 防追高)",
    "bull_pulling":   "牛市回调 (等待确认后低吸)",
    "bull_chop":      "偏多震荡 (不把宏观偏多当追涨信号)",
    "neutral_chop":   "中性震荡 (提高加仓门槛)",
    "risk_off":       "风险收缩 (价格/板块急跌，非宏观衰退结论)",
    "overheated":     "过热警戒 (反转/减仓为主)",
    "recession_risk": "衰退风险 (避险偏向, 仅极端入场)",
    "crisis":         "危机防御 (现金为王, 几乎不动)",
    "neutral":        "中性 (无强方向)",
}


def format_regime_banner(info: dict) -> list[str]:
    """给 logger 用的醒目横幅。规则即前提。"""
    W = 76
    regime = info.get("regime", "neutral")
    label  = _REGIME_LABEL.get(regime, regime)
    inputs = info.get("inputs") or {}
    short_style = info.get("short_style") or inputs.get("short_style") or {}

    def _fmt(v, suffix=""):
        if v is None: return "N/A"
        return f"{v:+.2f}{suffix}" if isinstance(v, (int, float)) else str(v)

    lines = [
        "█" * W,
        f"█  今日 REGIME = {regime}  ({label})".ljust(W - 1) + "█",
        f"█  所有今日决策以此为前提（单股暴跌 ≤-5% 时该股 override 成 crisis）".ljust(W - 1) + "█",
        "█" * W,
        f"  输入: SOX 今日={_fmt(inputs.get('sox_mkt_today_pct'),'%'):>8}  "
        f"5日={_fmt(inputs.get('sox_mkt_5d_pct'),'%'):>8}  "
        f"20日={_fmt(inputs.get('sox_mkt_20d_pct'),'%'):>8}",
        f"        SPY 今日={_fmt(inputs.get('spy_today_pct'),'%'):>8}  "
        f"VIX={_fmt(inputs.get('vix')):>6}  "
        f"F&G={_fmt(inputs.get('fg')):>5}  "
        f"T10Y2Y={_fmt(inputs.get('t10y2y')):>6}",
        f"  短线风格: {short_style.get('style_zh', 'N/A')}  "
        f"震荡分={short_style.get('chop_score', 'N/A')}  "
        f"依据={short_style.get('reason', 'N/A')}",
        f"  写入: {REGIME_STATE_PATH.name}  ts={info.get('ts','')[:19]}",
    ]
    # 解读
    rules = {
        "bull_trending":  "  规则: 顺动量, 高残差 z>+2σ 跟仓；REDUCE 阈值放宽",
        "bull_extended":  "  规则: 趋势仍强，但严禁把慢周期标签理解为无条件追涨",
        "bull_pulling":   "  规则: 等待价格止跌与动量/期权流确认后再低吸",
        "bull_chop":      "  规则: 提高加仓门槛、降低目标波动、禁止仅凭宏观偏多追涨",
        "neutral_chop":   "  规则: 来回震荡，减少加仓频率，只做多因子确认",
        "risk_off":       "  规则: 板块价格急跌，暂停追涨、等待止跌；不解读为宏观衰退",
        "overheated":     "  规则: 反转候选, 低残差 z<-2σ 反弹；REDUCE 阈值收紧",
        "recession_risk": "  规则: 仅 |z|>2.5σ 才动, 反转策略 + 风控优先",
        "crisis":         "  规则: 不开新仓, 卫星仓全空, 核心仓减半",
        "neutral":        "  规则: 仅 |z|>3σ 极端才入场",
    }
    if regime in rules:
        lines.append(rules[regime])
    lines.append("=" * W)
    return lines


if __name__ == "__main__":
    info = detect_and_save_regime()
    for line in format_regime_banner(info):
        print(line)
