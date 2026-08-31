"""
Backtest Engine — 两档系统回测：
  · Lite  : 信号方向命中率（5/10 天后价格 vs 信号方向）
  · Mid   : 完整 trader 模拟（10% 仓位、REDUCE 50%、kickout）+ P&L 曲线 vs B&H

数据源 yfinance daily K（OHLCV）。简化假设：
  · regime 固定 bull_trending（避免引入未来 SOX 因子）
  · events 取硬编码经济日历（events_watch 已有）
  · macro 用固定 baseline（VIX=18, FG=55, T10Y2Y=0.4）

输出：signals/backtest_report.md
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import SIGNALS_DIR
from trading_contracts import (
    BUY_ACTIONS,
    CRISIS_PROBE_TARGET_VOL,
    PROBE_ONLY_ACTIONS,
    confidence_min,
    confidence_multiplier,
)


# ── 配置 ────────────────────────────────────────────────────────────────────
# 从 config 派生: TICKERS + GLD (backtest 需覆盖 hedge 基准)
# 修改交易品种应改 config.TICKERS, 这里自动跟进
from config import TICKERS as _CFG_TICKERS
TICKERS = [t.replace("US.", "") for t in _CFG_TICKERS] + ["GLD"]
BACKTEST_DAYS = 60      # 回测最近 N 个交易日
FORWARD_DAYS  = [1, 5, 10]   # 信号后的 N 天检查
INITIAL_CASH  = 1_500_000    # Mid 模拟起始资金


# ── 数据加载 ────────────────────────────────────────────────────────────────
def load_history(ticker: str, days: int = 350) -> pd.DataFrame:
    """yfinance 拉一段 daily K，返回带常用列的 DataFrame。"""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=f"{days}d", interval="1d", auto_adjust=True)
    if df.empty:
        return df
    df = df.rename(columns={"Open":"open","High":"high","Low":"low",
                            "Close":"close","Volume":"volume"})
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    return df[["open","high","low","close","volume"]]


# ── 指标计算（与 market_watch 对齐） ────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """加 RSI/MA/CCI/BB/量比 + zone 字段，跟 market_watch._compute_indicators 对齐。"""
    d = df.copy()
    # MA
    d["ma20"] = d["close"].rolling(20).mean()
    d["ma50"] = d["close"].rolling(50).mean()
    d["ma200"] = d["close"].rolling(200).mean()
    # RSI 14
    delta = d["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    d["rsi_14"] = 100 - (100 / (1 + rs))
    # CCI 20
    tp = (d["high"] + d["low"] + d["close"]) / 3
    sma = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    d["cci_20"] = (tp - sma) / (0.015 * mad.replace(0, np.nan))
    # Bollinger Bands
    bb_std = d["close"].rolling(20).std()
    d["bb_upper"] = d["ma20"] + 2*bb_std
    d["bb_lower"] = d["ma20"] - 2*bb_std
    d["bb_pct"] = (d["close"] - d["bb_lower"]) / (d["bb_upper"] - d["bb_lower"]).replace(0, np.nan)
    # vol_ratio: 当日量 / 20 日均量
    d["vol_ratio"] = d["volume"] / d["volume"].rolling(20).mean()
    # pct_chg + 前一日 pct_chg（V 型反转判定用）
    d["pct_chg"] = d["close"].pct_change() * 100
    d["prev_pct"] = d["pct_chg"].shift(1)
    # 52w 高
    d["high_52w"] = d["high"].rolling(252).max()
    d["dist_from_high"] = (d["close"] - d["high_52w"]) / d["high_52w"] * 100
    d["is_new_52w_high"] = (d["close"] >= d["high_52w"] * 0.999).astype(int)
    # MACD
    ema12 = d["close"].ewm(span=12, adjust=False).mean()
    ema26 = d["close"].ewm(span=26, adjust=False).mean()
    d["macd_dif"] = ema12 - ema26
    d["macd_dea"] = d["macd_dif"].ewm(span=9, adjust=False).mean()
    d["macd_hist"]= d["macd_dif"] - d["macd_dea"]
    _dif_p = d["macd_dif"].shift(1); _dea_p = d["macd_dea"].shift(1)
    d["macd_signal"] = "none"
    d.loc[(_dif_p < _dea_p) & (d["macd_dif"] >= d["macd_dea"]), "macd_signal"] = "golden"
    d.loc[(_dif_p > _dea_p) & (d["macd_dif"] <= d["macd_dea"]), "macd_signal"] = "death"
    # ADX
    high, low, cls = d["high"], d["low"], d["close"]
    up_move, down_move = high.diff(), -low.diff()
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high-low, (high-cls.shift()).abs(), (low-cls.shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    plus_di  = 100 * pd.Series(plus_dm,  index=d.index).rolling(14).mean() / atr14
    minus_di = 100 * pd.Series(minus_dm, index=d.index).rolling(14).mean() / atr14
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    d["adx_14"] = dx.rolling(14).mean()
    return d


def _classify_rsi_zone(rsi):
    if pd.isna(rsi): return "neutral"
    if rsi < 35: return "oversold"
    if rsi > 70: return "overbought"
    return "neutral"


def _classify_vol_zone(vr):
    if pd.isna(vr): return "normal"
    if vr < 0.8: return "shrink"
    if vr > 1.25: return "expand"
    return "normal"


def _classify_cci_zone(cci):
    if pd.isna(cci): return "neutral"
    if cci < -100: return "oversold"
    if cci > 100: return "overbought"
    return "neutral"


def _classify_bb_zone(bp):
    if pd.isna(bp): return "normal"
    if bp > 1.0: return "above"
    if bp < 0.0: return "below"
    return "normal"


def _classify_macd_zone(dif, dea):
    if pd.isna(dif) or pd.isna(dea): return "neutral"
    if dif > 0 and dea > 0: return "bull"
    if dif < 0 and dea < 0: return "bear"
    return "neutral"


def _classify_macd_signal(dif, dea, prev_dif, prev_dea):
    if any(pd.isna(x) for x in (dif, dea, prev_dif, prev_dea)): return "none"
    if prev_dif < prev_dea and dif >= dea: return "golden"
    if prev_dif > prev_dea and dif <= dea: return "death"
    return "none"


def _classify_adx_zone(adx):
    if pd.isna(adx): return "weak"
    if adx >= 40: return "extreme"
    if adx >= 25: return "strong"
    if adx >= 20: return "moderate"
    return "weak"


def _classify_pct_zone(p):
    if pd.isna(p): return "normal"
    if p <= -5: return "crash"
    if p <= -2: return "drop"
    if p <= -1: return "mild_drop"
    if p >= 5: return "surge"
    if p >= 2: return "pop"
    if p >= 1: return "mild_pop"
    return "normal"


def build_mkt(ticker_full: str, row: pd.Series) -> dict:
    """从一行历史数据构造跟 market_watch 一致的 mkt dict 供 decision_agent 使用。"""
    rsi = row.get("rsi_14")
    cci = row.get("cci_20")
    ma20, ma50 = row.get("ma20"), row.get("ma50")
    close = row.get("close")
    trend = "up" if (not pd.isna(ma20) and close > ma20) else "down"
    ma_stack = "bull" if (not pd.isna(ma50) and not pd.isna(ma20) and ma20 > ma50) else "bear"
    return {
        "ticker":           ticker_full,
        "price":            float(close) if not pd.isna(close) else None,
        "pct_chg":          float(row.get("pct_chg", 0) or 0),
        "pct_chg_zone":     _classify_pct_zone(row.get("pct_chg")),
        "prev_pct":         float(row.get("prev_pct", 0)) if not pd.isna(row.get("prev_pct")) else 0,
        "rsi_14":           float(rsi) if not pd.isna(rsi) else 50,
        "rsi_zone":         _classify_rsi_zone(rsi),
        "cci_20":           float(cci) if not pd.isna(cci) else 0,
        "cci_zone":         _classify_cci_zone(cci),
        "vol_ratio":        float(row.get("vol_ratio", 1.0) or 1.0),
        "vol_zone":         _classify_vol_zone(row.get("vol_ratio")),
        "ma20":             float(ma20) if not pd.isna(ma20) else 0,
        "ma50":             float(ma50) if not pd.isna(ma50) else 0,
        "trend":            trend,
        "ma_stack":         ma_stack,
        "bb_pct":           float(row.get("bb_pct", 0.5) or 0.5),
        "bb_upper":         float(row.get("bb_upper", 0) or 0),
        "bb_lower":         float(row.get("bb_lower", 0) or 0),
        "bb_zone":          _classify_bb_zone(row.get("bb_pct")),
        "psar_signal":      "none",      # 历史 PSAR 不算（简化）
        "is_new_52w_high":  bool(row.get("is_new_52w_high", 0)),
        "dist_from_high":   float(row.get("dist_from_high", 0) or 0),
        # MACD / ADX
        "macd_dif":         float(row.get("macd_dif")  if not pd.isna(row.get("macd_dif"))  else 0),
        "macd_dea":         float(row.get("macd_dea")  if not pd.isna(row.get("macd_dea"))  else 0),
        "macd_hist":        float(row.get("macd_hist") if not pd.isna(row.get("macd_hist")) else 0),
        "macd_zone":        _classify_macd_zone(row.get("macd_dif"), row.get("macd_dea")),
        "macd_signal":      str(row.get("macd_signal") or "none"),
        "adx_14":           float(row.get("adx_14")    if not pd.isna(row.get("adx_14"))    else 0),
        "adx_zone":         _classify_adx_zone(row.get("adx_14")),
    }


# ── Lite: 信号方向命中率 ──────────────────────────────────────────────────
@dataclass
class SignalEvent:
    date:     str
    ticker:   str
    action:   str
    conf:     int
    price:    float
    forward:  dict[int, float]   # {1: ret_1d, 5: ret_5d, 10: ret_10d}
    win:      dict[int, bool | None]


def run_lite(tickers=None, days=BACKTEST_DAYS) -> dict:
    """对每个 ticker 跑过去 N 天 decision_agent，收集 BUY/SELL/REDUCE 信号 + 前向收益。"""
    from decision_agent import get_decision
    tickers = tickers or TICKERS
    fake_events = {"breaking_news": False, "days_to_event": 99,
                   "risk_level": "moderate", "gold_bias": "neutral",
                   "trump_signal": {"fallback": True}}  # 历史不能 retro-fit trump 推文
    fake_macro = {"vix": 18, "fg_score": 55, "t10y2y": 0.4}

    all_events: dict[str, list[SignalEvent]] = {}
    for tk in tickers:
        df = add_indicators(load_history(tk, days=days + 50))
        # 取最后 days 个有完整指标的交易日
        df = df.dropna(subset=["rsi_14","ma20","ma50","bb_pct","cci_20","vol_ratio"])
        if len(df) < days:
            df_test = df
        else:
            df_test = df.iloc[-days:]
        events = []
        full = "US." + tk
        for i in range(len(df_test) - 1):
            row = df_test.iloc[i]
            try:
                mkt = build_mkt(full, row)
                d = get_decision(mkt, fake_events, fake_macro,
                                 board_regime="bull_trending")
            except Exception:
                continue
            action = d.get("action", "HOLD")
            conf = d.get("confidence", 0)
            if action in ("HOLD", "CAUTION"):
                continue
            date_str = df_test.index[i].strftime("%Y-%m-%d")
            price0 = float(row["close"])
            # 前向收益
            fwd, win = {}, {}
            for n in FORWARD_DAYS:
                if i + n >= len(df_test):
                    fwd[n] = win[n] = None
                    continue
                price_n = float(df_test.iloc[i + n]["close"])
                ret = (price_n - price0) / price0 * 100
                fwd[n] = round(ret, 2)
                # 判定胜负
                if action in BUY_ACTIONS:
                    win[n] = ret > 0
                elif action in ("SELL", "REDUCE"):
                    win[n] = ret < 0
                else:
                    win[n] = None
            events.append(SignalEvent(date_str, tk, action, conf, price0, fwd, win))
        all_events[tk] = events
    return all_events


def report_lite(all_events: dict) -> list[str]:
    lines = ["", "+" + "="*74 + "+",
             "|  Lite 回测：decision_agent 信号方向命中率 (过去 60 个交易日)         |",
             "+" + "="*74 + "+"]
    for tk, events in all_events.items():
        lines.append(f"\n  === {tk} ===")
        if not events:
            lines.append(f"    (无信号)")
            continue
        # 按 action 分组
        by_action: dict[str, list[SignalEvent]] = {}
        for e in events:
            by_action.setdefault(e.action, []).append(e)
        lines.append(f"    总信号数: {len(events)}")
        for action, evs in by_action.items():
            cnt = len(evs)
            for n in FORWARD_DAYS:
                wins  = sum(1 for e in evs if e.win[n] is True)
                lose  = sum(1 for e in evs if e.win[n] is False)
                total = wins + lose
                wr = wins / total * 100 if total > 0 else 0
                avg_ret = np.mean([e.forward[n] for e in evs if e.forward[n] is not None])
                lines.append(f"      {action:<10} n={cnt:>3}  {n:>2}d 胜率 {wr:5.1f}% ({wins}/{total})  平均收益 {avg_ret:+6.2f}%")
    return lines


# ── Mid: 完整 trader 模拟 ────────────────────────────────────────────────
@dataclass
class Trade:
    date: str; ticker: str; side: str; qty: int; price: float; reason: str


@dataclass
class SimAccount:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)   # {ticker: {qty, cost}}
    history: list[Trade] = field(default_factory=list)
    nav_curve: list[tuple[str, float]] = field(default_factory=list)
    last_action_meta: dict[str, dict] = field(default_factory=dict)  # for re-BUY tracking

    def value(self, prices: dict[str, float]) -> float:
        mv = sum(p["qty"] * prices.get(tk, p["cost"]) for tk, p in self.positions.items())
        return self.cash + mv

    def buy(self, date, ticker, qty, price, reason="BUY"):
        cost = qty * price
        if cost > self.cash * 1.5:  # 简化 1.5x margin 上限
            return False
        self.cash -= cost
        cur = self.positions.get(ticker, {"qty":0, "cost":0})
        old_qty, old_cost = cur["qty"], cur["cost"]
        new_qty = old_qty + qty
        new_cost = (old_qty * old_cost + qty * price) / new_qty if new_qty else 0
        self.positions[ticker] = {"qty": new_qty, "cost": new_cost}
        self.history.append(Trade(date, ticker, "BUY", qty, price, reason))
        return True

    def sell(self, date, ticker, qty, price, reason="SELL"):
        cur = self.positions.get(ticker)
        if not cur or cur["qty"] < qty:
            return False
        self.cash += qty * price
        cur["qty"] -= qty
        if cur["qty"] == 0:
            del self.positions[ticker]
        self.history.append(Trade(date, ticker, "SELL", qty, price, reason))
        return True


def run_mid(tickers=None, days=BACKTEST_DAYS) -> dict:
    """完整模拟：每天对每个 ticker 算决策，按 trader 逻辑模拟下单。"""
    from decision_agent import _conf_scale, get_decision
    tickers = tickers or TICKERS
    fake_events = {"breaking_news": False, "days_to_event": 99,
                   "risk_level": "moderate", "gold_bias": "neutral",
                   "trump_signal": {"fallback": True}}  # 历史不能 retro-fit trump 推文
    fake_macro = {"vix": 18, "fg_score": 55, "t10y2y": 0.4}
    conf_scale = _conf_scale()
    conf_min = confidence_min("post-open", conf_scale)
    # 与 paper_trader 同步: vol-target sizing
    TARGET_PORT_VOL = {
        "bull_extended":  0.30, "bull_pulling":  0.25,
        "bull_trending":  0.22, "bull_chop":     0.15,
        "neutral_chop":   0.10, "neutral":        0.12, "overheated":    0.08,
        "risk_off":      0.08,
        "recession_risk": 0.05, "crisis":        0.00,
    }
    ASSET_VOL = {"US.TQQQ": 0.60, "US.SOXL": 0.80, "US.GLD": 0.15}
    POSITION_MAX  = 0.40
    # 杠杆缩放 sqrt(leverage)
    import math
    from config import LEVERAGE_FACTORS
    def _lev_sqrt(tk):
        return math.sqrt(LEVERAGE_FACTORS.get(tk, 1.0))

    def _vix_mult(v):
        if v is None: return 1.0
        if v < 13: return 1.8
        if v < 18: return 1.3
        if v < 22: return 0.8
        if v < 28: return 0.4
        if v < 35: return 0.1
        return 0.0

    def _position_fraction(full: str, regime: str, confidence: float,
                           power: float, action: str) -> float:
        """Shared sizing path for initial BUY, re-BUY, and Pyramid."""
        target_v = TARGET_PORT_VOL.get(regime, 0.12)
        is_probe = action in PROBE_ONLY_ACTIONS
        if target_v <= 0 and is_probe:
            target_v = CRISIS_PROBE_TARGET_VOL
        if target_v <= 0:
            return 0.0
        raw = target_v / ASSET_VOL.get(full, 0.40)
        drawdown = (power - INITIAL_CASH) / INITIAL_CASH if power < INITIAL_CASH else 0.0
        if drawdown <= -0.20:
            dd_mult = 0.0
        elif drawdown <= -0.15:
            dd_mult = 0.2
        elif drawdown <= -0.10:
            dd_mult = 0.4
        elif drawdown <= -0.05:
            dd_mult = 0.7
        else:
            dd_mult = 1.0
        conf_mult = confidence_multiplier(
            confidence, conf_scale, probe=is_probe
        )
        return min(raw * _vix_mult(18) * dd_mult * conf_mult, POSITION_MAX)

    # 准备所有 ticker 的历史数据 (对齐到共同日期范围)
    histories = {}
    for tk in tickers:
        df = add_indicators(load_history(tk, days=days + 60))
        df = df.dropna(subset=["rsi_14","ma20","ma50","bb_pct","cci_20"])
        histories[tk] = df
    # 拉 SPY 用于每日 sub-regime 判定
    spy = add_indicators(load_history("SPY", days=days + 60))
    common_dates = None
    for tk, df in histories.items():
        d_set = set(df.index)
        common_dates = d_set if common_dates is None else common_dates & d_set
    dates = sorted(common_dates)[-days:]

    def _daily_regime(date) -> str:
        """根据 SPY MA50 extension + ATR ratio 判 bull 子类。"""
        if date not in spy.index:
            return "bull_trending"
        spy_row = spy.loc[date]
        close = spy_row["close"]
        ma50 = spy_row["ma50"]
        if pd.isna(ma50) or ma50 <= 0:
            return "bull_trending"
        ext = (close - ma50) / ma50 * 100
        if ext > 10:
            return "bull_extended"
        if abs(ext) < 3:
            return "bull_pulling"
        return "bull_trending"

    account = SimAccount(cash=INITIAL_CASH)
    # 同步 buy-and-hold 基线（每 ticker 各放 1/3 资金）
    bh_alloc = INITIAL_CASH / len(tickers)
    bh_shares = {}
    for tk in tickers:
        start_price = float(histories[tk].loc[dates[0], "close"])
        bh_shares[tk] = bh_alloc / start_price

    for d in dates:
        prices_today = {tk: float(histories[tk].loc[d, "close"]) for tk in tickers}
        power_today = account.value(prices_today)
        regime_today = _daily_regime(d)   # Layer 1 子类
        for tk in tickers:
            row = histories[tk].loc[d]
            full = "US." + tk
            try:
                mkt = build_mkt(full, row)
                dec = get_decision(mkt, fake_events, fake_macro,
                                   board_regime=regime_today)
            except Exception:
                continue
            action = dec.get("action", "HOLD")
            conf = dec.get("confidence", 0) or 0
            if conf < conf_min:
                continue
            price = float(row["close"])
            pos = account.positions.get(full, {"qty":0,"cost":0})
            # ── 纪律性管理 (TP / SL / Pyramid) ──
            pos_data = account.positions.get(full)
            if pos_data and pos_data["qty"] > 0:
                meta = account.last_action_meta.get(full, {})
                entry_price = meta.get("entry_price", pos_data["cost"])
                entry_high  = max(meta.get("entry_high", entry_price), price)
                meta["entry_high"] = entry_high
                account.last_action_meta[full] = meta
                # Trailing stop -8%
                ts_pct = 0.08 * _lev_sqrt(full)
                if entry_high > 0 and (price - entry_high) / entry_high <= -ts_pct:
                    qty = pos_data["qty"]
                    account.sell(d.strftime("%Y-%m-%d"), full, qty, price,
                                 f"TRAIL-STOP from ${entry_high:.2f} ({(price-entry_high)/entry_high*100:+.1f}%) lev{_lev_sqrt(full):.2f}x")
                    account.last_action_meta[full] = {"action":"TRAIL_STOP","date":d,"price":price,"qty":qty,"rebuy_done":False}
                    continue
                # 阶梯止盈 (sqrt(leverage) 缩放)
                gain = (price - entry_price) / entry_price
                tp_hit = set(meta.get("tp_hit", []))
                original_qty = meta.get("entry_qty", pos_data["qty"])
                s_tp = _lev_sqrt(full)
                for thresh, frac, label in [(0.15*s_tp,0.30,"tp15"),(0.30*s_tp,0.30,"tp30"),(0.50*s_tp,0.40,"tp50")]:
                    if gain >= thresh and label not in tp_hit:
                        qty = max(1, min(int(original_qty * frac), pos_data["qty"]))
                        if qty > 0:
                            account.sell(d.strftime("%Y-%m-%d"), full, qty, price, f"TP-{label} (+{gain*100:.0f}%)")
                            tp_hit.add(label)
                            meta["tp_hit"] = list(tp_hit)
                            account.last_action_meta[full] = meta
                        break

            # auto re-BUY check v2: 平回到 vol-target 目标仓位
            last = account.last_action_meta.get(full)
            if last and last["action"] == "REDUCE" and action in BUY_ACTIONS:
                hours_since = (d - last["date"]).total_seconds() / 3600
                bounce = (price - last["price"]) / last["price"]
                if hours_since < 24*7 and bounce >= 0.03 * _lev_sqrt(full) and not last.get("rebuy_done"):
                    # 算 vol-target 目标仓位（与首次 BUY / Pyramid 共用尺度与 probe 规则）
                    frac = _position_fraction(full, regime_today, conf, power_today, action)
                    if frac > 0:
                        target_qty = int(power_today * frac // price)
                        current_qty = pos["qty"]
                        rebuy_qty = max(0, target_qty - current_qty)
                        if rebuy_qty > 0 and account.buy(d.strftime("%Y-%m-%d"), full, rebuy_qty, price, f"REBUY→target ({current_qty}→{target_qty}, bounce +{bounce*100:.1f}%)"):
                            last["rebuy_done"] = True
                            continue
            # Pyramid: 已持仓 + conf 高于入场 → 加 50% 原仓
            if action in BUY_ACTIONS and pos["qty"] > 0:
                meta = account.last_action_meta.get(full, {})
                entry_conf = meta.get("entry_conf", conf_min)
                layer = meta.get("layer", 1)
                if conf >= entry_conf + 1 and layer < 3:
                    frac = _position_fraction(full, regime_today, conf, power_today, action)
                    if frac > 0:
                        add_qty = int(power_today * frac * 0.5 // price)
                        if add_qty > 0 and account.buy(d.strftime("%Y-%m-%d"), full, add_qty, price, f"PYRAMID L{layer+1} (conf {entry_conf}→{conf})"):
                            meta["layer"] = layer + 1
                            meta["entry_conf"] = conf
                            account.last_action_meta[full] = meta
                continue
            if action in BUY_ACTIONS and pos["qty"] == 0:
                # vol-target sizing（与 re-BUY / Pyramid 共用同一实现）
                frac = _position_fraction(full, regime_today, conf, power_today, action)
                if frac > 0:
                    size = power_today * frac
                    qty = int(size // price)
                    if qty > 0:
                        if account.buy(d.strftime("%Y-%m-%d"), full, qty, price, f"{action} conf={conf} frac={frac:.0%}"):
                            # 记 entry 元数据 (给 TP/SL/Pyramid 用)
                            account.last_action_meta[full] = {
                                "action": "BUY", "date": d, "price": price,
                                "entry_price": price, "entry_high": price,
                                "entry_qty": qty, "entry_conf": conf,
                                "layer": 1, "tp_hit": [],
                            }
            elif action == "REDUCE" and pos["qty"] > 0:
                qty = max(1, pos["qty"] // 2)
                account.sell(d.strftime("%Y-%m-%d"), full, qty, price, f"REDUCE conf={conf}")
                # 记录 REDUCE 元信息供 re-BUY 用
                account.last_action_meta[full] = {
                    "action":"REDUCE","date":d,"price":price,"qty":qty,"rebuy_done":False
                }
            elif action == "SELL" and pos["qty"] > 0:
                account.sell(d.strftime("%Y-%m-%d"), full, pos["qty"], price, f"SELL conf={conf}")
        nav = account.value(prices_today)
        account.nav_curve.append((d.strftime("%Y-%m-%d"), nav))

    # buy-and-hold 终值
    bh_nav_end = sum(bh_shares[tk] * float(histories[tk].loc[dates[-1], "close"]) for tk in tickers)
    bh_return = (bh_nav_end - INITIAL_CASH) / INITIAL_CASH * 100

    nav_series = pd.Series([n for _, n in account.nav_curve], index=[d for d,_ in account.nav_curve])
    peak = nav_series.expanding().max()
    drawdown = (nav_series - peak) / peak * 100
    max_dd = drawdown.min()
    sys_return = (nav_series.iloc[-1] - INITIAL_CASH) / INITIAL_CASH * 100

    return {
        "days":       len(dates),
        "n_trades":   len(account.history),
        "final_nav":  nav_series.iloc[-1],
        "sys_return": sys_return,
        "bh_return":  bh_return,
        "alpha":      sys_return - bh_return,
        "max_dd":     float(max_dd),
        "history":    [t.__dict__ for t in account.history],
        "nav_curve":  list(account.nav_curve),
    }


def report_mid(result: dict, label: str = "Mid") -> list[str]:
    lines = ["", "+" + "="*74 + "+",
             f"|  {label} 回测：完整 trader 模拟".ljust(75) + "|",
             "+" + "="*74 + "+"]
    lines.append(f"  回测天数:   {result['days']}")
    lines.append(f"  总交易笔数: {result['n_trades']}")
    lines.append(f"  起始资金:   ${INITIAL_CASH:,.0f}")
    lines.append(f"  最终 NAV:   ${result['final_nav']:,.0f}")
    lines.append(f"  系统收益:   {result['sys_return']:+7.2f}%")
    lines.append(f"  B&H 基线:   {result['bh_return']:+7.2f}%")
    lines.append(f"  alpha:      {result['alpha']:+7.2f}%   {'✅ 跑赢' if result['alpha']>0 else '❌ 跑输'}")
    lines.append(f"  最大回撤:   {result['max_dd']:+7.2f}%")
    return lines


# ── 主流程 ────────────────────────────────────────────────────────────────
def main():
    out_lines = []
    out_lines.append(f"# 系统回测报告  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    out_lines.append("")
    out_lines.append(f"回测窗口: 过去 {BACKTEST_DAYS} 个交易日 (yfinance daily K)")
    out_lines.append(f"标的: {', '.join(TICKERS)}")
    out_lines.append(f"起始资金 (Mid): ${INITIAL_CASH:,}")
    out_lines.append("")

    print("[1/2] Lite 信号准确率...")
    lite_events = run_lite()
    for line in report_lite(lite_events):
        print(line); out_lines.append(line)

    print("\n[2/2] Mid 完整模拟...")
    mid_result = run_mid()
    for line in report_mid(mid_result):
        print(line); out_lines.append(line)

    # 写入 markdown
    out_path = Path(SIGNALS_DIR) / f"backtest_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"\n报告已写入: {out_path}")


if __name__ == "__main__":
    main()
