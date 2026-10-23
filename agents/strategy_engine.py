"""
StrategyEngine — 规则库 + 历史K线回测。
参考 trump-code/analysis_08_backtest.py 架构:
  候选规则 → moomoo历史K线 → train/test分割 → 胜率/平均收益 → 综合评分

run_strategy_backtest(ticker)  → 所有规则的回测结果（按综合分排序）
generate_backtest_report(...)  → 格式化报告字符串
"""

import json
import math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

from config import OPEND_HOST, OPEND_PORT, SIGNALS_DIR
from i18n import t
from research_validation import evaluate_rule_walk_forward

# ── 候选规则库 ─────────────────────────────────────────────────────────────────
# conditions: [(field, op, value), ...]  可用字段: rsi_14, vol_ratio, trend, ma_stack, is_new_52w_high
# action: REDUCE / WATCH_BUY / BUY / SELL
# hold:   持仓天数（日历日），到期检验方向对错

ETF_RULES = [
    {"id": "T1", "name": "RSI超买+缩量",
     "conditions": [("rsi_14", ">", 82), ("vol_ratio", "<", 0.75)],
     "action": "REDUCE", "hold": 3},

    {"id": "T2", "name": "RSI超买",
     "conditions": [("rsi_14", ">", 78)],
     "action": "REDUCE", "hold": 2},

    {"id": "T3", "name": "新高+量背离",
     "conditions": [("rsi_14", ">", 80), ("vol_ratio", "<", 0.72), ("is_new_52w_high", "=", True)],
     "action": "REDUCE", "hold": 5},

    {"id": "T4", "name": "超卖+上升趋势",
     "conditions": [("rsi_14", "<", 35), ("trend", "=", "up")],
     "action": "WATCH_BUY", "hold": 5},

    {"id": "T5", "name": "趋势+量配合",
     "conditions": [("rsi_14", ">", 55), ("rsi_14", "<", 82), ("trend", "=", "up"), ("vol_ratio", ">", 1.1)],
     "action": "WATCH_BUY", "hold": 7},

    {"id": "T6", "name": "极度超卖",
     "conditions": [("rsi_14", "<", 28)],
     "action": "WATCH_BUY", "hold": 3},

    {"id": "T7", "name": "下降趋势+超买",
     "conditions": [("trend", "=", "down"), ("ma_stack", "=", "bear"), ("rsi_14", ">", 70)],
     "action": "REDUCE", "hold": 5},

    {"id": "T8", "name": "下降趋势确认",
     "conditions": [("trend", "=", "down"), ("ma_stack", "=", "bear")],
     "action": "REDUCE", "hold": 5},

    {"id": "T9", "name": "强趋势+健康动能",
     "conditions": [("trend", "=", "up"), ("ma_stack", "=", "bull"),
                    ("rsi_14", ">", 60), ("rsi_14", "<", 80)],
     "action": "WATCH_BUY", "hold": 7},

    {"id": "T10", "name": "趋势+RSI极端",
     "conditions": [("trend", "=", "up"), ("rsi_14", ">", 85)],
     "action": "REDUCE", "hold": 3},
]

GOLD_RULES = [
    {"id": "G1", "name": "黄金超卖反弹",
     "conditions": [("rsi_14", "<", 32)],
     "action": "BUY", "hold": 5},

    {"id": "G2", "name": "黄金超买+量背离",
     "conditions": [("rsi_14", ">", 78), ("vol_ratio", "<", 0.75)],
     "action": "SELL", "hold": 3},

    {"id": "G3", "name": "黄金超卖+上升",
     "conditions": [("rsi_14", "<", 40), ("trend", "=", "up")],
     "action": "BUY", "hold": 7},

    {"id": "G4", "name": "黄金RSI极超买",
     "conditions": [("rsi_14", ">", 80)],
     "action": "SELL", "hold": 3},

    {"id": "G5", "name": "黄金动能延续",
     "conditions": [("rsi_14", ">", 50), ("rsi_14", "<", 78), ("trend", "=", "up")],
     "action": "BUY", "hold": 7},

    {"id": "G6", "name": "黄金超卖极值",
     "conditions": [("rsi_14", "<", 28)],
     "action": "BUY", "hold": 3},
]


# ── K线拉取 + 指标计算 ────────────────────────────────────────────────────────

def fetch_kline_history(ticker: str, days: int = 350) -> pd.DataFrame | None:
    """从 moomoo OpenD 拉取日K线（前复权），返回含指标的 DataFrame。"""
    try:
        from moomoo import KLType, AuType, RET_OK
        from moomoo_pool import get_quote_ctx
        ctx = get_quote_ctx()
        end   = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 120)).strftime("%Y-%m-%d")
        ret, df, _ = ctx.request_history_kline(
            ticker, start=start, end=end,
            ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000
        )
        if ret != 0 or df is None or df.empty:
            return None
        df = df.sort_values("time_key").reset_index(drop=True)
        df["time_key"] = pd.to_datetime(df["time_key"])
        return _compute_indicators(df)
    except Exception:
        return None


def _psar_trend(high: np.ndarray, low: np.ndarray,
                af_step: float = 0.02, af_max: float = 0.20) -> np.ndarray:
    """Parabolic SAR。返回 trend 数组：1=多头(SAR在价格下方)，-1=空头(SAR在价格上方)。"""
    n = len(high)
    trend = np.ones(n, dtype=int)
    sar = np.zeros(n)
    ep  = np.zeros(n)
    af  = np.full(n, af_step)

    trend[0] = 1 if (n > 1 and high[1] >= high[0]) else -1
    sar[0]   = low[0]  if trend[0] == 1 else high[0]
    ep[0]    = high[0] if trend[0] == 1 else low[0]

    for i in range(1, n):
        t = trend[i - 1]; s = sar[i - 1]; e = ep[i - 1]; a = af[i - 1]
        if t == 1:
            new_s = s + a * (e - s)
            new_s = min(new_s, low[i-1], low[i-2] if i >= 2 else low[i-1])
            if low[i] < new_s:
                trend[i] = -1; sar[i] = e; ep[i] = low[i]; af[i] = af_step
            else:
                trend[i] = 1; sar[i] = new_s
                if high[i] > e: ep[i] = high[i]; af[i] = min(a + af_step, af_max)
                else:           ep[i] = e;        af[i] = a
        else:
            new_s = s + a * (e - s)
            new_s = max(new_s, high[i-1], high[i-2] if i >= 2 else high[i-1])
            if high[i] > new_s:
                trend[i] = 1;  sar[i] = e; ep[i] = high[i]; af[i] = af_step
            else:
                trend[i] = -1; sar[i] = new_s
                if low[i] < e: ep[i] = low[i]; af[i] = min(a + af_step, af_max)
                else:          ep[i] = e;       af[i] = a
    return trend


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # RSI(14)
    delta = df["close"].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    df["rsi_14"] = (100 - 100 / (1 + gain.ewm(com=13, adjust=False).mean()
                                / loss.ewm(com=13, adjust=False).mean().replace(0, float("nan")))).round(1)

    # CCI(20): (典型价 - SMA) / (0.015 × 平均偏差)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(20).mean()
    mad    = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    df["cci_20"] = ((tp - sma_tp) / (0.015 * mad.replace(0, float("nan")))).round(1)

    # 量比
    df["vol_ratio"] = (df["volume"] / df["volume"].rolling(20).mean()).round(2)

    # 均线
    df["ma_5"]  = df["close"].rolling(5).mean()
    df["ma_20"] = df["close"].rolling(20).mean()
    df["ma_60"] = df["close"].rolling(60).mean()

    # 趋势 + MA叠加
    df["trend"]    = (df["close"] > df["ma_20"]).map({True: "up", False: "down"})
    df["ma_stack"] = ((df["ma_5"] > df["ma_20"]) & (df["ma_20"] > df["ma_60"])).map({True: "bull", False: "bear"})

    # 52周新高 (warm-up 期数据不足 → False, 不再 fillna(close) 伪造新高)
    df["is_new_52w_high"] = df["close"] >= df["close"].rolling(252).max() * 0.999

    # ── 语义区间列（基于标准技术分析分界值，非任意阈值） ──────────────────────
    # RSI 区间: 经典 35/70 分界（ETF 略高于标准 30/70）
    df["rsi_zone"] = pd.cut(
        df["rsi_14"],
        bins=[-np.inf, 35, 70, np.inf],
        labels=["oversold", "neutral", "overbought"]
    ).astype(str)

    # 量比区间: 以 1.0 为中心对称
    df["vol_zone"] = pd.cut(
        df["vol_ratio"],
        bins=[-np.inf, 0.8, 1.25, np.inf],
        labels=["shrink", "normal", "expand"]
    ).astype(str)

    # CCI 区间: 行业标准 ±100 分界
    df["cci_zone"] = pd.cut(
        df["cci_20"],
        bins=[-np.inf, -100, 100, np.inf],
        labels=["oversold", "neutral", "overbought"]
    ).astype(str)

    # ── 穿越信号（crossover signals）─────────────────────────────────────
    _ma5_p  = df["ma_5"].shift(1)
    _ma20_p = df["ma_20"].shift(1)
    df["ma_cross"] = "none"
    df.loc[(_ma5_p < _ma20_p) & (df["ma_5"] >= df["ma_20"]), "ma_cross"] = "golden"
    df.loc[(_ma5_p > _ma20_p) & (df["ma_5"] <= df["ma_20"]), "ma_cross"] = "death"

    _rsi_p = df["rsi_14"].shift(1)
    df["rsi_cross"] = "none"
    df.loc[(_rsi_p < 70) & (df["rsi_14"] >= 70), "rsi_cross"] = "up_70"
    df.loc[(_rsi_p > 30) & (df["rsi_14"] <= 30), "rsi_cross"] = "dn_30"

    _cci_p = df["cci_20"].shift(1)
    df["cci_cross"] = "none"
    df.loc[(_cci_p < 100)  & (df["cci_20"] >= 100),  "cci_cross"] = "up_100"
    df.loc[(_cci_p > -100) & (df["cci_20"] <= -100), "cci_cross"] = "dn_100"

    # ── Bollinger Bands (20, 2σ) ──────────────────────────────────────────────
    # 上轨 = MA20 + 2σ，下轨 = MA20 - 2σ；%B = (价格-下轨)/(上轨-下轨)
    # %B > 1.0 = 突破上轨（拉伸），%B < 0.0 = 跌破下轨（超卖）
    df["bb_std"]   = df["close"].rolling(20).std(ddof=1)
    df["bb_upper"] = df["ma_20"] + 2 * df["bb_std"]
    df["bb_lower"] = df["ma_20"] - 2 * df["bb_std"]
    bb_width       = (df["bb_upper"] - df["bb_lower"]).replace(0, float("nan"))
    df["bb_pct"]   = ((df["close"] - df["bb_lower"]) / bb_width).round(3)
    df["bb_zone"]  = "normal"
    df.loc[df["bb_pct"] > 1.0, "bb_zone"] = "above"
    df.loc[df["bb_pct"] < 0.0, "bb_zone"] = "below"

    # ── Parabolic SAR (AF step=0.02, max=0.20) ────────────────────────────────
    # SAR在价格下方=多头趋势；SAR从上方翻到下方=bear_flip（顶部反转信号）
    if len(df) >= 5:
        psar_arr        = _psar_trend(df["high"].values, df["low"].values)
        df["psar_trend"] = psar_arr
        _psar_prev      = pd.Series(psar_arr).shift(1).values
        df["psar_signal"] = "none"
        df.loc[(_psar_prev == -1) & (df["psar_trend"] == 1),  "psar_signal"] = "bull_flip"
        df.loc[(_psar_prev ==  1) & (df["psar_trend"] == -1), "psar_signal"] = "bear_flip"
    else:
        df["psar_trend"]  = 1
        df["psar_signal"] = "none"

    # ── MACD (12,26,9) — 动量+趋势复合 ───────────────────────────────────────
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd_dif"]  = ema12 - ema26
    df["macd_dea"]  = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd_dif"] - df["macd_dea"]
    _dif_p = df["macd_dif"].shift(1)
    _dea_p = df["macd_dea"].shift(1)
    df["macd_signal"] = "none"
    df.loc[(_dif_p < _dea_p) & (df["macd_dif"] >= df["macd_dea"]), "macd_signal"] = "golden"
    df.loc[(_dif_p > _dea_p) & (df["macd_dif"] <= df["macd_dea"]), "macd_signal"] = "death"
    df["macd_zone"] = "neutral"
    df.loc[(df["macd_dif"] > 0) & (df["macd_dea"] > 0), "macd_zone"] = "bull"
    df.loc[(df["macd_dif"] < 0) & (df["macd_dea"] < 0), "macd_zone"] = "bear"

    # ── ADX(14) — 趋势强度 (不分方向) ────────────────────────────────────────
    high, low, cls = df["high"], df["low"], df["close"]
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([(high - low),
                    (high - cls.shift()).abs(),
                    (low  - cls.shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).rolling(14).mean() / atr14
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(14).mean() / atr14
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx_14"] = dx.rolling(14).mean()
    df["adx_zone"] = "weak"
    df.loc[df["adx_14"] >= 20, "adx_zone"] = "moderate"
    df.loc[df["adx_14"] >= 25, "adx_zone"] = "strong"
    df.loc[df["adx_14"] >= 40, "adx_zone"] = "extreme"

    return df.dropna(subset=["rsi_14", "cci_20"]).reset_index(drop=True)


# ── 规则评估 ─────────────────────────────────────────────────────────────────

def _check_condition(val, op: str, threshold) -> bool:
    try:
        if op == "=":  return val == threshold
        v = float(val)
        t = float(threshold)
        if op == ">":  return v > t
        if op == "<":  return v < t
        if op == ">=": return v >= t
        if op == "<=": return v <= t
    except (TypeError, ValueError):
        pass
    return False


def check_rule(row: pd.Series, rule: dict) -> bool:
    return all(_check_condition(row.get(f), op, th) for f, op, th in rule["conditions"])


def backtest_rule(rule: dict, df: pd.DataFrame, split: float = 0.8) -> dict:
    """Train/test 分割回测单条规则，返回结果 dict。"""
    is_bull = rule["action"] in ("WATCH_BUY", "BUY")
    hold    = rule["hold"]
    split_i = int(len(df) * split)
    train_trades, test_trades = [], []

    for i in range(len(df) - hold - 1):
        if not check_rule(df.iloc[i], rule):
            continue
        entry = float(df.iloc[i]["close"])
        exit_ = float(df.iloc[i + hold]["close"])
        ret   = (exit_ - entry) / entry * 100
        trade = {
            "date":  str(df.iloc[i]["time_key"].date()),
            "ret":   round(ret, 2),
            "win":   (ret > 0) if is_bull else (ret < 0),
        }
        (train_trades if i < split_i else test_trades).append(trade)

    def stats(trades):
        if not trades:
            return {"n": 0, "win_rate": 0.0, "avg_ret": 0.0, "sharpe": 0.0}
        rets = [t["ret"] for t in trades]
        wins = sum(1 for t in trades if t["win"])
        mean = sum(rets) / len(rets)
        std  = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) if len(rets) > 1 else 1
        return {
            "n":        len(trades),
            "win_rate": round(wins / len(trades) * 100, 1),
            "avg_ret":  round(mean, 2),
            "sharpe":   round(mean / std * math.sqrt(252 / hold) if std > 0 else 0, 2),
        }

    tr = stats(train_trades)
    te = stats(test_trades)
    # 综合分: 测试胜率权重60% + 训练胜率40%，测试样本不足时降权
    test_weight = min(te["n"] / 5, 1.0)
    combined = round(tr["win_rate"] * 0.4 + te["win_rate"] * 0.6 * test_weight
                     + te["win_rate"] * 0.0 * (1 - test_weight), 1)

    return {
        "id":           rule["id"],
        "name":         rule["name"],
        "conditions":   rule["conditions"],
        "action":       rule["action"],
        "hold":         hold,
        "train_n":      tr["n"],
        "train_wr":     tr["win_rate"],
        "train_ret":    tr["avg_ret"],
        "test_n":       te["n"],
        "test_wr":      te["win_rate"],
        "test_ret":     te["avg_ret"],
        "test_sharpe":  te["sharpe"],
        "combined":     combined,
        "recent":       test_trades[-3:],  # 最近3笔
    }


# ── 全规则回测入口 ────────────────────────────────────────────────────────────

def run_strategy_backtest(ticker: str, is_gold: bool = False) -> list[dict]:
    """
    对 ticker 运行固定、可解释的候选规则，返回按 combined 排序的结果列表。
    """
    df = fetch_kline_history(ticker)
    if df is None or len(df) < 80:
        return []

    # 手工规则回测
    rules = GOLD_RULES if is_gold else ETF_RULES
    results = []
    for rule in rules:
        result = backtest_rule(rule, df)
        result["walk_forward"] = evaluate_rule_walk_forward(rule, df, check_rule)
        # 固定规则不需要用训练集挑参数；排名优先依据真正的滚动样本外结果。
        wf = result["walk_forward"]
        result["oos_gate"] = "pass" if wf["passed"] else "insufficient_or_unstable"
        result["institutional_score"] = round(
            wf["oos_win_rate"] * min(1.0, wf["oos_n"] / 10.0) *
            (0.5 + 0.5 * wf["fold_stability"]),
            1,
        )
        results.append(result)
    results = [r for r in results if r["train_n"] >= 3]

    results.sort(
        key=lambda x: (
            bool((x.get("walk_forward") or {}).get("passed")),
            x.get("institutional_score", 0),
            x["combined"],
        ),
        reverse=True,
    )
    path = Path(SIGNALS_DIR) / f"backtest_{ticker.replace('.','-')}.json"
    path.write_text(json.dumps({"ts": datetime.now().isoformat(), "ticker": ticker,
                                "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


# ── 技术形态胜率排行榜 ────────────────────────────────────────────────────────

def generate_pattern_leaderboard(etf_tickers: list, gold_ticker: str = "US.GLD") -> str:
    """
    日K技术形态胜率排行。
    胜率 = 触发后持有 N 天，收益方向正确的次数 / 触发总次数。
    同一固定技术形态取最优持仓周期的胜率。
    """
    W   = 92
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = t("技术形态胜率排行", "テクニカルパターン勝率ランキング")
    lines = [
        "+" + "=" * (W - 2) + "+",
        f"|  {title}  |  {now}".ljust(W - 1) + "|",
        "+" + "=" * (W - 2) + "+",
        "",
    ]

    def _grade(wr: float) -> str:
        if wr >= 65: return "★★★"
        if wr >= 58: return "★★"
        if wr >= 52: return "★"
        return ""

    # 表头标签
    hdr_pattern = t("形态",       "パターン")
    hdr_trig    = t("触发",       "発火")
    hdr_test_n  = t("测试N",      "テストN")
    hdr_hold    = t("持仓",       "保有")
    hdr_wr      = t("胜率(测试)", "勝率(テスト)")
    hdr_ret     = t("平均收益",   "平均収益")
    hdr_grade   = t("评级",       "評価")
    hold_unit   = t("天",         "日")
    label_buy   = t("买入信号",             "買いシグナル")
    label_sell  = t("减仓/卖出信号",        "利確/売りシグナル")
    rules_unit  = t("条规则",     "件のルール")
    trig_unit   = t("历史触发",   "過去発火")
    trig_times  = t("次",         "回")

    def _section(ticker: str, results: list) -> None:
        name = ticker.split(".")[-1]
        if not results:
            lines.append(t(f"  {name}: 数据不足", f"  {name}: データ不足"))
            lines.append("")
            return

        best: dict[str, dict] = {}
        for r in results:
            key = r["name"]
            if key not in best or r["test_wr"] > best[key]["test_wr"]:
                best[key] = r
        deduped = sorted(best.values(), key=lambda x: x["test_wr"], reverse=True)

        buy  = [r for r in deduped if r["action"] in ("WATCH_BUY", "BUY")  and r["test_n"] >= 2]
        sell = [r for r in deduped if r["action"] in ("REDUCE",   "SELL")  and r["test_n"] >= 2]
        total_n = sum(r["train_n"] + r["test_n"] for r in deduped)

        lines.append(f"  【{name}】  {len(deduped)} {rules_unit} · {trig_unit} {total_n} {trig_times}")
        lines.append("-" * W)

        for label, subset in [(label_buy, buy), (label_sell, sell)]:
            if not subset:
                continue
            lines.append(f"  {label} (Top {min(8, len(subset))})")
            lines.append(
                f"  {hdr_pattern:<42} {hdr_trig:>5}  {hdr_test_n:>5}  {hdr_hold:>4}  "
                f"{hdr_wr:>10}  {hdr_ret:>10}   {hdr_grade}"
            )
            lines.append(
                f"  {'-'*42} {'-'*5}  {'-'*5}  {'-'*4}  {'-'*10}  {'-'*10}   ----"
            )
            for r in subset[:8]:
                total = r["train_n"] + r["test_n"]
                tn    = r["test_n"]
                hold  = r.get("hold", "?")
                wr_str = f"{r['test_wr']:5.1f}%{'*' if tn < 5 else ' '}"
                ret_str = f"{r.get('test_ret', 0):+6.2f}%"
                lines.append(
                    f"  {r['name'][:42]:<42} {total:>5}  {tn:>5}  {hold:>3}{hold_unit}  "
                    f"{wr_str:>10}  {ret_str:>10}   {_grade(r['test_wr'])}"
                )
            lines.append(t(
                "  (* 测试样本 N<5，胜率仅供参考；持仓=测试时的天数；"
                "平均收益=测试集上每次触发的平均收益率)",
                "  (* テストサンプル N<5、勝率は参考値；保有=テスト時の日数；"
                "平均収益=テスト集合の発火ごと平均収益率)",
            ))
            lines.append("")

    for ticker in etf_tickers:
        _section(ticker, run_strategy_backtest(ticker))

    _section(gold_ticker, run_strategy_backtest(gold_ticker, is_gold=True))

    lines += [
        "=" * W,
        t("  ★★★ 测试胜率≥65%  ★★ ≥58%  ★ ≥52%",
          "  ★★★ テスト勝率≥65%  ★★ ≥58%  ★ ≥52%"),
        t("  数据来源: moomoo OpenD 日K线（前复权）· 80/20 拆分回测",
          "  データソース: moomoo OpenD 日足(前方除権)・80/20 分割バックテスト"),
        "=" * W,
    ]
    return "\n".join(lines)


# ── 今日信号实况（实操视图）─────────────────────────────────────────────────────

def _eval_today(market: dict, rule: dict) -> bool:
    """对今日的市场状态评估一条规则是否触发。"""
    for f, op, th in rule.get("conditions", []):
        if not _check_condition(market.get(f), op, th):
            return False
    return rule.get("conditions") is not None and len(rule["conditions"]) > 0


def generate_today_signals(market_signals: dict) -> str:
    """
    今日信号实况：当前指标快照 + 哪些历史高胜率规则在今天触发。

    market_signals: {ticker: market_dict, ...}  来自 market_watch/futures_watch
    """
    W = 76
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = t("今日信号实况", "本日シグナル実況")
    lines = [
        "+" + "=" * (W - 2) + "+",
        f"|  {title}  |  {now}".ljust(W - 1) + "|",
        "+" + "=" * (W - 2) + "+",
        "",
    ]

    def _zone_tag(zone: str, kind: str) -> str:
        tags = {
            "rsi": {"oversold": t("超卖","売られ過ぎ"), "neutral": t("中性","中立"),  "overbought": t("超买","買われ過ぎ")},
            "cci": {"oversold": t("超卖","売られ過ぎ"), "neutral": t("中性","中立"),  "overbought": t("超买","買われ過ぎ")},
            "vol": {"shrink":   t("缩量","出来高減"),    "normal":  t("正常","通常"),  "expand":     t("放量","出来高増")},
            "bb":  {"above":    t("破上轨","上限突破"),  "normal":  t("带内","レンジ内"),"below":     t("破下轨","下限割込み")},
        }
        return tags.get(kind, {}).get(zone, zone)

    for ticker_key, mkt in market_signals.items():
        if not mkt or "error" in mkt:
            continue
        name = mkt.get("ticker", ticker_key)

        # ── 当前指标快照 ────────────────────────────────────────────────────
        price    = mkt.get("price", 0)
        pct      = mkt.get("pct_chg", 0)
        pct_zone = mkt.get("pct_chg_zone", "normal")
        dist     = mkt.get("dist_from_high", 0)
        rsi      = mkt.get("rsi_14")  or 0
        cci      = mkt.get("cci_20") or 0
        bb_pct   = mkt.get("bb_pct")
        bb_up    = mkt.get("bb_upper") or 0
        bb_lo    = mkt.get("bb_lower") or 0
        vr       = mkt.get("vol_ratio") or 1.0
        ma20     = mkt.get("ma20") or 0
        ma50     = mkt.get("ma50") or 0
        trend    = mkt.get("trend", "?")
        stack    = mkt.get("ma_stack", "?")
        psar_tr  = mkt.get("psar_trend", 1)
        psar_sig = mkt.get("psar_signal", "none")

        # 当日极端波动警示（显著标注）
        pct_warn = ""
        if   pct_zone == "crash":     pct_warn = t("  ⚠⚠ 暴跌(-5%以上)", "  ⚠⚠ 急落(-5%以上)")
        elif pct_zone == "drop":      pct_warn = t("  ⚠ 下跌(-2%以上)", "  ⚠ 下落(-2%以上)")
        elif pct_zone == "mild_drop": pct_warn = t("  · 小跌(-1%以上)",  "  · 小幅下落(-1%以上)")
        elif pct_zone == "surge":     pct_warn = t("  ⚠⚠ 暴涨(+5%以上)", "  ⚠⚠ 急騰(+5%以上)")
        elif pct_zone == "pop":       pct_warn = t("  ⚠ 上涨(+2%以上)",  "  ⚠ 上昇(+2%以上)")
        elif pct_zone == "mild_pop":  pct_warn = t("  · 小涨(+1%以上)",  "  · 小幅上昇(+1%以上)")

        dist_label = t("距52周高点", "52週高値からの距離")
        lines.append(f"  【{name}】 ${price:.2f}  ({pct:+.2f}%){pct_warn}  {dist_label} {dist:+.2f}%")

        # 完整时段链路（盘前 → 盘中 → 盘后 → 夜盘 → 今日盘前 → 今日盘中）
        now_et = mkt.get("now_et", "")
        if now_et:
            lines.append(t(
                f"  当前时间: {now_et}  (各时段以 ET 标注)",
                f"  現在時刻: {now_et}  (各セッションは ET 基準)",
            ))

        # 1. 上一交易日的盘前/盘中/盘后/夜盘——按时间顺序排
        pre_pct, pre_pr = mkt.get("pre_pct"),    mkt.get("pre_price")
        af_pct,  af_pr  = mkt.get("after_pct"),  mkt.get("after_price")
        on_pct,  on_pr  = mkt.get("overnight_pct"), mkt.get("overnight_price")
        prev_sess       = mkt.get("prev_session")
        today_sess      = mkt.get("today_session")

        # 盘前
        if pre_pct is not None and pre_pr is not None:
            lbl = mkt.get("pre_label", t("盘前", "プレマーケット"))
            d   = mkt.get("pre_date", "")
            lines.append(f"    [{d}] {lbl}:   ${pre_pr:>8.2f}   {pre_pct:+6.2f}%")

        # 盘中（关键——之前漏掉了）
        if prev_sess:
            lines.append(t(
                f"    [{prev_sess['date']}] 盘中(已收): "
                f"开${prev_sess['open']:.2f} → 收${prev_sess['close']:.2f}  "
                f"({prev_sess['pct']:+.2f}%, 高${prev_sess['high']:.2f}/低${prev_sess['low']:.2f})",
                f"    [{prev_sess['date']}] レギュラーセッション(終了): "
                f"始値${prev_sess['open']:.2f} → 終値${prev_sess['close']:.2f}  "
                f"({prev_sess['pct']:+.2f}%, 高値${prev_sess['high']:.2f}/安値${prev_sess['low']:.2f})",
            ))

        # 盘后
        if af_pct is not None and af_pr is not None:
            lbl = mkt.get("after_label", t("盘后", "アフターマーケット"))
            d   = mkt.get("after_date", "")
            lines.append(f"    [{d}] {lbl}:   ${af_pr:>8.2f}   {af_pct:+6.2f}%")

        # 夜盘
        if on_pct is not None and on_pr is not None:
            lbl = mkt.get("overnight_label", t("夜盘", "ナイトセッション"))
            d   = mkt.get("overnight_date", "")
            lines.append(f"    [{d}] {lbl}:   ${on_pr:>8.2f}   {on_pct:+6.2f}%")

        # 今日盘中（如已开盘）
        if today_sess:
            status_raw = today_sess.get("status", "进行中")
            status_disp = t(status_raw, "進行中" if "进行" in status_raw else "終了")
            lines.append(t(
                f"    [{today_sess['date']}] 今日盘中({status_disp}): "
                f"开${today_sess['open']:.2f} → 现${today_sess['current']:.2f}  "
                f"({today_sess['pct']:+.2f}%)",
                f"    [{today_sess['date']}] 本日レギュラーセッション({status_disp}): "
                f"始値${today_sess['open']:.2f} → 現在${today_sess['current']:.2f}  "
                f"({today_sess['pct']:+.2f}%)",
            ))
        else:
            note = mkt.get("today_not_yet_opened")
            if note:
                lines.append(t(f"    [今日盘中] {note}",
                               f"    [本日レギュラー] {note}"))

        lines.append(f"  {'-' * (W - 2)}")
        lines.append(t("  指标快照:", "  指標スナップショット:"))
        lines.append(
            f"    RSI(14)={rsi:5.1f} [{_zone_tag(mkt.get('rsi_zone','neutral'),'rsi')}]    "
            f"CCI(20)={cci:5.0f} [{_zone_tag(mkt.get('cci_zone','neutral'),'cci')}]"
        )
        bb_zone_tag = _zone_tag(mkt.get("bb_zone", "normal"), "bb")
        bb_pct_str  = f"%B={bb_pct:.2f}" if bb_pct is not None else "%B=N/A"
        lines.append(t(
            f"    BB(20,2σ): {bb_pct_str} [{bb_zone_tag}]  上轨${bb_up:.2f}/下轨${bb_lo:.2f}",
            f"    BB(20,2σ): {bb_pct_str} [{bb_zone_tag}]  上限${bb_up:.2f}/下限${bb_lo:.2f}",
        ))
        if   psar_tr == 1  and psar_sig == "none": psar_text = t("多头持续(SAR在价下方)", "強気継続(SARが価格下方)")
        elif psar_tr == -1 and psar_sig == "none": psar_text = t("空头持续(SAR在价上方)", "弱気継続(SARが価格上方)")
        elif psar_sig == "bull_flip":              psar_text = t("刚转多 ↑", "強気転換 ↑")
        elif psar_sig == "bear_flip":              psar_text = t("刚转空 ↓", "弱気転換 ↓")
        else:                                       psar_text = "?"
        vol_label_zh = t("量比", "出来高比")
        ma_arrange   = (t("多排", "強気配列") if stack == 'bull' else t("空排", "弱気配列"))
        lines.append(
            f"    PSAR: {psar_text}    "
            f"{vol_label_zh}={vr:.2f}[{_zone_tag(mkt.get('vol_zone','normal'),'vol')}]    "
            f"MA: {ma_arrange}(MA20={ma20:.2f}/MA50={ma50:.2f})"
        )

        # ── 今日触发的历史规则 ────────────────────────────────────────────
        # 从 backtest JSON 拉规则，匹配 ticker 命名（市场字段无 . 前缀）
        candidates = []
        for ext_ticker in [f"US.{name}", name]:
            p = Path(SIGNALS_DIR) / f"backtest_{ext_ticker.replace('.','-')}.json"
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    candidates = [
                        r for r in data.get("results", [])
                        if not str(r.get("name", "")).startswith("[进化]")
                        and not str(r.get("id", "")).startswith("E")
                    ]
                    break
                except Exception:
                    pass

        triggered_buy, triggered_sell = [], []
        for r in candidates:
            if r.get("test_n", 0) < 3 or r.get("test_wr", 0) < 55:
                continue
            walk_forward = r.get("walk_forward") or {}
            if walk_forward and not walk_forward.get("passed"):
                continue
            if not _eval_today(mkt, r):
                continue
            tup = (r["name"], r["test_wr"], r["test_n"], r["action"],
                   r.get("hold", "?"), r.get("test_ret", 0))
            if r["action"] in ("WATCH_BUY", "BUY"):
                triggered_buy.append(tup)
            elif r["action"] in ("REDUCE", "SELL"):
                triggered_sell.append(tup)

        # ── 暴跌/暴涨当日：屏蔽方向相反的历史规则 ─────────────────────────
        suppressed_note = ""
        if pct_zone in ("crash", "drop"):
            n_buy = len(triggered_buy)
            triggered_buy = []
            if n_buy:
                suppressed_note = t(
                    f"  (当日{pct:.1f}%下跌，已屏蔽 {n_buy} 条买入信号——历史规则未见此情境)",
                    f"  (当日{pct:.1f}%下落により、買いシグナル {n_buy} 件を抑制——過去ルールが当該局面を含まないため)",
                )
        elif pct_zone in ("surge", "pop"):
            n_sell = len(triggered_sell)
            triggered_sell = []
            if n_sell:
                suppressed_note = t(
                    f"  (当日{pct:+.1f}%上涨，已屏蔽 {n_sell} 条减仓信号——历史规则未见此情境)",
                    f"  (当日{pct:+.1f}%上昇により、利確シグナル {n_sell} 件を抑制——過去ルールが当該局面を含まないため)",
                )

        # 去重：同名规则取胜率最高的
        def _dedupe(lst):
            best = {}
            for tup in lst:
                n, wr = tup[0], tup[1]
                if n not in best or wr > best[n][1]:
                    best[n] = tup
            return sorted(best.values(), key=lambda x: -x[1])[:5]

        triggered_buy  = _dedupe(triggered_buy)
        triggered_sell = _dedupe(triggered_sell)

        lines.append("")
        lines.append(t(
            "  今日触发规则 (测试胜率≥55% + 样本N≥3):",
            "  本日発火ルール (テスト勝率≥55% + サンプルN≥3):",
        ))
        hold_unit  = t("天", "日")
        wr_label   = t("胜率", "勝率")
        ret_label  = t("均收益", "平均収益")
        if triggered_buy:
            lines.append(t("    [买入信号]", "    [買いシグナル]"))
            for n, wr, tn, _, hd, ret in triggered_buy:
                lines.append(
                    f"      ▲ {n[:42]:<42} {t('持','保有')}{hd}{hold_unit} {wr_label} {wr:5.1f}%"
                    f" {ret_label} {ret:+6.2f}% (N={tn})"
                )
        if triggered_sell:
            lines.append(t("    [减仓信号]", "    [利確シグナル]"))
            for n, wr, tn, _, hd, ret in triggered_sell:
                lines.append(
                    f"      ▼ {n[:42]:<42} {t('持','保有')}{hd}{hold_unit} {wr_label} {wr:5.1f}%"
                    f" {ret_label} {ret:+6.2f}% (N={tn})"
                )
        if not triggered_buy and not triggered_sell:
            lines.append(t(
                "    (无高胜率规则触发，参考共振和决策信号)",
                "    (高勝率ルールの発火なし、共振と決定シグナルを参考に)",
            ))
        if suppressed_note:
            lines.append(suppressed_note)

        # ── 净信号 ─────────────────────────────────────────────────────────
        bn, sn = len(triggered_buy), len(triggered_sell)
        if bn or sn:
            if   sn >= bn + 2:  judgement = t("偏空：建议减仓或观望", "弱気優勢：利確または様子見")
            elif sn > bn:       judgement = t("略偏空：关注减仓时机", "やや弱気：利確タイミング注視")
            elif bn > sn + 1:   judgement = t("偏多：可关注买入",     "強気優勢：押し目買い検討")
            elif bn > 0:        judgement = t("略偏多：可小仓试探",   "やや強気：軽めポジションで試し買い")
            else:               judgement = t("中性",                 "中立")
            net_label = t(f"  净信号: {bn}多 vs {sn}空  →  {judgement}",
                          f"  ネットシグナル: 強気{bn} vs 弱気{sn}  →  {judgement}")
            lines.append(net_label)
        lines.append("")

    lines += [
        "=" * W,
        t("  说明: 仅使用固定、可解释的技术规则；触发≠保证。结合共振+宏观综合判断。",
          "  注: 固定された説明可能なテクニカルルールのみ使用。発火≠保証。共振+マクロと併せて判断。"),
        "=" * W,
    ]
    return "\n".join(lines)
