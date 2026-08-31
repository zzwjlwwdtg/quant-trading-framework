"""
本地 Paper Trader — 消费 decision_agent 的输出，通过 OpenD 在 SIMULATE
账户（acc_id 来自 secrets.local.json 的 MOOMOO_ACC_ID）自动下单。挂机模式：由 orchestrator 每个交易窗口
调用 execute()。

7 条核心规则：
  1) 仅共享 ORDER_ACTIONS 中的动作触发下单（CAUTION/HOLD 忽略）
  2) confidence 达到当前窗口门槛才下单（自动适配 5/10 分制）
  3) 仓位：每笔 = 账户购买力 × POSITION_FRACTION_BASE (1/10)；卫星 z 高时升到 MAX (1/6.7)
  4) 仅 TRADE_WINDOWS 中的窗口实际下单，pre-open 只刷新候选池
  5) BUY 成交后挂 stop-loss（若 decision.stop_ref 给了价位）
  6) GLD 纳入交易
  7) 默认 LIVE（在 SIMULATE 账户实际下单）；DRY-run 见环境变量 TRADER_DRY_RUN=1

核心+卫星架构（B 方案）：
  · 配置的核心仓与跟踪标的每天都跑，均可按信号交易
  · 动态卫星仓每天 pre-open 由 universe_picker 从 SOX 池中额外发现
  · 新 picks 进来时，仅轮换不在 picks 的动态卫星；绝不踢出用户显式加入标的

CLI:
  python paper_trader.py status   # 看持仓 + state
  python paper_trader.py flatten  # 平掉账户所有 LONG
  python paper_trader.py reset    # 清空 state 文件（不动账户）
  python paper_trader.py picks    # 看今日卫星仓候选池
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from moomoo import (
    OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv,
    TrdSide, OrderType, ModifyOrderOp, RET_OK,
)

from config import (
    OPEND_HOST,
    OPEND_PORT,
    MOOMOO_ACC_ID,
    TICKERS,
    TRADE_ELIGIBLE_TICKERS,
    TRADING_ACCOUNT_MODE,
    is_sim_active_trading,
)
from notifier import logger
from atomic_io import atomic_write_json
from data_quality import order_data_gate
from execution_analytics import (
    actual_execution_quality,
    append_execution_event,
    estimate_execution,
)
from portfolio_analytics import pretrade_portfolio_gate
from trading_contracts import (
    BUY_ACTIONS,
    CRISIS_PROBE_TARGET_VOL,
    ORDER_ACTIONS,
    PROBE_ONLY_ACTIONS,
    REDUCE_ACTIONS,
    SELL_ACTIONS,
    TRADE_WINDOWS,
    confidence_min,
    confidence_multiplier,
    extended_chase_signals,
)


ACC_ID  = MOOMOO_ACC_ID
TRD_ENV = TrdEnv.SIMULATE
if TRADING_ACCOUNT_MODE != "SIMULATE":
    raise RuntimeError("paper_trader currently supports SIMULATE accounts only")

# 盘前只给 0.5% 的成交余量。实时价明显高于日 K 参考价时直接放弃 BUY，
# 防止把“允许盘前成交”错误实现成“无条件追涨”。
WINDOW_CFG = {
    "pre-market": {"buffer": 0.005, "fill_outside_rth": True},
    "post-open":  {"buffer": 0.005, "fill_outside_rth": False},
    "midday":     {"buffer": 0.005, "fill_outside_rth": False},
    "pre-close":  {"buffer": 0.005, "fill_outside_rth": False},
}
PREMARKET_BUY_MAX_POSITIVE_GAP_PCT = 0.02

# ========== 仓位规模引擎 (Vol-Target + DD Floor + VIX Multiplier) ==========
#
# 最终仓位公式（机构 vol-target 标准）:
#   size_pct = (TARGET_PORT_VOL[regime] / asset_vol_annual)
#              × VIX_MULTIPLIER × DD_MULTIPLIER × CONF_MULTIPLIER
#   capped to POSITION_FRACTION_MAX per ticker
#
# 每个 regime 的目标组合年化波动率 (越激进越高)
TARGET_PORT_VOL = {
    "bull_extended":  0.30,
    "bull_pulling":   0.25,
    "bull_trending":  0.22,
    "bull_chop":      0.15,
    "neutral_chop":   0.10,
    "risk_off":       0.08,
    "neutral":        0.12,
    "overheated":     0.08,
    "recession_risk": 0.05,
    "crisis":         0.00,
}
POSITION_FRACTION_MAX    = 0.40   # 单笔最高 40% (避免 single trade 爆仓)

# 相关性组总暴露上限（防止 TQQQ + SOXL + MULL 同时满仓 = 6x+ 实质杠杆）
# 每个 ticker 归一个 group；BUY 前如该组当前暴露 + 新仓 > 上限，按比例缩减
CORRELATION_GROUP = {
    "US.TQQQ":  "tech_3x",
    "US.SOXL":  "tech_3x",
    "US.MULL":  "tech_2x",
    "US.DRAM":  "tech_1x",   # 1x memory ETF
    "US.NVDA":  "single_high_beta",
    "US.AAPL":  "single_high_beta",
    "US.TSLA":  "single_high_beta",
    "US.META":  "single_high_beta",
    "US.GOOGL": "single_high_beta",
    "US.AMD":   "single_high_beta",
    "US.GLD":   "defensive",
    "US.TLT":   "defensive",
    "US.SQQQ":  "inverse",
    "US.SOXS":  "inverse",
    "US.SH":    "inverse",
}
GROUP_CAP = {
    "tech_3x":          0.50,   # 3x ETF 合计 ≤ 50% NAV（实际 beta = 150%）
    "tech_2x":          0.30,
    "tech_1x":          0.30,
    "single_high_beta": 0.30,   # 高 beta 单股合计 ≤ 30%
    "defensive":        0.30,
    "inverse":          0.20,   # 反向工具占比小
}
ACCOUNT_POWER_FALLBACK   = 1_500_000

# 每个核心 ticker 的年化波动率 (回测算出, 缓存值)
ASSET_VOL_DEFAULTS = {
    "US.TQQQ": 0.60,   # 3x NDX, 高波
    "US.SOXL": 0.80,   # 3x SOX, 更高
    "US.GLD":  0.15,   # 黄金 ETF, 低波
}

# 卫星票动态算 (yfinance 60d), 缓存到避免每次查
_ASSET_VOL_CACHE: dict[str, float] = {}


def _annual_vol(ticker: str) -> float:
    """计算单只标的年化波动率 (yfinance 60 日 daily return std × sqrt(252))。"""
    if ticker in ASSET_VOL_DEFAULTS:
        return ASSET_VOL_DEFAULTS[ticker]
    if ticker in _ASSET_VOL_CACHE:
        return _ASSET_VOL_CACHE[ticker]
    try:
        import yfinance as yf
        import numpy as np
        symbol = ticker.replace("US.", "")
        h = yf.Ticker(symbol).history(period="80d", interval="1d", auto_adjust=True)
        if len(h) < 30:
            return 0.40
        ret = h["Close"].pct_change().dropna()
        v = float(ret.tail(60).std() * np.sqrt(252))
        _ASSET_VOL_CACHE[ticker] = v
        return v
    except Exception:
        return 0.40   # 兜底


# Layer 2 (强化版): VIX-band 仓位倍数 — 低 VIX 加杠杆, 高 VIX 现金
def _vix_multiplier() -> float:
    try:
        from regime_today import get_today_info
        vix = (get_today_info().get("inputs") or {}).get("vix")
        if vix is None: return 1.0
        v = float(vix)
        if v < 13: return 1.8    # 超低波 → 满杠
        if v < 18: return 1.3
        if v < 22: return 0.8
        if v < 28: return 0.4
        if v < 35: return 0.1
        return 0.0               # 极端 panic → 不开新仓
    except Exception:
        return 1.0


# Drawdown Floor: 组合从 NAV peak 跌幅触发降仓 (CPPI 思路)
NAV_PEAK_KEY = "__nav_peak"
def _drawdown_multiplier() -> float:
    state = _state_load()
    meta = state.get(NAV_PEAK_KEY, {})
    peak = meta.get("peak_nav", 0)
    cur  = meta.get("current_nav", 0)
    if not peak or not cur or cur >= peak:
        return 1.0
    dd = (cur - peak) / peak
    if dd > -0.05: return 1.0
    if dd > -0.10: return 0.7
    if dd > -0.15: return 0.4
    if dd > -0.20: return 0.2
    # 模拟仓积极模式的目的包含持续产生可复盘样本。深回撤时仍只给 25% 风险预算，
    # 而不是完全冻结；非模拟积极模式继续严格归零。
    return 0.25 if is_sim_active_trading() else 0.0


def _kelly_mult(ticker: str, min_trades: int = 10) -> float:
    """读 trade_log.jsonl 该 ticker 历史已平仓交易，算 half-Kelly 仓位乘子。
    样本 < min_trades 直接返回 1.0（无信号）。Cap [0.5, 1.5] 防极端。

    Kelly = W - (1-W)/R   (W=胜率, R=avg_win/|avg_loss|)
    Half Kelly = 0.5 × Kelly（行业惯例，full Kelly 过度激进）
    """
    try:
        from pathlib import Path
        from collections import defaultdict
        log_path = Path(__file__).parent / "signals" / "trade_log.jsonl"
        if not log_path.exists():
            return 1.0
        events = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    e = __import__("json").loads(line)
                    if not e.get("dry_run", True) and e["ticker"] == ticker:
                        events.append(e)
                except Exception:
                    continue
        # 简化配对（同 ticker FIFO）
        open_pos = []
        closed = []
        for ev in events:
            if ev["side"] == "BUY":
                open_pos.append({"qty": ev["qty"], "price": ev["price"]})
            elif ev["side"] == "SELL":
                rem = ev["qty"]
                while rem > 0 and open_pos:
                    p = open_pos[0]
                    cq = min(rem, p["qty"])
                    closed.append((ev["price"] - p["price"]) / p["price"] * 100)
                    p["qty"] -= cq
                    if p["qty"] <= 0:
                        open_pos.pop(0)
                    rem -= cq
        n = len(closed)
        if n < min_trades:
            return 1.0
        wins = [p for p in closed if p > 0]
        losses = [p for p in closed if p <= 0]
        if not losses:   # 100% 胜率 → cap 上限
            return 1.5
        if not wins:     # 0% 胜率 → 缩到下限
            return 0.5
        win_rate = len(wins) / n
        avg_win  = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))
        if avg_loss == 0:
            return 1.5
        R = avg_win / avg_loss
        full_kelly = win_rate - (1 - win_rate) / R
        half_kelly = 0.5 * full_kelly
        # 把 half_kelly（理论范围 -0.5 到 +0.5）映射到仓位乘子（0.5 到 1.5）
        mult = 1.0 + half_kelly
        return max(0.5, min(1.5, mult))
    except Exception:
        return 1.0


def _within_sitting_window(tstate: dict) -> bool:
    """是否仍在 sitting confirm 期内（持仓 < SITTING_MIN_DAYS 天）。
    用 first_entry_utc 作为基准；缺失则按 last_time_utc 兜底。"""
    try:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        ts_str = tstate.get("first_entry_utc") or tstate.get("last_time_utc")
        if not ts_str:
            return False
        entry_dt = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (_dt.now(_tz.utc) - entry_dt) < _td(days=SITTING_MIN_DAYS)
    except Exception:
        return False


def _check_loss_streak() -> tuple[bool, str]:
    """读 trade_log.jsonl 配对最近 N 笔；全亏 → 暂停。
    返回 (is_paused, reason)。任何异常 → 不暂停。"""
    try:
        from pathlib import Path
        from collections import defaultdict
        log_path = Path(__file__).parent / "signals" / "trade_log.jsonl"
        if not log_path.exists():
            return False, ""
        events = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    e = __import__("json").loads(line)
                    if not e.get("dry_run", True):  # 只看真实成交
                        events.append(e)
                except Exception:
                    continue
        # 配对 FIFO
        open_pos = defaultdict(list)
        closed = []
        for ev in events:
            tk = ev["ticker"]
            if ev["side"] == "BUY":
                open_pos[tk].append({"qty": ev["qty"], "price": ev["price"], "ts": ev["ts"]})
            elif ev["side"] == "SELL":
                rem = ev["qty"]
                while rem > 0 and open_pos[tk]:
                    p = open_pos[tk][0]
                    cq = min(rem, p["qty"])
                    closed.append({"ts": ev["ts"], "pnl_pct": (ev["price"] - p["price"]) / p["price"] * 100})
                    p["qty"] -= cq
                    if p["qty"] <= 0:
                        open_pos[tk].pop(0)
                    rem -= cq
        # 最近 N 笔
        recent = closed[-LOSS_STREAK_THRESHOLD:]
        if len(recent) < LOSS_STREAK_THRESHOLD:
            return False, ""
        all_losses = all(c["pnl_pct"] <= 0 for c in recent)
        if all_losses:
            avg_loss = sum(c["pnl_pct"] for c in recent) / len(recent)
            return True, f"最近 {LOSS_STREAK_THRESHOLD} 笔全亏，平均 {avg_loss:.2f}%"
        return False, ""
    except Exception:
        return False, ""


def _is_loss_streak_paused(state: dict | None = None) -> tuple[bool, str]:
    """检查 trader_state 里的 pause_until 是否还有效。"""
    state = _state_load() if state is None else state
    pause = state.get(LOSS_STREAK_STATE_KEY, {})
    if not pause:
        return False, ""
    try:
        until = datetime.fromisoformat(pause["until"])
        if datetime.now(timezone.utc) < until:
            return True, pause.get("reason", "")
    except Exception:
        pass
    # 过期了 → 清除
    state.pop(LOSS_STREAK_STATE_KEY, None)
    _state_save(state)
    return False, ""


def _trigger_loss_streak_pause(reason: str) -> None:
    state = _state_load()
    from datetime import timedelta
    until = (datetime.now(timezone.utc) + timedelta(hours=LOSS_STREAK_PAUSE_HOURS)).isoformat()
    state[LOSS_STREAK_STATE_KEY] = {"until": until, "reason": reason,
                                    "triggered_at": datetime.now(timezone.utc).isoformat()}
    _state_save(state)
    logger.warning(f"[trader] 🛑 连续亏损暂停 → {until} ({reason})")


def _apply_loss_streak_pause_after_sell() -> None:
    """Persist a new loss-streak pause after the caller saved its trade state."""
    try:
        is_streak, reason = _check_loss_streak()
        if not is_streak:
            return
        already_paused, _ = _is_loss_streak_paused()
        if already_paused:
            return
        _trigger_loss_streak_pause(reason)
        from notifications import send_alert
        send_alert(f"⛔ 连续亏损暂停 24h: {reason}", level="warning")
    except Exception:
        pass


def _update_nav_peak(current_nav: float) -> None:
    state = _state_load()
    meta  = state.get(NAV_PEAK_KEY, {"peak_nav": 0, "current_nav": 0})
    meta["current_nav"] = float(current_nav)
    if current_nav > meta.get("peak_nav", 0):
        meta["peak_nav"] = float(current_nav)
    state[NAV_PEAK_KEY] = meta
    _state_save(state)
    # 追加历史时序（每个 ET window 一次，给 benchmark 报告用）
    try:
        from pathlib import Path
        from datetime import datetime, timezone
        hist_path = Path(__file__).parent / "signals" / "nav_history.jsonl"
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        peak = meta.get("peak_nav", current_nav) or current_nav
        dd_pct = (current_nav - peak) / peak * 100 if peak > 0 else 0
        entry = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "nav":     round(float(current_nav), 2),
            "peak":    round(float(peak), 2),
            "dd_pct":  round(dd_pct, 2),
        }
        from atomic_io import append_jsonl
        append_jsonl(hist_path, entry)
    except Exception:
        pass   # history is best-effort; trading must not fail because of logging

CORE_TICKERS = set(TICKERS) | {"US.GLD"}

# #4 修复: 总持仓上限放宽到 12，卫星上限 9，允许约 150% 总暴露（使用 margin）
MAX_TOTAL_POSITIONS  = 12
MAX_SATELLITE_POSITIONS = 9

# #3 修复: REDUCE 后 24h 内若价格反弹 +3% → 自动 re-BUY 平回仓位
REBUY_LOOKBACK_HOURS = 24
# REBUY_BOUNCE_PCT 已替换为 _rebuy_bounce_pct(ticker) — 见下方杠杆缩放

# 阶梯止盈 (基础 1x, 实际按 sqrt(leverage) 缩放)
TP_BASE_LEVELS = [
    (0.15, 0.30, "tp15"),   # 1x +15% 卖 30%, SOXL (3x) +26% 卖 30%
    (0.30, 0.30, "tp30"),   # 1x +30% 卖 30%, SOXL +52%
    (0.50, 0.40, "tp50"),   # 1x +50% 卖剩下, SOXL +87%
]

# Trailing Stop: 从入场后最高价跌 N% 全平 (基础 1x = 8%; SOXL ~14%)
TRAILING_STOP_BASE_PCT = 0.08

# REBUY 反弹门槛 (基础 1x = +3%; SOXL ~+5.2%)
REBUY_BOUNCE_BASE_PCT  = 0.03

# Pyramid 加仓: 每层加 50% 原仓, 最多 3 层
PYRAMID_MAX_LAYERS = 3
PYRAMID_ADD_FRAC   = 0.50


def _leverage_sqrt(ticker: str) -> float:
    """返回 sqrt(leverage) 缩放因子。1x → 1.0, 3x → 1.73"""
    import math
    from config import LEVERAGE_FACTORS
    return math.sqrt(LEVERAGE_FACTORS.get(ticker, 1.0))


def _trailing_stop_pct(ticker: str) -> float:
    return TRAILING_STOP_BASE_PCT * _leverage_sqrt(ticker)


def _tp_levels(ticker: str) -> list[tuple[float, float, str]]:
    s = _leverage_sqrt(ticker)
    return [(t * s, frac, label) for t, frac, label in TP_BASE_LEVELS]


def _rebuy_bounce_pct(ticker: str) -> float:
    return REBUY_BOUNCE_BASE_PCT * _leverage_sqrt(ticker)

# 反向 ETF 对照表：BUY 前若已持有"反向对子" → 跳过，避免内部对冲
# 例：已持 TQQQ (3x 多头 QQQ)，再买 SQQQ (3x 空头 QQQ) = 互相对冲，纯亏交易成本
INVERSE_PAIRS = {
    "US.TQQQ": ["US.SQQQ", "US.PSQ", "US.QID"],
    "US.SQQQ": ["US.TQQQ", "US.QLD"],
    "US.QLD":  ["US.SQQQ", "US.PSQ", "US.QID"],
    "US.SOXL": ["US.SOXS"],
    "US.SOXS": ["US.SOXL"],
    "US.SPY":  ["US.SH", "US.SDS", "US.SPXU"],
    "US.SH":   ["US.SPY", "US.SSO", "US.UPRO"],
    "US.SPXU": ["US.SPY", "US.UPRO", "US.SSO"],
    "US.UPRO": ["US.SPY", "US.SPXU", "US.SDS"],
    "US.GLD":  ["US.DGLD", "US.JDST", "US.GLL"],
    "US.NVDA": ["US.NVDD"],
    "US.NVDD": ["US.NVDA", "US.NVDU"],
    "US.NVDU": ["US.NVDD"],
    "US.TSLA": ["US.TSLZ", "US.TSLQ"],
    "US.TSLZ": ["US.TSLA", "US.TSLL"],
    "US.TSLQ": ["US.TSLA", "US.TSLL"],
    "US.TSLL": ["US.TSLZ", "US.TSLQ"],
    "US.AAPL": ["US.AAPD"],
    "US.AAPD": ["US.AAPL", "US.AAPU"],
    "US.AAPU": ["US.AAPD"],
}

# 杠杆/底层标的对照表：BUY 前若已持有"对子另一边" → 跳过，避免叠杠杆
# 例：MULL 是 2x MU，已有 MULL 时不再买 MU（否则相当于 3x MU 暴露）
LEVERAGED_PAIRS = {
    "US.MU":    ["US.MULL"],
    "US.MULL":  ["US.MU"],
    "US.NVDA":  ["US.NVDU", "US.NVDX", "US.NVDD"],
    "US.NVDU":  ["US.NVDA"],
    "US.NVDX":  ["US.NVDA"],
    "US.TSLA":  ["US.TSLL", "US.TSLR", "US.TSLZ", "US.TSLQ"],
    "US.TSLL":  ["US.TSLA"],
    "US.TSLR":  ["US.TSLA"],
    "US.GOOGL": ["US.GGLL"],
    "US.GGLL":  ["US.GOOGL"],
    "US.AMD":   ["US.AMDL", "US.AMDU"],
    "US.AMDL":  ["US.AMD"],
    "US.AAPL":  ["US.AAPU", "US.AAPB"],
    "US.AAPU":  ["US.AAPL"],
    "US.META":  ["US.METU", "US.METD"],
    "US.METU":  ["US.META"],
    "US.MSFT":  ["US.MSFU"],
    "US.AVGO":  ["US.AVGX"],
    "US.AMZN":  ["US.AMZU", "US.AMZD"],
}

# 连续亏损暂停（P4.2）：最近 N 笔已平仓全亏 → 暂停新仓 PAUSE_HOURS 小时
LOSS_STREAK_THRESHOLD = 3
LOSS_STREAK_PAUSE_HOURS = 24
LOSS_STREAK_STATE_KEY = "__loss_streak_pause"

# Sitting 确认期（P4.1）：开仓后 N 天内信号驱动的 SELL/REDUCE 不放行
# （trailing stop / TP / crisis regime override / earnings guard 仍正常）
# 目的：抓 Livermore "sit through" 大趋势，避免在窗口里被瞬时反转信号洗出
SITTING_MIN_DAYS = 3

DRY_RUN = os.environ.get("TRADER_DRY_RUN", "0") == "1"

# 灰度切换（P5.1）：LIVE 模式下也可以只用账户的一部分资金
# LIVE_FRACTION = 0.1 → 实际 BUY size 缩到原计划的 10%；用于 paper → live 渐进上线
# 范围 0.0 ~ 1.0，超出 cap 到 1.0
LIVE_FRACTION = max(0.0, min(1.0, float(os.environ.get("TRADER_LIVE_FRACTION", "1.0"))))

STATE_PATH    = Path(__file__).parent / "trader_state.json"
UNIVERSE_PATH = Path(__file__).parent / "universe_state.json"
EXECUTION_LOG_PATH = Path(__file__).parent / "signals" / "execution_ledger.jsonl"


# ---------- Trade context (lazy singleton) ----------

_ctx = None
_TRADER_LOCK = threading.RLock()

def _ctx_get() -> OpenSecTradeContext:
    global _ctx
    if _ctx is None:
        _ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=OPEND_HOST, port=OPEND_PORT,
            security_firm=SecurityFirm.FUTUSECURITIES,
        )
    return _ctx


def _ctx_close() -> None:
    global _ctx
    if _ctx is not None:
        try: _ctx.close()
        except Exception: pass
        _ctx = None


# ---------- State ----------

class StateLoadError(RuntimeError):
    """The persisted trader risk state exists but cannot be trusted."""


def _state_load() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        message = f"交易状态不可读，已停止本轮下单: {STATE_PATH} ({exc})"
        logger.error(f"[trader] {message}")
        raise StateLoadError(message) from exc
    if not isinstance(state, dict):
        message = f"交易状态格式错误，已停止本轮下单: {STATE_PATH} (root must be object)"
        logger.error(f"[trader] {message}")
        raise StateLoadError(message)
    return state


def _entry_conf_on_scale(tstate: dict, current_scale: int,
                         fallback: float) -> float:
    """Convert persisted entry confidence to the active 5/10-point scale.

    Legacy states did not persist the scale. A valid old 10-point entry was at
    least 6, so values above 5 are unambiguously /10; values up to 5 are /5.
    """
    try:
        raw_conf = float(tstate["entry_conf"])
        if raw_conf <= 0:
            raise ValueError("entry confidence must be positive")
    except (KeyError, TypeError, ValueError):
        return float(fallback)
    try:
        source_scale = int(tstate["entry_conf_scale"])
        if source_scale not in (5, 10):
            raise ValueError("unsupported confidence scale")
    except (KeyError, TypeError, ValueError):
        source_scale = 10 if raw_conf > 5 else 5
    safe_current_scale = current_scale if current_scale in (5, 10) else 10
    return raw_conf * safe_current_scale / source_scale


def _state_save(state: dict) -> None:
    atomic_write_json(STATE_PATH, state)


def _universe_load() -> dict:
    if not UNIVERSE_PATH.exists():
        return {"date": None, "regime": None, "picks": []}
    try:
        universe = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        if not isinstance(universe, dict):
            raise ValueError("root must be object")
        return universe
    except Exception as exc:
        logger.warning(f"[trader] universe state 不可读，禁用卫星开仓: {UNIVERSE_PATH} ({exc})")
        return {"date": None, "regime": None, "picks": []}


def _universe_save(uni: dict) -> None:
    atomic_write_json(UNIVERSE_PATH, uni)


def _picks_by_ticker(uni: dict) -> dict:
    """{'US.NVDA': {z, size_usd, ...}, ...}"""
    return {p["ticker_full"]: p for p in (uni.get("picks") or [])}


# 账户购买力缓存（5 分钟）
_power_cache: float | None = None
_power_cache_ts: float = 0.0


def _get_account_power() -> float:
    """查 SIMULATE 账户购买力，5 分钟内缓存。OpenD 故障时返回 ACCOUNT_POWER_FALLBACK。"""
    global _power_cache, _power_cache_ts
    now = time.time()
    if _power_cache is not None and now - _power_cache_ts < 300:
        return _power_cache
    try:
        ctx = _ctx_get()
        ret, info = ctx.accinfo_query(trd_env=TRD_ENV, acc_id=ACC_ID, currency="USD")
        if ret == RET_OK and info is not None and not info.empty:
            p = float(info.iloc[0].get("power", 0) or 0)
            if p > 0:
                _power_cache, _power_cache_ts = p, now
                return p
    except Exception:
        pass
    return ACCOUNT_POWER_FALLBACK


def _group_current_exposure_usd(group: str) -> float:
    """查询该 correlation group 当前所有 ticker 的 market value 总和。"""
    if not group:
        return 0.0
    tickers_in_group = [tk for tk, g in CORRELATION_GROUP.items() if g == group]
    try:
        ctx = _ctx_get()
        ret, pos = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID, code=None)
    except Exception:
        return 0.0
    if ret != RET_OK or pos is None or pos.empty:
        return 0.0
    total = 0.0
    for _, row in pos.iterrows():
        code = str(row.get("code", ""))
        if code in tickers_in_group:
            qty = float(row.get("qty", 0) or 0)
            price = float(row.get("nominal_price", 0) or row.get("cost_price", 0) or 0)
            total += qty * price
    return total


def _group_cap_usd(ticker: str) -> tuple[float, str] | tuple[None, None]:
    """返回 (该 group 剩余可买额度 USD, group_name)；无组归属或无 cap → (None, None)。"""
    group = CORRELATION_GROUP.get(ticker)
    if not group or group not in GROUP_CAP:
        return None, None
    power = _get_account_power()
    if power <= 0:
        return None, None
    cap_usd = power * GROUP_CAP[group]
    current = _group_current_exposure_usd(group)
    remaining = max(0.0, cap_usd - current)
    return remaining, group


def _position_size_usd(ticker: str, conf: int = 6, action: str = "BUY") -> float:
    """
    Vol-Target + DD Floor + VIX Multiplier (机构标准 sizing):

      raw_pct  = TARGET_PORT_VOL[regime] / asset_vol_annual
      final    = raw_pct × vix_mult × dd_mult × conf_mult
      capped at POSITION_FRACTION_MAX = 40%

    例 (bull_extended + VIX 16 + TQQQ vol 60%):
      raw = 0.30 / 0.60 = 0.50 (50%)
      × VIX 1.3 × DD 1.0 × conf 1.0 (conf 6) = 65% → cap 40%
    """
    # 用户显式加入的标的无需经过动态 universe picks；只有系统额外发现的
    # 卫星票才要求当天仍在 picks，防止未知代码绕过入池规则。
    if ticker not in TRADE_ELIGIBLE_TICKERS:
        uni = _universe_load()
        if not _picks_by_ticker(uni).get(ticker):
            return 0.0

    power = _get_account_power()
    # 1) 当前 regime → 目标组合波动率
    try:
        from regime_today import get_today_regime
        regime = get_today_regime()
    except Exception:
        regime = "neutral"
    target_vol = TARGET_PORT_VOL.get(regime, 0.12)

    # WATCH_BUY_PROBE 例外：crisis 下允许极小 probe 仓位（1/2 normal probe）
    # 模拟仓积极模式下 WATCH_BUY 统一按小试探仓处理；明确 BUY 仍按正常仓位。
    is_probe = (
        action in PROBE_ONLY_ACTIONS
        or (is_sim_active_trading() and action == "WATCH_BUY")
    )
    if target_vol <= 0:
        if is_probe:
            target_vol = CRISIS_PROBE_TARGET_VOL
        else:
            return 0.0   # crisis / 未知 regime → 不开新仓

    # 2) 单股年化波动
    asset_vol = _annual_vol(ticker)
    raw_pct = target_vol / asset_vol if asset_vol > 0 else 0

    # 3) 乘数
    vix_mult  = _vix_multiplier()
    dd_mult   = _drawdown_multiplier()
    if is_probe and is_sim_active_trading() and vix_mult <= 0:
        # 极端 VIX 仍允许极小模拟试探仓，便于验证危机反弹规则；真实/普通模式不变。
        vix_mult = 0.10
    # conf → mult：低信心 = 试探仓 (probe)，高信心 = 满仓 + boost
    # PROBE_ONLY_ACTIONS: 强制 0.30 (30% 常规仓)，无视 conf
    try:
        from decision_agent import _conf_scale
        scale = _conf_scale()
    except Exception:
        scale = 10
    conf_mult = confidence_multiplier(conf, scale, probe=is_probe)

    kelly_mult = _kelly_mult(ticker)
    final_pct = raw_pct * vix_mult * dd_mult * conf_mult * kelly_mult
    final_pct = min(final_pct, POSITION_FRACTION_MAX)
    raw_size_usd = power * final_pct * LIVE_FRACTION   # 灰度切换

    # ── 相关性组总暴露上限 ─────────────────────────────────────────
    # 该 group 剩余可买额度 < raw_size_usd → 缩减到剩余额度
    remaining, group = _group_cap_usd(ticker)
    if remaining is not None and remaining < raw_size_usd:
        if remaining <= 0:
            logger.info(f"[trader] {ticker} 组 {group} 已满 (cap reached) → 跳过新仓")
            return 0.0
        logger.info(f"[trader] {ticker} 组 {group} 剩余 ${remaining:,.0f} < 计划 ${raw_size_usd:,.0f} → 缩减")
        return remaining
    return raw_size_usd


# ---------- AI target loader (A 方案：Claude 结构化目标覆盖)----------

def _load_ai_target_safe(ticker: str) -> dict | None:
    """从 signals/ai_targets_<date>.json 读取该 ticker 的 AI 价位目标。
    失败/不存在返回 None，不影响默认下单流程。"""
    try:
        from ai_prompt import load_ai_target
        return load_ai_target(ticker)
    except Exception:
        return None


def _ai_target_matches_market(ticker: str, target: dict | None,
                              current_price: float) -> bool:
    """拒绝已经明显脱离现价的旧 AI 目标。

    3x/2x 产品日内波动更大，允许 8%；普通资产允许 4%。这是是否继续消费
    旧计划的安全阈值，不代表追价区间。
    """
    if not target or current_price <= 0:
        return False
    try:
        entry = float(target.get("entry_ref") or 0)
    except (TypeError, ValueError):
        return False
    if entry <= 0:
        return False
    drift_limit = 0.08 if ticker in {"US.TQQQ", "US.SOXL", "US.MULL"} else 0.04
    drift = abs(entry - current_price) / current_price
    if drift > drift_limit:
        logger.info(
            f"[trader] IGNORE stale AI target {ticker}: entry ${entry:.2f} vs "
            f"current ${current_price:.2f} ({drift*100:.1f}% > {drift_limit*100:.0f}%)"
        )
        return False
    return True


_PENDING_ORDER_STATUSES = {
    "SUBMITTED", "SUBMITTING", "WAITING_SUBMIT", "FILLED_PART",
}
_MANAGED_ENTRY_KEYS = (
    "managed_entry_order_id", "managed_entry_kind", "managed_entry_ref",
    "managed_entry_stop_ref", "managed_entry_target_ts",
    "managed_entry_updated_utc",
)


def _clear_managed_entry(tstate: dict, *, clear_unfilled_entry: bool = False) -> None:
    for key in _MANAGED_ENTRY_KEYS:
        tstate.pop(key, None)
    if clear_unfilled_entry:
        for key in (
            "first_entry_utc", "entry_price", "entry_high", "entry_qty",
            "entry_basis_source", "entry_conf", "entry_conf_scale",
            "pyramid_layer", "tp_levels_hit",
        ):
            tstate.pop(key, None)
        _clear_protective_stop(tstate)


def _sync_pending_entry_for_decision(
    ticker: str,
    tstate: dict,
    action: str,
    current_price: float,
) -> str:
    """让本系统提交的未成交 BUY 跟随最新决策/AI 价位。

    返回 none/keep/updated/cancelled/error。只处理 trader_state 记录的
    order_id，因此不会触碰手工单；保护性卖单也不会进入此路径。
    """
    oid = tstate.get("last_order_id")
    if not oid or str(tstate.get("last_side") or "").upper() != "BUY":
        return "none"
    try:
        ctx = _ctx_get()
        ret, orders = ctx.order_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    except Exception as exc:
        logger.warning(f"[trader] {ticker} pending-entry query failed: {exc}")
        return "error"
    if ret != RET_OK or orders is None:
        return "error"

    row = None
    for _, candidate in orders.iterrows():
        if str(candidate.get("order_id") or "") == str(oid):
            row = candidate
            break
    if row is None:
        _clear_managed_entry(tstate)
        return "none"
    status = str(row.get("order_status") or "").upper()
    if status not in _PENDING_ORDER_STATUSES:
        _clear_managed_entry(tstate)
        # 刚成交时持仓查询可能比订单状态慢一个节拍；本轮先不重复买。
        if float(row.get("dealt_qty") or 0) > 0:
            return "keep"
        return "none"

    # 新系统决策已不再要求买入：旧限价必须撤销，不能继续在后台埋伏。
    if action not in BUY_ACTIONS:
        reason = f"latest decision={action or 'NONE'}"
        op = ModifyOrderOp.CANCEL
    else:
        ai_t = _load_ai_target_safe(ticker)
        ai_entry = (ai_t or {}).get("entry_ref")
        desired_limit = bool(
            ai_t
            and _ai_target_matches_market(ticker, ai_t, current_price)
            and ai_t.get("action") in ("watch_buy", "buy")
            and ai_t.get("use_limit")
            and ai_entry
            and float(ai_entry) < current_price
        )
        if desired_limit:
            desired_price = round(float(ai_entry) * 1.001, 2)
            old_price = float(row.get("price") or 0)
            if abs(desired_price - old_price) < 0.005:
                return "keep"
            try:
                ret, info = ctx.modify_order(
                    modify_order_op=ModifyOrderOp.NORMAL,
                    order_id=oid,
                    qty=float(row.get("qty") or 0),
                    price=desired_price,
                    trd_env=TRD_ENV,
                    acc_id=ACC_ID,
                )
            except Exception as exc:
                logger.error(f"[trader] {ticker} pending limit update failed: {exc}")
                return "error"
            if ret != RET_OK:
                logger.error(f"[trader] {ticker} pending limit update rejected: {info}")
                return "error"
            tstate.update({
                "managed_entry_order_id": str(oid),
                "managed_entry_kind": "ai_limit",
                "managed_entry_ref": float(ai_entry),
                "managed_entry_stop_ref": (ai_t or {}).get("stop_ref"),
                "managed_entry_target_ts": (ai_t or {}).get("_source_ts"),
                "managed_entry_updated_utc": datetime.now(timezone.utc).isoformat(),
                "last_price": float(ai_entry),
            })
            logger.info(
                f"[trader] {ticker} UPDATE pending AI limit order={oid} "
                f"${old_price:.2f} → ${desired_price:.2f}"
            )
            return "updated"

        # 只有明确标记为 AI resting limit 的单，才因 AI 不再要求限价而撤销；
        # 老版本/普通可成交 BUY 保守地保留，避免误撤。
        if tstate.get("managed_entry_kind") != "ai_limit":
            return "keep"
        reason = "latest AI target no longer requests a resting limit"
        op = ModifyOrderOp.CANCEL

    try:
        ret, info = ctx.modify_order(
            modify_order_op=op,
            order_id=oid,
            qty=0,
            price=0,
            trd_env=TRD_ENV,
            acc_id=ACC_ID,
        )
    except Exception as exc:
        logger.error(f"[trader] {ticker} stale pending cancel failed: {exc}")
        return "error"
    if ret != RET_OK:
        logger.error(f"[trader] {ticker} stale pending cancel rejected: {info}")
        return "error"
    logger.info(f"[trader] CANCEL stale pending BUY {ticker} order={oid}: {reason}")
    _clear_managed_entry(tstate, clear_unfilled_entry=True)
    tstate["last_order_id"] = None
    return "cancelled"


# ---------- Claude vs Rules 冲突 alert（A 方案：不下单，只提示）----------

CLAUDE_CONFLICT_LOG = Path(__file__).parent / "signals" / "claude_conflict.jsonl"
_CONFLICT_DEDUP_KEY = "_claude_conflict_dedup"   # in trader_state


def _notify_claude_rules_conflict(ticker: str, rule_action: str, ai_t: dict,
                                    entry: float | None, stop: float | None,
                                    cur_price: float, window_key: str) -> None:
    """规则说 HOLD/CAUTION，但 Claude ai_target 说 buy/watch_buy → 打 Discord alert。

    Dedup: 同一 ticker+window 一天只推 1 次（避免每 5 min cycle 刷屏）。
    """
    # Dedup 检查
    state = _state_load()
    dedup = state.get(_CONFLICT_DEDUP_KEY, {})
    today = datetime.now(timezone.utc).date().isoformat()
    dedup_key = f"{today}:{ticker}:{window_key}"
    if dedup.get(dedup_key):
        return  # 今天该 window 已推过
    # 清理旧日期 dedup entries (保留 3 天)
    keep = {k: v for k, v in dedup.items() if k.split(":")[0] >= today}

    ai_action = ai_t.get("action", "?")
    ai_notes = ai_t.get("notes", "")[:200]
    entry_str = f"${entry:.2f}" if entry else "—"
    stop_str  = f"${stop:.2f}"  if stop  else "—"
    stop_pct  = f" ({(stop-cur_price)/cur_price*100:+.1f}%)" if (stop and cur_price) else ""
    msg = (
        f"⚡ {ticker} Claude vs 规则冲突\n"
        f"规则: {rule_action} · Claude: {ai_action}\n"
        f"Claude 建议: 入场 {entry_str} · 止损 {stop_str}{stop_pct} · 现价 ${cur_price:.2f}\n"
        f"理由: {ai_notes}"
    )
    try:
        from notifications import send_alert
        send_alert(msg, level="warning")
    except Exception:
        pass

    # 记录到 jsonl 供长期追踪 hit rate（Claude 建议 vs 后续实际走势）
    try:
        CLAUDE_CONFLICT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CLAUDE_CONFLICT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts":          datetime.now(timezone.utc).isoformat(),
                "ticker":      ticker,
                "rule_action": rule_action,
                "ai_action":   ai_action,
                "entry_ref":   entry,
                "stop_ref":    stop,
                "target_ref":  ai_t.get("target_ref"),
                "cur_price":   cur_price,
                "notes":       ai_notes,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    keep[dedup_key] = True
    state[_CONFLICT_DEDUP_KEY] = keep
    _state_save(state)
    logger.info(f"[trader] Claude/rules conflict alerted: {ticker} rule={rule_action} claude={ai_action}")


# ---------- Position query ----------

def _position_snapshot(code: str) -> dict:
    """Read broker position facts used by sizing, cost basis and software stops."""
    ctx = _ctx_get()
    ret, pos = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID, code=code)
    if ret != RET_OK or pos is None or pos.empty:
        return {
            "position_qty": 0,
            "can_sell_qty": 0,
            "cost_price": 0.0,
            "nominal_price": 0.0,
        }
    row = pos.iloc[0]

    def _number(field: str) -> float:
        try:
            return float(row.get(field, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    can_sell_qty = int(_number("can_sell_qty"))
    position_qty = int(_number("qty")) or can_sell_qty
    return {
        "position_qty": position_qty,
        "can_sell_qty": can_sell_qty,
        "cost_price": _number("cost_price"),
        "nominal_price": _number("nominal_price"),
    }


def _position_qty(code: str) -> int:
    return int(_position_snapshot(code)["can_sell_qty"])


def _position_cost_price(code: str, fallback: float) -> float:
    """Prefer the broker's weighted fill cost; fall back to the execution reference."""
    try:
        cost = float(_position_snapshot(code).get("cost_price") or 0)
        if cost > 0:
            return cost
    except Exception as exc:
        logger.warning(f"[trader] cost basis query failed for {code}: {exc}")
    return float(fallback)


# ---------- Order placement ----------


def _portfolio_what_if(code: str, side: str, qty: int, price: float) -> dict:
    """Broker-backed pre-trade portfolio stress check; unavailable is explicit."""
    if DRY_RUN:
        # A dry-run must not open a broker context merely to calculate an
        # observational warning.  Live SIMULATE orders use the real holdings.
        return {
            "allow_order": True,
            "status": "dry_run_observe_only",
            "policy": "no_broker_connection_no_risk_block",
        }
    try:
        ctx = _ctx_get()
        ret, frame = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
        if ret != RET_OK:
            raise RuntimeError("position query failed")
        positions = []
        if frame is not None and not frame.empty:
            for _, row in frame.iterrows():
                current_price = float(row.get("nominal_price", 0) or row.get("cost_price", 0) or 0)
                current_qty = float(row.get("qty", 0) or 0)
                positions.append({
                    "ticker": str(row.get("code") or ""),
                    "qty": current_qty,
                    "current_price": current_price,
                    "cost_price": float(row.get("cost_price", 0) or 0),
                    "market_val": float(row.get("market_val", 0) or current_qty * current_price),
                    "pl_val": float(row.get("pl_val", 0) or 0),
                })
        nav = 0.0
        ret_info, info = ctx.accinfo_query(trd_env=TRD_ENV, acc_id=ACC_ID, currency="USD")
        if ret_info == RET_OK and info is not None and not info.empty:
            nav = float(info.iloc[0].get("total_assets", 0) or 0)
        if nav <= 0:
            nav = max(_get_account_power(), sum(p["market_val"] for p in positions))
        return pretrade_portfolio_gate(
            positions,
            nav=nav,
            ticker=code,
            side=side,
            qty=qty,
            price=price,
        )
    except Exception as exc:
        return {"allow_order": True, "status": "unavailable", "reason": str(exc)}

def _get_realtime_price(code: str, fallback: float) -> float:
    """
    pre-market/after-hours 时取真实实时价，避免 daily K 收盘价和实时价有大 gap
    导致限价单挂在书上等不到撮合。fail-safe 回退到 fallback。
    """
    try:
        import yfinance as yf
        symbol = code.replace("US.", "")
        df = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)
        if df is not None and not df.empty:
            rt = float(df["Close"].iloc[-1])
            if rt > 0:
                return rt
    except Exception as e:
        logger.warning(f"[trader] realtime price for {code} failed: {e}")
    return fallback


def _log_trade(ticker: str, side: str, qty: int, price: float,
                order_id: str | None, tag: str, decision: dict | None = None,
                mkt: dict | None = None, window: str | None = None,
                extra: dict | None = None) -> None:
    """每笔成功成交（或 dry-run）追加到 signals/trade_log.jsonl，给复盘归因用。
    永不影响主流程；任何异常吞掉。"""
    try:
        from pathlib import Path
        path = Path(__file__).parent / "signals" / "trade_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        decision = decision or {}
        mkt = mkt or {}
        entry = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "ticker":   ticker,
            "side":     side,
            "qty":      int(qty),
            "price":    round(float(price), 2),
            "order_id": order_id,
            "tag":      tag,
            "window":   window,
            "dry_run":  DRY_RUN,
            "decision": {
                "action":     decision.get("action"),
                "confidence": decision.get("confidence"),
                "reason":     (decision.get("reason") or "")[:120],
                "regime":     decision.get("regime"),
                "engine":     decision.get("engine"),
                "score_breakdown": decision.get("score_breakdown"),
                "earnings_guard":  decision.get("earnings_guard"),
                "uncertain":       decision.get("uncertain"),
                "trump_override":  decision.get("trump_override"),
            },
            "market": {
                "rsi":      mkt.get("rsi_14"),
                "cci":      mkt.get("cci_20"),
                "vol_ratio":mkt.get("vol_ratio"),
                "trend":    mkt.get("trend"),
                "ma_stack": mkt.get("ma_stack"),
                "ma20":     mkt.get("ma20"),
                "ma50":     mkt.get("ma50"),
                "pct_chg":  mkt.get("pct_chg"),
                "cum_5d":   mkt.get("cum_5d_pct"),
                "cum_10d":  mkt.get("cum_10d_pct"),
                "bb_pct":   mkt.get("bb_pct"),
            },
        }
        if extra:
            entry["context"] = extra
        with open(path, "a", encoding="utf-8") as f:
            f.write(__import__("json").dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _place(code: str, side, qty: int, price: float, tag: str = "",
           buffer: float = 0.005, fill_outside_rth: bool = False,
           decision: dict | None = None, mkt: dict | None = None,
           window: str | None = None, extra: dict | None = None,
           resting_limit: bool = False):
    """
    返回 order_id（实盘）或 'DRY'（dry-run）或 None（失败/跳过）。
    decision/mkt/window/extra：选填，仅供 trade_log 记录（不影响下单）。
    """
    if qty <= 0 or price <= 0:
        logger.warning(f"[trader] SKIP {side} {code} qty={qty} price={price}")
        return None
    side_label = "BUY " if side == TrdSide.BUY else "SELL"
    # Data provenance gate is fail-closed for new risk and fail-open for exits.
    # Missing optional quote fields only creates a warning; invalid/stale price
    # blocks BUY.  This keeps historical callers compatible while hardening the
    # live market path.
    extra = dict(extra or {})
    portfolio_gate = _portfolio_what_if(code, side_label.strip(), qty, price)
    if is_sim_active_trading() and not portfolio_gate.get("allow_order", False):
        portfolio_gate["sim_active_override"] = True
        portfolio_gate["allow_order"] = True
        logger.warning(
            f"[trader] SIM_ACTIVE portfolio warning only for {code}: "
            f"{', '.join(portfolio_gate.get('breaches') or [])}"
        )
    extra["portfolio_what_if"] = portfolio_gate
    if not portfolio_gate.get("allow_order", False):
        logger.error(
            f"[trader] SKIP {side_label.strip()} {code}: portfolio risk gate "
            f"({', '.join(portfolio_gate.get('breaches') or [])})"
        )
        return None
    if mkt:
        quality = order_data_gate(mkt, side_label.strip())
        extra["data_quality"] = quality
        if not quality.get("allow_order", False):
            reasons = ", ".join(x.get("code", "data") for x in quality.get("issues", []))
            logger.error(f"[trader] SKIP {side_label.strip()} {code}: data-quality gate ({reasons})")
            return None
    # 盘前普通订单以实时价为基准，但正向跳空超过上限时不追价。
    # 明确低于市价的 AI resting limit 保留原限价，不受此门禁影响。
    ref_price = price
    if fill_outside_rth:
        rt = _get_realtime_price(code, price)
        gap_pct = (rt - price) / price
        if (
            side == TrdSide.BUY
            and not resting_limit
            and gap_pct > PREMARKET_BUY_MAX_POSITIVE_GAP_PCT
        ):
            logger.warning(
                f"[trader] SKIP BUY {code}: pre-market gap {gap_pct*100:+.1f}% "
                f"exceeds chase cap {PREMARKET_BUY_MAX_POSITIVE_GAP_PCT*100:.1f}% "
                f"(realtime ${rt:.2f}, reference ${price:.2f})"
            )
            return None
        if not resting_limit and rt > 0:
            if rt != price:
                logger.info(
                    f"[trader] {code} realtime ${rt:.2f} vs reference ${price:.2f} "
                    f"(gap {gap_pct*100:+.1f}%)"
                )
            ref_price = rt
    if side == TrdSide.BUY:
        order_price = round(ref_price * (1 + buffer), 2)
    else:
        order_price = round(ref_price * (1 - buffer), 2)
    rth_label = "+RTHx" if fill_outside_rth else ""
    ref_label = f"ref {ref_price:.2f}" + (f" / daily {price:.2f}" if ref_price != price else "")
    execution_plan = estimate_execution(
        code,
        side_label.strip(),
        qty,
        ref_price,
        bid=(mkt or {}).get("bid_price") or (mkt or {}).get("bid"),
        ask=(mkt or {}).get("ask_price") or (mkt or {}).get("ask"),
        avg_daily_volume=(mkt or {}).get("avg_volume_20"),
        annual_volatility=(mkt or {}).get("vol_20d_annual"),
        outside_rth=fill_outside_rth,
        resting_limit=resting_limit,
    )
    extra["execution_plan"] = execution_plan
    if DRY_RUN:
        logger.info(f"[trader-DRY ] {side_label} {qty:>5} {code:<8} @ {order_price:>8.2f} ({ref_label}) {rth_label} {tag}")
        _log_trade(code, side_label.strip(), qty, order_price, "DRY", tag,
                   decision=decision, mkt=mkt, window=window, extra=extra)
        modeled_qty = int(execution_plan.get("modeled_fill_qty") or 0)
        modeled_price = float(execution_plan.get("expected_fill_price") or order_price)
        append_execution_event(EXECUTION_LOG_PATH, {
            "event": "modeled",
            "order_id": "DRY",
            "ticker": code,
            "side": side_label.strip(),
            "plan": execution_plan,
            "quality": actual_execution_quality(
                side=side_label.strip(), requested_qty=qty, dealt_qty=modeled_qty,
                reference_price=ref_price, average_fill_price=modeled_price,
            ),
        })
        return "DRY"
    ctx = _ctx_get()
    try:
        ret, info = ctx.place_order(
            price=order_price, qty=float(qty), code=code,
            trd_side=side, order_type=OrderType.NORMAL,
            trd_env=TRD_ENV, acc_id=ACC_ID,
            fill_outside_rth=fill_outside_rth,
        )
    except TypeError:
        # 老版 moomoo SDK 不认识 fill_outside_rth 参数，回退到不传
        ret, info = ctx.place_order(
            price=order_price, qty=float(qty), code=code,
            trd_side=side, order_type=OrderType.NORMAL,
            trd_env=TRD_ENV, acc_id=ACC_ID,
        )
    if ret != RET_OK:
        logger.error(f"[trader] FAIL  {side_label} {qty} {code} @ {order_price:.2f}: {info}")
        return None
    oid = info.iloc[0]["order_id"]
    logger.info(f"[trader-LIVE] {side_label} {qty:>5} {code:<8} @ {order_price:>8.2f} ({ref_label}) {rth_label} order={oid} {tag}")
    _log_trade(code, side_label.strip(), qty, order_price, str(oid), tag,
               decision=decision, mkt=mkt, window=window, extra=extra)
    append_execution_event(EXECUTION_LOG_PATH, {
        "event": "submitted",
        "order_id": str(oid),
        "ticker": code,
        "side": side_label.strip(),
        "requested_qty": qty,
        "order_price": order_price,
        "reference_price": ref_price,
        "plan": execution_plan,
    })
    # 主动推送 trade（Telegram/Discord，如已配置）
    try:
        from notifications import notify_trade
        conf = (decision or {}).get("confidence") if decision else None
        notify_trade(code, side_label.strip(), qty, order_price, tag=tag, conf=conf)
    except Exception:
        pass
    return oid


def refresh_execution_ledger() -> None:
    """Reconcile submitted orders with broker fill/partial/cancel facts."""
    if DRY_RUN or not EXECUTION_LOG_PATH.exists():
        return
    submitted: dict[str, dict] = {}
    try:
        with open(EXECUTION_LOG_PATH, encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                oid = str(event.get("order_id") or "")
                if not oid:
                    continue
                if event.get("event") == "submitted":
                    submitted[oid] = event
    except Exception:
        return
    if not submitted:
        return
    try:
        ctx = _ctx_get()
        ret, orders = ctx.order_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
        if ret != RET_OK or orders is None or orders.empty:
            return
    except Exception:
        return
    state = _state_load()
    seen = state.get("__execution_reconcile", {})
    if not isinstance(seen, dict):
        seen = {}
    dirty = False
    for _, row in orders.iterrows():
        oid = str(row.get("order_id") or "")
        base = submitted.get(oid)
        if not base:
            continue
        requested = int(float(row.get("qty", base.get("requested_qty", 0)) or 0))
        dealt = float(row.get("dealt_qty", 0) or 0)
        avg_fill = float(row.get("dealt_avg_price", 0) or 0)
        status = str(row.get("order_status") or "")
        signature = f"{status}|{dealt:.8f}|{avg_fill:.8f}"
        if seen.get(oid) == signature:
            continue
        upper = status.upper()
        if dealt >= requested > 0:
            event_name = "filled"
        elif dealt > 0:
            event_name = "partial"
        elif "CANCEL" in upper or "DISABLE" in upper or "FAIL" in upper:
            event_name = "cancelled"
        else:
            continue
        reference = float(base.get("reference_price") or base.get("order_price") or 0)
        quality = actual_execution_quality(
            side=base.get("side", "BUY"), requested_qty=requested, dealt_qty=dealt,
            reference_price=reference, average_fill_price=avg_fill,
        )
        append_execution_event(EXECUTION_LOG_PATH, {
            "event": event_name,
            "order_id": oid,
            "ticker": base.get("ticker"),
            "side": base.get("side"),
            "broker_status": status,
            "requested_qty": requested,
            "dealt_qty": dealt,
            "average_fill_price": avg_fill,
            "quality": quality,
        })
        seen[oid] = signature
        dirty = True
    if dirty:
        state["__execution_reconcile"] = dict(list(seen.items())[-500:])
        _state_save(state)


def sync_pending_entries_from_latest_signals() -> list[dict]:
    """AI 目标刷新后，立即同步系统尚未成交的限价 BUY。

    只扫描带 ``managed_entry_order_id`` 的系统订单；不新建订单、不触碰
    手工单。最新规则 action 失效则撤单，AI entry_ref 改变则原单改价。
    """
    with _TRADER_LOCK:
        state = _state_load()
        changes: list[dict] = []
        dirty = False
        signals_dir = Path(__file__).parent / "signals"
        for ticker, tstate in state.items():
            if not isinstance(tstate, dict) or not tstate.get("managed_entry_order_id"):
                continue
            if _position_qty(ticker) > 0:
                _clear_managed_entry(tstate)
                dirty = True
                continue
            short = ticker.split(".")[-1]
            signal_path = signals_dir / f"{short}_latest.json"
            if not signal_path.exists():
                continue
            try:
                signal = json.loads(signal_path.read_text(encoding="utf-8"))
                decision = signal.get("decision") or {}
                market = signal.get("market") or {}
                action = str(decision.get("action") or "")
                current_price = float(market.get("price") or 0)
            except Exception:
                continue
            if current_price <= 0:
                continue
            result = _sync_pending_entry_for_decision(
                ticker, tstate, action, current_price
            )
            if result in {"updated", "cancelled", "none"}:
                dirty = True
            if result in {"updated", "cancelled"}:
                changes.append({"ticker": ticker, "result": result})
        if dirty:
            _state_save(state)
        return changes


def _place_stop_loss(code: str, qty: int, stop_price: float):
    """BUY 后挂 SELL STOP 保护。失败不影响主流程。"""
    if DRY_RUN:
        logger.info(f"[trader-DRY ] STOP  {qty:>5} {code:<8} trigger={stop_price:.2f}")
        return None
    try:
        ctx = _ctx_get()
        ret, info = ctx.place_order(
            price=round(stop_price * 0.99, 2),   # limit 略低于 trigger，保证能成交
            qty=float(qty), code=code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.STOP,
            trd_env=TRD_ENV, acc_id=ACC_ID,
            aux_price=round(stop_price, 2),
        )
        if ret == RET_OK:
            oid = info.iloc[0]["order_id"]
            logger.info(f"[trader-LIVE] STOP  {qty:>5} {code:<8} trigger={stop_price:.2f}  order={oid}")
            return oid
        logger.warning(f"[trader] STOP FAIL {code} @ {stop_price:.2f}: {info}")
    except Exception as e:
        logger.warning(f"[trader] STOP EXC  {code}: {e}")
    return None


_PROTECTIVE_STOP_KEYS = (
    "protective_stop_price",
    "protective_stop_mode",
    "protective_stop_order_id",
    "protective_stop_updated_utc",
)


def _clear_protective_stop(tstate: dict) -> None:
    for key in _PROTECTIVE_STOP_KEYS:
        tstate.pop(key, None)


def monitor_software_stops() -> list[str]:
    with _TRADER_LOCK:
        return _monitor_software_stops_unlocked()


def _monitor_software_stops_unlocked() -> list[str]:
    """Check fallback stops independently of decision/signal trading windows.

    Moomoo SIMULATE accounts can reject STOP orders. Those entries are marked as
    software-protected and polled by the orchestrator once per minute. A failed
    exit remains armed so the next poll retries instead of silently dropping risk.
    """
    state = _state_load()
    triggered: list[str] = []
    dirty = False
    for ticker, tstate in list(state.items()):
        if not isinstance(tstate, dict):
            continue
        if tstate.get("protective_stop_mode") != "software":
            continue
        try:
            stop_price = float(tstate.get("protective_stop_price") or 0)
        except (TypeError, ValueError):
            stop_price = 0
        if stop_price <= 0:
            logger.error(f"[trader] invalid software stop for {ticker}; keeping fail-closed state")
            continue

        snapshot = _position_snapshot(ticker)
        position_qty = int(snapshot.get("position_qty") or 0)
        can_sell_qty = int(snapshot.get("can_sell_qty") or 0)
        current_price = float(snapshot.get("nominal_price") or 0)
        if position_qty <= 0:
            # AI resting BUY 还在 broker 等待成交时，保留随单止损状态；
            # 否则订单稍后成交会变成没有保护的裸仓。
            if tstate.get("managed_entry_order_id"):
                continue
            _clear_protective_stop(tstate)
            dirty = True
            continue
        if current_price <= 0 or current_price > stop_price:
            continue
        if can_sell_qty <= 0:
            logger.error(
                f"[trader] SOFTWARE STOP {ticker} triggered at ${current_price:.2f} "
                "but no sellable quantity is available; will retry"
            )
            continue

        tag = f"[SOFTWARE-STOP trigger ${stop_price:.2f} current ${current_price:.2f}]"
        oid = _place(
            ticker,
            TrdSide.SELL,
            can_sell_qty,
            current_price,
            tag=tag,
            buffer=0.005,
            fill_outside_rth=True,
            extra={"risk_exit": "software_stop", "stop_price": stop_price},
        )
        if oid is None:
            logger.error(f"[trader] SOFTWARE STOP exit failed for {ticker}; remains armed")
            continue

        now_iso = datetime.now(timezone.utc).isoformat()
        tstate.update({
            "last_action": "SOFTWARE_STOP",
            "last_side": "SELL",
            "last_qty": can_sell_qty,
            "last_price": current_price,
            "last_time_utc": now_iso,
            "last_order_id": None if oid == "DRY" else oid,
            "protective_stop_mode": "software_triggered",
            "protective_stop_order_id": None if oid == "DRY" else oid,
            "protective_stop_updated_utc": now_iso,
        })
        triggered.append(ticker)
        dirty = True

    if dirty:
        _state_save(state)
    return triggered


# ---------- Main entry ----------

def execute(ticker: str, decision: dict, mkt: dict, window: str | None,
            _manual: bool = False) -> None:
    with _TRADER_LOCK:
        return _execute_unlocked(ticker, decision, mkt, window, _manual=_manual)


def _execute_unlocked(ticker: str, decision: dict, mkt: dict, window: str | None,
                      _manual: bool = False) -> None:
    """
    orchestrator 在每个 ticker 决策出来后调用。
    window:  pre-open / midday / post-open / pre-close / None
    _manual: True 表示这是手动测试调用（demo/test script），
             window_key 用 "manual:..." 前缀不污染真窗口的防重复机制。
    """
    if window not in TRADE_WINDOWS:
        return

    action = (decision or {}).get("action")
    conf = (decision or {}).get("confidence") or 0
    price = (mkt or {}).get("price")
    if not price:
        return

    state  = _state_load()
    tstate = state.get(ticker, {})
    if not isinstance(tstate, dict):
        message = f"{ticker} 交易状态格式错误，已停止本轮下单"
        logger.error(f"[trader] {message}")
        raise StateLoadError(message)

    is_core = ticker in CORE_TICKERS
    win_cfg = WINDOW_CFG.get(window, {})

    # Build the idempotency key before any discipline branch can place an order.
    today = datetime.now(timezone.utc).date().isoformat()
    if _manual:
        window_key = f"manual:{time.time_ns()}"
    else:
        window_key = f"{today}:{window}"

    # 普通已完成动作保持原有的零查询幂等路径；只有仍有 managed entry 的
    # 情况才需要先向 broker 核对，以便同一窗口内也能响应目标更新。
    if (
        tstate.get("last_window_key") == window_key
        and not tstate.get("managed_entry_order_id")
    ):
        return

    # 先核对上一轮由本系统提交、尚未成交的 BUY。最新决策转 HOLD/SELL 时
    # 撤单；AI 入场价变化时直接改单；仍有效时阻止重复提交。
    pos_qty_check = _position_qty(ticker)
    sync_result = "none"
    if pos_qty_check <= 0:
        sync_result = _sync_pending_entry_for_decision(
            ticker, tstate, action, float(price)
        )
        if sync_result in {"updated", "cancelled", "none"}:
            state[ticker] = tstate
            _state_save(state)
        if sync_result in {"keep", "updated", "error"}:
            return
    elif tstate.get("managed_entry_order_id"):
        _clear_managed_entry(tstate)
        state[ticker] = tstate
        _state_save(state)

    # A successful order in this window must block every branch, including
    # trailing-stop/TP, when the scheduler retries a partially failed cycle.
    if tstate.get("last_window_key") == window_key and sync_result != "cancelled":
        return

    # ── 纪律性管理（与方向信号解耦, 任一触发立刻 return 不走 normal action）──
    cur_price = float(price)
    risk_state_dirty = False
    if pos_qty_check > 0:
        # 更新 entry_high (持仓期间最高)
        entry_price = float(tstate.get("entry_price") or cur_price)
        entry_high  = float(tstate.get("entry_high")  or entry_price)
        if "entry_price" not in tstate:
            tstate["entry_price"] = entry_price
            risk_state_dirty = True
        if "entry_high" not in tstate:
            tstate["entry_high"] = entry_high
            risk_state_dirty = True
        if cur_price > entry_high:
            entry_high = cur_price
            tstate["entry_high"] = entry_high
            risk_state_dirty = True

        # #3 Trailing Stop: 从入场后高点跌 ≥ N% (按杠杆缩放) → 全平
        ts_pct = _trailing_stop_pct(ticker)
        if entry_high > 0 and (cur_price - entry_high) / entry_high <= -ts_pct:
            qty = pos_qty_check
            tag = f"[TRAILING-STOP from high ${entry_high:.2f} → ${cur_price:.2f} ({(cur_price-entry_high)/entry_high*100:+.1f}%)]"
            oid = _place(ticker, TrdSide.SELL, qty, cur_price, tag=tag,
                         buffer=win_cfg.get("buffer", 0.005),
                         fill_outside_rth=win_cfg.get("fill_outside_rth", False))
            if oid:
                tstate.update({"last_action":"TRAILING_STOP","last_qty":qty,
                               "last_price":cur_price,
                               "last_time_utc": datetime.now(timezone.utc).isoformat(),
                               "last_window_key": window_key,
                               "last_order_id": None if oid=="DRY" else oid})
                tstate.pop("first_entry_utc", None)
                tstate.pop("entry_price", None)
                tstate.pop("entry_high", None)
                tstate.pop("entry_qty", None)
                tstate.pop("entry_basis_source", None)
                tstate.pop("entry_conf", None)
                tstate.pop("entry_conf_scale", None)
                tstate.pop("pyramid_layer", None)
                tstate.pop("tp_levels_hit", None)
                _clear_protective_stop(tstate)
                state[ticker] = tstate
                _state_save(state)
                if oid != "DRY":
                    _apply_loss_streak_pause_after_sell()
                return

        # #2 阶梯止盈: 涨 +15%/+30%/+50% 各卖 30%/30%/40%
        gain = (cur_price - entry_price) / entry_price
        tp_hit = set(tstate.get("tp_levels_hit", []))
        original_qty = int(tstate.get("entry_qty") or pos_qty_check)
        for thresh, sell_frac, label in _tp_levels(ticker):
            if gain >= thresh and label not in tp_hit:
                qty = max(1, int(original_qty * sell_frac))
                qty = min(qty, pos_qty_check)
                if qty <= 0: break
                tag = f"[TAKE-PROFIT {label} (+{gain*100:.1f}% from ${entry_price:.2f})]"
                oid = _place(ticker, TrdSide.SELL, qty, cur_price, tag=tag,
                             buffer=win_cfg.get("buffer", 0.005),
                             fill_outside_rth=win_cfg.get("fill_outside_rth", False))
                if oid:
                    tp_hit.add(label)
                    tstate["tp_levels_hit"] = list(tp_hit)
                    tstate.update({"last_action":f"TP_{label}","last_qty":qty,
                                   "last_price":cur_price,
                                   "last_time_utc": datetime.now(timezone.utc).isoformat(),
                                   "last_window_key": window_key,
                                   "last_order_id": None if oid=="DRY" else oid})
                    state[ticker] = tstate
                    _state_save(state)
                    if oid != "DRY":
                        _apply_loss_streak_pause_after_sell()
                    return
                break

    # High-water marks are risk state too; persist them even when the current
    # decision is HOLD/low-confidence and no order is placed.
    if risk_state_dirty:
        state[ticker] = tstate
        _state_save(state)

    # From here down are signal-driven orders. Discipline exits above must not
    # depend on action membership, confidence, loss pause, or universe sizing.
    if action not in ORDER_ACTIONS:
        # Claude autonomy A方案: rules 说 HOLD/CAUTION 但 Claude ai_target 说 buy/watch_buy
        # → 打 Discord alert，不自动执行，让用户决定
        try:
            ai_t = _load_ai_target_safe(ticker)
            if (
                ai_t
                and _ai_target_matches_market(ticker, ai_t, cur_price)
                and ai_t.get("action") in ("watch_buy", "buy")
            ):
                entry = ai_t.get("entry_ref")
                stop  = ai_t.get("stop_ref")
                _notify_claude_rules_conflict(ticker, action, ai_t, entry, stop, cur_price, window_key)
        except Exception:
            pass
        return

    if action in BUY_ACTIONS:
        chase_signals = extended_chase_signals(mkt)
        if len(chase_signals) >= 2:
            logger.warning(
                f"[trader] SKIP BUY {ticker}: extended chase guard "
                f"({', '.join(chase_signals)})"
            )
            return

    # 门槛按 /10 量程定义；实际 conf 可能是 /5（TECH_ONLY）→ 等比缩放
    try:
        from decision_agent import _conf_scale
        scale = _conf_scale()
    except Exception:
        scale = 10
    conf_min = confidence_min(window, scale)
    if conf < conf_min:
        logger.info(f"[trader] {ticker} {action} conf {conf}/{scale} < min {conf_min:.1f} → 跳过")
        return

    # ── 连续亏损暂停（P4.2）：BUY 全部跳过；SELL / REDUCE / 风控仍允许 ─────
    if action in BUY_ACTIONS:
        paused, p_reason = _is_loss_streak_paused(state)
        active_probe = (
            is_sim_active_trading()
            and (action in PROBE_ONLY_ACTIONS or action == "WATCH_BUY")
        )
        if paused and not active_probe:
            logger.info(f"[trader] {ticker} {action} 跳过：连续亏损暂停 ({p_reason})")
            return
        if paused and active_probe:
            logger.info(f"[trader] {ticker} 连亏暂停中，但 SIM_ACTIVE 允许小试探仓 ({p_reason})")

    size_usd = 0.0
    if action in BUY_ACTIONS:
        size_usd = _position_size_usd(ticker, conf=conf, action=action)
        # 未配置的动态卫星票必须在今日 picks；显式加入标的已由仓位函数放行。
        if not is_core and size_usd == 0:
            return

    # #3 修复 (v2): REDUCE 后自动 re-BUY — 回到 vol-target 目标仓位，不只是上次卖量
    if (action in BUY_ACTIONS
            and tstate.get("last_action") == "REDUCE"
            and tstate.get("last_price")):
        try:
            last_time = datetime.fromisoformat(tstate["last_time_utc"])
            hours_since = (datetime.now(timezone.utc) - last_time).total_seconds() / 3600
            last_price = float(tstate["last_price"])
            cur_price = float(price)
            bounce = (cur_price - last_price) / last_price
            if (hours_since < REBUY_LOOKBACK_HOURS
                    and bounce >= _rebuy_bounce_pct(ticker)
                    and not tstate.get("rebuy_done")):
                # v2: 算出 vol-target 应有仓位，与当前差额就是 rebuy 量
                target_size_usd = size_usd
                target_qty = int(target_size_usd // cur_price)
                current_qty = _position_qty(ticker)
                rebuy_qty = max(0, target_qty - current_qty)
                if rebuy_qty > 0:
                    rb_oid = _place(
                        ticker, TrdSide.BUY, rebuy_qty, cur_price,
                        tag=f"[REBUY → vol-target. bounce {bounce*100:+.1f}%, 现有 {current_qty}→目标 {target_qty}]",
                        buffer=win_cfg.get("buffer", 0.005),
                        fill_outside_rth=win_cfg.get("fill_outside_rth", False),
                    )
                    if rb_oid:
                        tstate.update({
                            "rebuy_done": True,
                            "last_action": "REBUY",
                            "last_qty": rebuy_qty,
                            "last_price": cur_price,
                            "last_time_utc": datetime.now(timezone.utc).isoformat(),
                            "last_window_key": window_key,
                            "last_order_id": None if rb_oid == "DRY" else rb_oid,
                        })
                        state[ticker] = tstate
                        _state_save(state)
                        return
        except (ValueError, KeyError):
            pass

    pos_qty = _position_qty(ticker)
    side = qty = None
    stop = None

    if action in BUY_ACTIONS:
        if pos_qty > 0:
            # #1 Pyramid 加仓: 已持仓时若 conf 比入场 conf 高 ≥1 → 加 50% 原仓
            entry_conf = _entry_conf_on_scale(tstate, scale, conf_min)
            layer = int(tstate.get("pyramid_layer") or 1)
            if (conf >= entry_conf + 1
                    and layer < PYRAMID_MAX_LAYERS
                    and size_usd > 0):
                add_qty = int((size_usd * PYRAMID_ADD_FRAC) // price)
                if add_qty > 0:
                    tag = f"[PYRAMID L{layer+1} (conf {entry_conf}→{conf})]"
                    oid = _place(ticker, TrdSide.BUY, add_qty, float(price), tag=tag,
                                 buffer=win_cfg.get("buffer", 0.005),
                                 fill_outside_rth=win_cfg.get("fill_outside_rth", False))
                    if oid:
                        tstate["pyramid_layer"] = layer + 1
                        tstate["entry_conf"]    = conf   # 新 layer 用新 conf
                        tstate["entry_conf_scale"] = scale
                        tstate.update({"last_action":"PYRAMID","last_qty":add_qty,
                                       "last_price":float(price),
                                       "last_time_utc": datetime.now(timezone.utc).isoformat(),
                                       "last_window_key": window_key,
                                       "last_order_id": None if oid=="DRY" else oid})
                        state[ticker] = tstate
                        _state_save(state)
                    return
            logger.info(f"[trader] {ticker} {action} 但已有 {pos_qty} 仓位 (layer {layer}, conf={entry_conf}), 跳过加仓")
            return
        # 杠杆对子去重：账户里已有"对子的另一只"则跳过，避免叠杠杆
        for paired in LEVERAGED_PAIRS.get(ticker, []):
            paired_qty = _position_qty(paired)
            if paired_qty > 0:
                logger.info(
                    f"[trader] {ticker} BUY 跳过：账户已持有杠杆对 {paired} "
                    f"({paired_qty} 股)，避免叠杠杆"
                )
                return
        # 反向对子去重：账户里已有反向 ETF 则跳过，避免内部对冲
        for inverse in INVERSE_PAIRS.get(ticker, []):
            inv_qty = _position_qty(inverse)
            if inv_qty > 0:
                logger.info(
                    f"[trader] {ticker} BUY 跳过：账户已持反向对 {inverse} "
                    f"({inv_qty} 股)，避免内部对冲"
                )
                return
        if size_usd <= 0:
            return
        # AI target 仅在仍贴近现价时覆盖（entry_ref + stop_ref + use_limit）
        ai_t = _load_ai_target_safe(ticker)
        if ai_t and not _ai_target_matches_market(ticker, ai_t, float(price)):
            ai_t = None
        ai_price = float(price)  # 默认用当前价撮合
        ai_use_limit = False
        ai_stop_override = None
        if ai_t and ai_t.get("action") in ("watch_buy", "buy"):
            ai_entry = ai_t.get("entry_ref")
            if ai_entry and ai_t.get("use_limit") and ai_entry < float(price):
                # 等回踩：用 entry_ref 作为限价（必须低于当前价才合理）
                ai_price = float(ai_entry)
                ai_use_limit = True
                logger.info(f"[trader] {ticker} AI 限价等回踩 ${ai_entry:.2f} (现价 ${price:.2f})")
            candidate_stop = ai_t.get("stop_ref")
            # 止损必须低于本轮实际执行基准；旧目标中的 stop 若已高于现价，
            # 不能继续沿用，否则会在成交后立刻触发错误卖出。
            if candidate_stop and float(candidate_stop) < ai_price:
                ai_stop_override = candidate_stop
        qty  = int(size_usd // ai_price)
        side = TrdSide.BUY
        # stop_ref 优先级：AI > decision_agent > None
        stop = ai_stop_override or decision.get("stop_ref")
    elif action in SELL_ACTIONS:
        if pos_qty <= 0:
            logger.info(f"[trader] {ticker} {action} 但无持仓，跳过")
            return
        # Sitting 确认期（P4.1）：信号驱动的 SELL 不允许在持仓 < N 天内触发
        # 硬性 stop（trailing/crisis/earnings guard）仍在上面早 return 路径放行
        if _within_sitting_window(tstate):
            logger.info(f"[trader] {ticker} SELL 跳过：sitting 确认期内（持仓 < {SITTING_MIN_DAYS} 天，信号 SELL 不放行）")
            return
        qty  = pos_qty
        side = TrdSide.SELL
    elif action in REDUCE_ACTIONS:
        if pos_qty <= 0:
            logger.info(f"[trader] {ticker} REDUCE 但无持仓，跳过")
            return
        # Sitting：REDUCE 在持仓 < N 天内也跳过（除非是 crisis regime）
        if _within_sitting_window(tstate) and (decision or {}).get("regime") != "crisis":
            logger.info(f"[trader] {ticker} REDUCE 跳过：sitting 确认期内（非 crisis）")
            return
        qty  = max(1, pos_qty // 2)
        side = TrdSide.SELL
    else:
        return                                     # HOLD / CAUTION

    # AI 限价模式：用 entry_ref 作为撮合价，buffer 收紧到 0.001（精确挂在 AI 目标价）
    place_price = float(price)
    place_buffer = win_cfg.get("buffer", 0.005)
    place_tag = f"[{action} conf={conf} win={window} {'core' if is_core else 'sat'}]"
    if side == TrdSide.BUY and 'ai_use_limit' in locals() and ai_use_limit:
        place_price = ai_price
        place_buffer = 0.001  # 精确挂在 AI 目标价
        place_tag = f"[{action} conf={conf} win={window} AI-LIMIT@${ai_price:.2f} {'core' if is_core else 'sat'}]"
    oid = _place(
        ticker, side, qty, place_price,
        tag=place_tag,
        buffer=place_buffer,
        fill_outside_rth=win_cfg.get("fill_outside_rth", False),
        decision=decision, mkt=mkt, window=window,
        extra={"is_core": is_core, "size_usd": size_usd},
        resting_limit=(
            side == TrdSide.BUY
            and 'ai_use_limit' in locals()
            and ai_use_limit
        ),
    )
    if oid is None:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    is_first_entry = side == TrdSide.BUY and not tstate.get("first_entry_utc")
    execution_basis = float(price)
    if is_first_entry:
        fallback_basis = float(place_price)
        if win_cfg.get("fill_outside_rth", False):
            fallback_basis = _get_realtime_price(ticker, fallback_basis)
        execution_basis = (
            _position_cost_price(ticker, fallback_basis)
            if oid != "DRY"
            else fallback_basis
        )
    tstate.update({
        "is_core":         is_core,
        "last_action":     action,
        "last_side":       "BUY" if side == TrdSide.BUY else "SELL",
        "last_qty":        qty,
        "last_price":      execution_basis if side == TrdSide.BUY else float(price),
        "last_time_utc":   now_iso,
        "last_order_id":   None if oid == "DRY" else oid,
        "last_window_key": window_key,
    })
    if (
        side == TrdSide.BUY
        and oid != "DRY"
        and 'ai_use_limit' in locals()
        and ai_use_limit
    ):
        tstate.update({
            "managed_entry_order_id": str(oid),
            "managed_entry_kind": "ai_limit",
            "managed_entry_ref": float(ai_price),
            "managed_entry_stop_ref": stop,
            "managed_entry_target_ts": (ai_t or {}).get("_source_ts"),
            "managed_entry_updated_utc": now_iso,
        })
    if is_first_entry:
        tstate["first_entry_utc"] = now_iso
        # 首次入场: 记 entry 数据供 TP/SL/Pyramid 用
        tstate["entry_price"]    = execution_basis
        tstate["entry_high"]     = execution_basis
        tstate["entry_basis_source"] = (
            "broker_cost_or_execution_reference"
            if oid != "DRY"
            else "execution_reference"
        )
        tstate["entry_qty"]      = qty
        tstate["entry_conf"]     = conf
        tstate["entry_conf_scale"] = scale
        tstate["pyramid_layer"]  = 1
        tstate["tp_levels_hit"]  = []
    if side == TrdSide.SELL and pos_qty == qty:
        # 清仓后清掉所有 entry 元数据
        for k in ("first_entry_utc","entry_price","entry_high","entry_qty",
                  "entry_basis_source","entry_conf","entry_conf_scale",
                  "pyramid_layer","tp_levels_hit"):
            tstate.pop(k, None)
        _clear_protective_stop(tstate)
    # 每次新动作清掉 rebuy_done 标记 (允许下次 REDUCE 后再 rebuy)
    if action == "REDUCE":
        tstate.pop("rebuy_done", None)

    if side == TrdSide.BUY and stop and stop > 0:
        stop_price = float(stop)
        if oid == "DRY":
            stop_oid = None
            stop_mode = "dry_run"
        else:
            stop_oid = _place_stop_loss(ticker, qty, stop_price)
            stop_mode = "broker" if stop_oid else "software"
            if not stop_oid:
                logger.error(
                    f"[trader] {ticker} broker stop unavailable; armed software stop "
                    f"at ${stop_price:.2f}"
                )
        tstate.update({
            "protective_stop_price": stop_price,
            "protective_stop_mode": stop_mode,
            "protective_stop_order_id": stop_oid,
            "protective_stop_updated_utc": now_iso,
        })
    state[ticker] = tstate
    _state_save(state)

    if side == TrdSide.SELL and oid != "DRY":
        _apply_loss_streak_pause_after_sell()

# ---------- Universe lifecycle ----------

def _list_satellite_positions() -> list[dict]:
    """返回当前账户的卫星持仓（排除核心仓 + 0 仓位）。"""
    ctx = _ctx_get()
    ret, pos = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    if ret != RET_OK or pos is None or pos.empty:
        return []
    out = []
    for _, row in pos.iterrows():
        code = row.get("code")
        qty  = float(row.get("can_sell_qty", 0))
        if qty <= 0 or code in CORE_TICKERS:
            continue
        out.append({"code": code, "qty": qty,
                    "nominal_price": float(row.get("nominal_price", 0))})
    return out


def apply_universe(picks_result: dict) -> dict:
    with _TRADER_LOCK:
        return _apply_universe_unlocked(picks_result)


def _apply_universe_unlocked(picks_result: dict) -> dict:
    """
    orchestrator 在 pre-open 算完 picks 后调用。
    步骤：
      1. 持久化今日 picks
      2. 对每个新 pick：若已持仓 → 更新 last_seen_in_picks_utc
      3. 计算踢出：当前卫星持仓 - 今日 picks，按 first_entry_utc 旧→新排序，
         踢到 (卫星持仓 + 待新建) ≤ MAX_SATELLITE_POSITIONS 为止
      4. SELL 全平踢出名单（受 DRY_RUN 控制）
    返回 {"kept": [...], "kicked": [...], "new": [...]}
    """
    picks = picks_result.get("picks") or []
    pick_tickers = {p["ticker_full"] for p in picks}

    today = datetime.now(timezone.utc).date().isoformat()
    _universe_save({
        "date":   today,
        "regime": picks_result.get("regime"),
        "ts":     picks_result.get("ts"),
        "picks":  picks,
    })

    state = _state_load()
    now_iso = datetime.now(timezone.utc).isoformat()
    for tk in pick_tickers:
        ts = state.setdefault(tk, {})
        ts["last_seen_in_picks_utc"] = now_iso
        ts["is_core"] = False
    _state_save(state)

    sat_positions = _list_satellite_positions()
    held = {p["code"]: p for p in sat_positions}
    # 候选踢出 = 已持仓的动态卫星 - 今日 picks。显式加入的标的是长期管理对象，
    # 即使当天不在动态 picks 也不能被轮换器强制卖出。
    kickout_candidates = [
        code for code in held
        if code not in pick_tickers and code not in TRADE_ELIGIBLE_TICKERS
    ]
    # 按 first_entry_utc 排序，最老的优先踢
    kickout_candidates.sort(
        key=lambda c: (state.get(c, {}) or {}).get("first_entry_utc") or ""
    )

    new_picks_to_open = [tk for tk in pick_tickers if tk not in held]
    keep_existing_sat = [
        tk for tk in held
        if tk in pick_tickers or tk in TRADE_ELIGIBLE_TICKERS
    ]
    # 不踢的话最终的卫星持仓数 = 全部现持仓 + 全部新 picks
    # （现持仓里"在 picks"的不变；现持仓里"不在 picks"的依然占位；再加新进入的）
    projected_total = len(held) + len(new_picks_to_open)

    kicked = []
    while projected_total > MAX_SATELLITE_POSITIONS and kickout_candidates:
        victim = kickout_candidates.pop(0)
        kicked.append(victim)
        projected_total -= 1   # 踢一个腾一个位

    # 踢出 = 立即 SELL 全平（不等 SELL 信号触发，因为这是强制周转规则）
    live_kickout_sell = False
    for code in kicked:
        info = held[code]
        qty = int(info["qty"])
        price = info["nominal_price"]
        if qty <= 0 or price <= 0:
            continue
        oid = _place(code, TrdSide.SELL, qty, price,
                     tag=f"[kickout 不在新 picks 内 + 超出 MAX_SATELLITE]")
        if oid:
            live_kickout_sell = live_kickout_sell or oid != "DRY"
            ts = state.setdefault(code, {})
            ts.update({
                "last_action":     "KICKOUT",
                "last_side":       "SELL",
                "last_qty":        qty,
                "last_price":      price,
                "last_time_utc":   now_iso,
                "last_order_id":   None if oid == "DRY" else oid,
            })
            ts.pop("first_entry_utc", None)
            _clear_protective_stop(ts)
    _state_save(state)
    if live_kickout_sell:
        _apply_loss_streak_pause_after_sell()

    # 踢出后真正留下来的卫星持仓 = (现持仓 - 被踢的) + 新开
    survivors = [tk for tk in held if tk not in kicked]
    return {
        "kept":   sorted(keep_existing_sat),
        "kicked": sorted(kicked),
        "new":    sorted(new_picks_to_open),
        "held_after": sorted(set(survivors) | set(new_picks_to_open)),
    }


def get_active_tickers() -> list[str]:
    """orchestrator 用：显式加入标的 + 今日 picks + 还在持仓的动态卫星老仓。"""
    uni = _universe_load()
    pick_tickers = [p["ticker_full"] for p in (uni.get("picks") or [])]
    held = {p["code"] for p in _list_satellite_positions()}
    return sorted(set(TRADE_ELIGIBLE_TICKERS) | set(pick_tickers) | held)


def refresh_nav_peak() -> None:
    with _TRADER_LOCK:
        return _refresh_nav_peak_unlocked()


def _refresh_nav_peak_unlocked() -> None:
    """每个 cycle 调用一次: 查账户总值, 更新 NAV peak (用于 drawdown floor)。"""
    try:
        ctx = _ctx_get()
        ret, info = ctx.accinfo_query(trd_env=TRD_ENV, acc_id=ACC_ID, currency="USD")
        if ret == RET_OK and info is not None and not info.empty:
            total = float(info.iloc[0].get("total_assets") or 0)
            if total > 0:
                _update_nav_peak(total)
    except Exception:
        pass
    try:
        refresh_execution_ledger()
    except Exception as exc:
        logger.warning(f"[trader] execution reconciliation skipped: {exc}")


# ---------- CLI ----------

def _cli_status():
    print(f"DRY_RUN={DRY_RUN}  acc_id={ACC_ID}  env={TRD_ENV}")
    state = _state_load()
    print("\n--- trader_state.json ---")
    print(json.dumps(state, indent=2, ensure_ascii=False) if state else "(空)")

    ctx = _ctx_get()
    print("\n--- 账户 ---")
    ret, info = ctx.accinfo_query(trd_env=TRD_ENV, acc_id=ACC_ID, currency="USD")
    if ret == RET_OK and not info.empty:
        r = info.iloc[0]
        print(f"  total={r.get('total_assets')}  cash={r.get('cash')}  "
              f"market_val={r.get('market_val')}  power={r.get('power')}")
    print("\n--- 持仓 ---")
    ret, pos = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    if ret == RET_OK and pos is not None and not pos.empty:
        cols = ["code", "qty", "can_sell_qty", "cost_price", "nominal_price", "market_val", "pl_ratio"]
        keep = [c for c in cols if c in pos.columns]
        print(pos[keep].to_string(index=False))
    else:
        print("(无持仓)")
    print("\n--- 未结订单 ---")
    ret, od = ctx.order_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    if ret == RET_OK and od is not None and not od.empty:
        pending = od[od["order_status"].isin(["SUBMITTED", "SUBMITTING", "WAITING_SUBMIT"])]
        if not pending.empty:
            print(pending[["order_id", "code", "trd_side", "qty", "price", "order_status"]].to_string(index=False))
        else:
            print("(无未结)")
    else:
        print("(无)")


def submit_rebalance_order(ticker: str, side: str, qty: int, price: float,
                            reason: str = "auto_rebalance") -> str | None:
    """公开 API: auto_rebalance 模块用这个提交订单, 不走 decision engine + gate.

    ticker: 'US.SHY' 或 'SHY' (自动加前缀)
    side:   'BUY' | 'SELL'
    qty:    整数股数
    price:  限价 (通常是当前 last_px 附近)
    reason: 日志里的说明

    Returns order_id (成功) 或 None (失败).
    DRY_RUN 模式下只 log 不发单.
    """
    code = ticker if ticker.startswith("US.") else f"US.{ticker}"
    side_enum = TrdSide.BUY if side.upper() == "BUY" else TrdSide.SELL
    side_label = side.upper()
    tag = f"[REBALANCE {reason}]"

    if DRY_RUN:
        logger.info(f"[trader-DRY] {side_label} {qty} {code} @ {price:.2f} {tag}")
        _log_trade(code, side_label, qty, round(price, 2), "DRY", tag,
                   decision={"action": f"REBALANCE_{side_label}",
                             "reason": reason, "engine": "rebalance"},
                   mkt={"price": price}, window="pre-close")
        return "DRY"

    ctx = _ctx_get()
    try:
        ret, info = ctx.place_order(
            price=round(price, 2), qty=float(qty), code=code,
            trd_side=side_enum, order_type=OrderType.NORMAL,
            trd_env=TRD_ENV, acc_id=ACC_ID,
        )
    except Exception as exc:
        logger.error(f"[rebalance-submit] {code} FAIL: {exc}")
        return None
    if ret != RET_OK:
        logger.error(f"[rebalance-submit] {side_label} {qty} {code} @ {price:.2f}: {info}")
        return None
    oid = info.iloc[0]["order_id"] if hasattr(info, "iloc") else str(info)
    logger.info(f"[trader-LIVE] {side_label} {qty} {code} @ {price:.2f} "
                f"order={oid} {tag}")
    _log_trade(code, side_label, qty, round(price, 2), str(oid), tag,
               decision={"action": f"REBALANCE_{side_label}",
                         "reason": reason, "engine": "rebalance"},
               mkt={"price": price}, window="pre-close")
    return str(oid)


def _cli_flatten():
    ctx = _ctx_get()
    ret, od = ctx.order_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    if ret == RET_OK and od is not None and not od.empty:
        pending = od[od["order_status"].isin(["SUBMITTED", "SUBMITTING", "WAITING_SUBMIT"])]
        for _, row in pending.iterrows():
            ctx.modify_order(modify_order_op=ModifyOrderOp.CANCEL,
                             order_id=row["order_id"], qty=0, price=0,
                             trd_env=TRD_ENV, acc_id=ACC_ID)
            print(f"  CANCEL {row['code']} order={row['order_id']}")
    time.sleep(1)
    ret, pos = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    if ret != RET_OK or pos is None or pos.empty:
        print("(无持仓)"); return
    pos = pos[(pos["qty"] > 0) & (pos["position_side"] == "LONG")]
    for _, row in pos.iterrows():
        code, qty, price = row["code"], float(row["can_sell_qty"]), float(row["nominal_price"])
        r, info = ctx.place_order(price=round(price, 2), qty=qty, code=code,
                                  trd_side=TrdSide.SELL, order_type=OrderType.NORMAL,
                                  trd_env=TRD_ENV, acc_id=ACC_ID)
        oid = info.iloc[0]["order_id"] if r == RET_OK else "FAIL"
        print(f"  SELL {qty:>6.0f} {code:<10} @ {price:>8.2f}  -> {oid}")


def _cli_reset():
    if STATE_PATH.exists():
        STATE_PATH.unlink()
        print(f"已删除 {STATE_PATH}")
    else:
        print("state 不存在，无需清理")


def _cli_picks():
    uni = _universe_load()
    print(f"DRY_RUN={DRY_RUN}  caps: total<={MAX_TOTAL_POSITIONS}  satellite<={MAX_SATELLITE_POSITIONS}")
    print(f"\n--- universe_state.json ---")
    if not uni.get("picks"):
        print(f"(无 picks; date={uni.get('date')} regime={uni.get('regime')})")
        return
    print(f"date={uni['date']}  regime={uni['regime']}  ts={uni.get('ts')}")
    print(f"\n  {'#':<3} {'ticker':<10} {'z(σ)':>7} {'α-t':>6} {'$':>6}  {'主题':<14} 提示")
    for i, p in enumerate(uni["picks"], 1):
        print(f"  {i:<3} {p['ticker_full']:<10} {p.get('z',0):>+6.2f} "
              f"{p.get('alpha_t',0):>+6.2f} ${p.get('size_usd',0):>5.0f}  "
              f"{p.get('theme','?'):<14} {p.get('hint','')}")
    print(f"\n--- 当前活跃 tickers (core + 持仓卫星 + 今日 picks) ---")
    print("  " + ", ".join(get_active_tickers()))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        {
            "status":  _cli_status,
            "flatten": _cli_flatten,
            "reset":   _cli_reset,
            "picks":   _cli_picks,
        }[cmd]()
    except KeyError:
        print(f"用法: python paper_trader.py {{status|flatten|reset|picks}}"); sys.exit(2)
    finally:
        _ctx_close()
