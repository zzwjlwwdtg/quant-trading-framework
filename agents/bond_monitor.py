"""bond_monitor.py — 美债收益率 + TIPS 实际利率 + GLD 联动监控.

**思路**：黄金的直接对手是 **实际利率** (TIPS, DFII10)，不是名义收益率。
- 实际利率 UP → GLD DOWN (历史相关性 -0.7 到 -0.9)
- 20d rolling correlation 反过来（>0）时 = "hedge 关系破裂 · 通胀 regime shift 警报"

数据源（全免费）：
- yfinance: ^TNX (10Y ×10) / ^FVX (5Y ×10) / ^IRX (13W ×100) / ^TYX (30Y ×10)
- FRED: DGS2 (2Y) / DFII10 (10Y TIPS 实际) / T10Y2Y (2s10s spread)
- yfinance: GLD (作为 correlation base)

统一返回结构：
{
  "asof": "2026-08-06T...",
  "yields": {
    "10y":  {"value": 4.25, "chg_1d_bps": +5, "chg_5d_bps": -12, "chg_20d_bps": +18, "z_score_20d": 1.4, "history": [...]},
    "2y":   {...},
    "5y":   {...},
    "30y":  {...},
    "tips_10y": {...}      # 关键
  },
  "spreads": {
    "2s10s":   {"value": 0.45, "regime": "flat" | "normal" | "steep" | "inverted"},
  },
  "gld_correlation": {
    "vs_10y":       -0.62,   # 20d rolling correlation
    "vs_tips_10y":  -0.78,   # 关键：GLD vs TIPS 实际利率
    "regime":       "normal_hedge" | "broken" | "aligned",
    "reason":       "TIPS 实际利率 vs GLD 20d correlation = -0.78, 正常对冲关系"
  },
  "anomalies": [
    {"metric": "10y", "reason": "10Y 单日 +12 bps (2.3σ)", "severity": "high"},
    ...
  ]
}
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional


# Yahoo Finance yield index Close is already a percentage value.
_YF_YIELDS = {
    "10y":  {"symbol": "^TNX", "scale": 1.0,  "name": "10Y 国债名义"},
    "5y":   {"symbol": "^FVX", "scale": 1.0,  "name": "5Y 国债"},
    "30y":  {"symbol": "^TYX", "scale": 1.0,  "name": "30Y 国债"},
    "3m":   {"symbol": "^IRX", "scale": 1.0,  "name": "3M 短端"},
}


def _is_finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _json_safe(value):
    """Recursively replace NaN/Inf so browsers always receive valid JSON."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _fetch_yield_history(symbol: str, period_days: int = 60):
    """从 yfinance 拉某 yield symbol 的历史 close 序列。"""
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=f"{period_days}d", auto_adjust=False)
        if df is None or df.empty:
            return None
        return df
    except Exception:
        return None


def _fetch_fred_series(series_id: str, days: int = 60) -> Optional[list]:
    """FRED 拉某个 series，返 list of (date, value) tuples。"""
    try:
        import urllib.request, json as _json
        from config import FRED_API_KEY
        if not FRED_API_KEY:
            return None
        url = (f"https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={series_id}&api_key={FRED_API_KEY}"
               f"&sort_order=desc&limit={days + 5}&file_type=json")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.load(r)
        out = []
        for o in data.get("observations", []):
            v = o.get("value")
            if v and v != ".":
                try:
                    out.append((o["date"], float(v)))
                except ValueError:
                    continue
        return out
    except Exception:
        return None


def _compute_yield_metrics(values: list[float], asof: str = "") -> dict:
    """给 values (最新在 [0])，算今日/5d/20d 变化 (bps) + z-score."""
    values = [float(value) for value in values if _is_finite_number(value)]
    if not values or len(values) < 2:
        return {"value": values[0] if values else None, "error": "insufficient_data"}
    v0 = values[0]
    v1 = values[1] if len(values) > 1 else v0
    v5 = values[5] if len(values) > 5 else v0
    v20 = values[20] if len(values) > 20 else v0
    hist_20 = values[:20]
    # 日变化的 z-score
    daily_chgs = [values[i] - values[i+1] for i in range(min(19, len(values)-1))]
    import statistics
    try:
        std = statistics.stdev(daily_chgs) if len(daily_chgs) >= 2 else 0
        today_chg = v0 - v1
        z_score = (today_chg / std) if std > 0 else 0
    except Exception:
        z_score = 0
    return {
        "value":        round(v0, 3),
        "chg_1d_bps":   round((v0 - v1) * 100, 1),
        "chg_5d_bps":   round((v0 - v5) * 100, 1),
        "chg_20d_bps":  round((v0 - v20) * 100, 1),
        "z_score_20d":  round(z_score, 2),
        "history":      [round(v, 3) for v in hist_20],
        "asof":         asof,
    }


def _rolling_corr(a: list[float], b: list[float], window: int = 20) -> Optional[float]:
    """简易 Pearson correlation. a[0] 是最新，b 同."""
    if not a or not b:
        return None
    pairs = []
    for left, right in zip(a, b):
        if not (_is_finite_number(left) and _is_finite_number(right)):
            continue
        pairs.append((float(left), float(right)))
        if len(pairs) >= window:
            break
    n = len(pairs)
    if n < 5:
        return None
    ax = [left for left, _ in pairs]
    bx = [right for _, right in pairs]
    mean_a = sum(ax) / n
    mean_b = sum(bx) / n
    num = sum((ax[i] - mean_a) * (bx[i] - mean_b) for i in range(n))
    den_a = sum((ax[i] - mean_a) ** 2 for i in range(n)) ** 0.5
    den_b = sum((bx[i] - mean_b) ** 2 for i in range(n)) ** 0.5
    if den_a == 0 or den_b == 0:
        return None
    result = num / (den_a * den_b)
    return round(result, 3) if math.isfinite(result) else None


def _spread_regime(spread: float) -> str:
    """2s10s 利差 → regime label."""
    if spread < -0.20: return "深度倒挂"
    if spread < 0:     return "轻微倒挂"
    if spread < 0.30:  return "曲线平坦"
    if spread < 0.80:  return "正常"
    return                     "陡峭化"


def _correlation_regime(corr: float | None) -> tuple[str, str]:
    """GLD vs TIPS 相关性 → regime + reason."""
    if corr is None or not _is_finite_number(corr):
        return ("unknown", "数据不足无法计算")
    if corr <= -0.5:
        return ("normal_hedge", f"TIPS 实际利率 vs GLD = {corr}，正常对冲关系（黄金按剧本涨跌）")
    if corr <= 0:
        return ("weak_hedge",   f"相关性 {corr}，对冲关系变弱")
    if corr <= 0.3:
        return ("aligned",      f"相关性 {corr}，正相关（可能通胀 regime，两者同向）")
    return ("broken",           f"相关性 {corr}，明显正相关，黄金 hedge 关系破裂 — 警惕 regime shift")


def get_bond_monitor() -> dict:
    """入口：拉所有 yield + GLD 计算 correlation + anomaly."""
    asof = datetime.now().isoformat(timespec="minutes")
    yields_out = {}

    # 1) yfinance yield symbols
    for key, cfg in _YF_YIELDS.items():
        df = _fetch_yield_history(cfg["symbol"], period_days=60)
        if df is None or df.empty:
            continue
        # yfinance 返 index=date, columns=[Open, High, Low, Close, ...]
        # 最新在最后，reverse 让 [0] 是最新
        closes = df["Close"].tolist()[::-1]
        vals = [v * cfg["scale"] for v in closes]
        metrics = _compute_yield_metrics(vals, asof=df.index[-1].strftime("%Y-%m-%d"))
        metrics["name"] = cfg["name"]
        metrics["source"] = f"yfinance {cfg['symbol']}"
        yields_out[key] = metrics

    # 2) FRED: 2Y (yfinance 无) + TIPS 10Y 实际利率
    for key, sid, name in [
        ("2y",       "DGS2",   "2Y 国债"),
        ("tips_10y", "DFII10", "10Y TIPS 实际利率"),
    ]:
        rows = _fetch_fred_series(sid, days=60)
        if not rows:
            continue
        vals = [v for _, v in rows]
        metrics = _compute_yield_metrics(vals, asof=rows[0][0] if rows else "")
        metrics["name"] = name
        metrics["source"] = f"FRED {sid}"
        yields_out[key] = metrics

    # 3) 2s10s spread
    spreads_out = {}
    rows = _fetch_fred_series("T10Y2Y", days=30)
    if rows:
        v0 = rows[0][1]
        v20 = rows[19][1] if len(rows) > 19 else v0
        spreads_out["2s10s"] = {
            "value":        round(v0, 3),
            "chg_20d_bps":  round((v0 - v20) * 100, 1),
            "regime":       _spread_regime(v0),
            "asof":         rows[0][0],
        }

    # 4) GLD correlation
    gld_corr = {}
    try:
        import yfinance as yf
        gld_df = yf.Ticker("GLD").history(period="60d", auto_adjust=False)
        if gld_df is not None and not gld_df.empty:
            gld_closes = gld_df["Close"].tolist()[::-1]  # [0] = 最新
            # 计算 daily returns
            gld_rets = [(gld_closes[i] / gld_closes[i+1] - 1) * 100
                        for i in range(min(len(gld_closes) - 1, 25))]
            # vs 10Y 名义 changes (bps)
            if "10y" in yields_out and yields_out["10y"].get("history"):
                y10_hist = yields_out["10y"]["history"]
                y10_chgs = [(y10_hist[i] - y10_hist[i+1]) * 100
                            for i in range(min(len(y10_hist) - 1, 25))]
                gld_corr["vs_10y"] = _rolling_corr(gld_rets, y10_chgs, window=20)
            # vs TIPS 实际利率 changes (bps) — 关键
            if "tips_10y" in yields_out and yields_out["tips_10y"].get("history"):
                tips_hist = yields_out["tips_10y"]["history"]
                tips_chgs = [(tips_hist[i] - tips_hist[i+1]) * 100
                             for i in range(min(len(tips_hist) - 1, 25))]
                gld_corr["vs_tips_10y"] = _rolling_corr(gld_rets, tips_chgs, window=20)
    except Exception as e:
        gld_corr["error"] = str(e)[:100]

    regime, reason = _correlation_regime(gld_corr.get("vs_tips_10y"))
    gld_corr["regime"] = regime
    gld_corr["reason"] = reason

    # 5) Anomaly detection: |z_score_20d| >= 2 = flag (突发波动异常)
    anomalies = []
    for key, m in yields_out.items():
        if isinstance(m, dict) and m.get("z_score_20d"):
            z = abs(m["z_score_20d"])
            if z >= 2.0:
                sev = "high" if z >= 3.0 else "medium"
                dir_zh = "上冲" if m.get("chg_1d_bps", 0) > 0 else "下探"
                anomalies.append({
                    "metric":   key,
                    "reason":   f"{m['name']} 单日 {m['chg_1d_bps']:+.1f} bps ({z:.1f}σ · {dir_zh})",
                    "severity": sev,
                })

    # 6) 绝对水平 warnings (banner only — 不进 decision 评分)
    # 参考区间：
    #   10Y 名义 ≥ 4.5% = 限制性；≥ 5.0% = 高压
    #   TIPS 10Y ≥ 2.0% = 实际利率高位 (历史股市承压)
    #   30Y vs 10Y 20d 差 ≥ 5bps = 长端 duration selling (风险扩散)
    #   2s10s 0-20bps = 刚脱离倒挂 (历史衰退临界)
    warnings: list[dict] = []
    def _warn(lvl: str, msg: str, key: str):
        warnings.append({"level": lvl, "msg": msg, "key": key})

    y10 = yields_out.get("10y", {})
    if isinstance(y10, dict) and _is_finite_number(y10.get("value")):
        v = y10["value"]
        if v >= 5.0:
            _warn("bad",  f"10Y {v:.2f}% 高压区 (>5%)", "10y_high")
        elif v >= 4.5:
            _warn("warn", f"10Y {v:.2f}% 限制性利率 (>4.5%)", "10y_restrictive")

    tips = yields_out.get("tips_10y", {})
    if isinstance(tips, dict) and _is_finite_number(tips.get("value")):
        v = tips["value"]
        if v >= 2.5:
            _warn("bad",  f"实际利率 {v:.2f}% 极端高位", "tips_extreme")
        elif v >= 2.0:
            _warn("warn", f"实际利率 {v:.2f}% 股市历史承压区 (>2%)", "tips_high")

    # 30Y 绝对水平: 长端超长久期资产折现率
    #   历史 2010s 常在 3-4%, ≥5% = 明显偏高, ≥5.5% = 2007 以来罕见
    y30 = yields_out.get("30y", {})
    if isinstance(y30, dict) and _is_finite_number(y30.get("value")):
        v = y30["value"]
        if v >= 5.5:
            _warn("bad",  f"30Y {v:.2f}% 极端高位 (2007 以来罕见)", "30y_extreme")
        elif v >= 5.0:
            _warn("warn", f"30Y {v:.2f}% 长端偏高 (>5%, 长久期折现率压力)", "30y_high")

    # 长端 duration selling: 30Y 20d 涨幅 vs 10Y 差 ≥10bps
    if (isinstance(y30, dict) and isinstance(y10, dict)
            and _is_finite_number(y30.get("chg_20d_bps"))
            and _is_finite_number(y10.get("chg_20d_bps"))):
        d = y30["chg_20d_bps"] - y10["chg_20d_bps"]
        if d >= 10:
            _warn("warn", f"长端加速抛售: 30Y +{y30['chg_20d_bps']:+.1f}bps vs 10Y +{y10['chg_20d_bps']:+.1f}bps (20d)", "duration_selling")

    # 30s10s spread (30Y-10Y term spread): 陡化增强 = 长端 term premium 定价上升
    # spread 稍后放进 macro_context, warning 在此触发
    term_spread_30_10 = None
    if (isinstance(y30, dict) and isinstance(y10, dict)
            and _is_finite_number(y30.get("value")) and _is_finite_number(y10.get("value"))):
        term_spread_30_10 = round((y30["value"] - y10["value"]) * 100, 1)  # bps
        if term_spread_30_10 >= 80:
            _warn("warn", f"30s10s 陡化到 {term_spread_30_10:.0f}bps (>80 = 长端 term premium 定价上升)", "term_spread_steep")

    # 2s10s 刚脱离倒挂: 0 ~ 20bps
    s2s10 = spreads_out.get("2s10s", {})
    if isinstance(s2s10, dict) and _is_finite_number(s2s10.get("value")):
        v = s2s10["value"] * 100  # convert to bps
        if 0 <= v <= 20:
            _warn("info", f"2s10s {v:.0f}bps 刚脱离倒挂 (历史衰退临界期)", "curve_un_inverting")

    # GLD hedge 失效 (bond-gold 常规负相关被打破)
    corr_regime = gld_corr.get("regime")
    if corr_regime in ("weak_hedge", "broken"):
        corr_val = gld_corr.get("vs_tips_10y")
        if _is_finite_number(corr_val):
            _warn("warn", f"GLD/TIPS 对冲失效 (相关性 {corr_val:.2f}) — 通胀/流动性 regime 改变", "hedge_broken")

    # ── 7-9: 大投行 rate strategy desk 常用的 3 个宏观 stress 指标 ─────────
    # 都是 banner-only，绝不进 decision scoring (回测证过 macro 注入退化 6-9%)
    macro_context: dict = {}
    if term_spread_30_10 is not None:
        macro_context["term_spread_30_10_bps"] = term_spread_30_10

    # 7) Equity Risk Premium (Fed Model): SPX EPS yield vs TIPS 10Y
    #    历史 1985+ ERP < 2% 只在 1987/2000/2007 出现，都是 major top
    #    yfinance 已停返 forwardPE → 用 trailingPE (SPY/IVV/VOO 三候选)
    try:
        import yfinance as yf
        pe = None
        pe_type = None
        for sym in ("SPY", "IVV", "VOO"):
            try:
                info = yf.Ticker(sym).info or {}
                fpe = info.get("forwardPE")
                tpe = info.get("trailingPE")
                if fpe and 5 < float(fpe) < 100:
                    pe, pe_type = float(fpe), "forward"
                    break
                if tpe and 5 < float(tpe) < 100:
                    pe, pe_type = float(tpe), "trailing"
                    # 继续找 forward，找到就替换
            except Exception:
                continue
        if pe and isinstance(tips, dict) and _is_finite_number(tips.get("value")):
            pe = round(pe, 2)
            eps_yield = round(100.0 / pe, 2)
            tips_v = tips["value"]
            erp = round(eps_yield - tips_v, 2)
            macro_context[f"spx_{pe_type}_pe"] = pe
            macro_context["spx_eps_yield_pct"] = eps_yield
            macro_context["erp_vs_tips_pct"] = erp
            macro_context["erp_basis"] = pe_type
            pe_label = "前瞻" if pe_type == "forward" else "尾随"
            if erp < 1.0:
                _warn("bad", f"ERP {erp:.2f}% 极端压缩 (SPX {pe_label} EPS yield {eps_yield}% − TIPS {tips_v}%) — 历史顶部区", "erp_extreme")
            elif erp < 2.0:
                _warn("warn", f"ERP {erp:.2f}% 股权风险溢价压缩到危险区 (SPX {pe_label} EPS yield {eps_yield}% − TIPS {tips_v}%，1985+ 仅 1987/2000/2007 出现过)", "erp_low")
    except Exception:
        pass

    # 8) NFCI (Chicago Fed National Financial Conditions Index) — 大投行主用 FCI 之一
    #    >0 = 金融条件紧于均值 (股市承压), <0 = 宽松
    nfci_rows = _fetch_fred_series("NFCI", days=180)  # 180d 拿 12w 趋势
    if nfci_rows:
        nfci_v, nfci_asof = nfci_rows[0][1], nfci_rows[0][0]
        macro_context["nfci"] = {"value": round(nfci_v, 3), "asof": nfci_asof}
        # 12w 前的 NFCI (每周更新, 12 行 ≈ 12 周前)
        if len(nfci_rows) >= 12:
            nfci_12w = nfci_rows[11][1]
            macro_context["nfci_12w_delta"] = round(nfci_v - nfci_12w, 3)
        if nfci_v > 0.5:
            _warn("bad", f"NFCI +{nfci_v:.2f} 金融条件明显收紧 ({nfci_asof})", "nfci_tight")
        elif nfci_v > 0:
            _warn("warn", f"NFCI +{nfci_v:.2f} 金融条件紧于均值 ({nfci_asof})", "nfci_above_avg")

    # 8b) 流动性/影子 QE 指标 — 让 AI 能判断 "nfci loose 背后是 policy tightening 停摆
    #     还是 Treasury 影子 QE 抵消". 之前 bond_ai 只看到 nfci=loose 没上下文,
    #     容易脑补"Treasury 影子 QE" 但数据说 TGA 反而在吸金 (2026-08-26 例证).
    #  RRP (隔夜逆回购): 剩余 → 短端流动性缓冲. 见底 = 断粮.
    rrp_rows = _fetch_fred_series("RRPONTSYD", days=90)
    if rrp_rows:
        rrp_bn = round(rrp_rows[0][1], 1)
        macro_context["rrp_bn"] = rrp_bn
        macro_context["rrp_asof"] = rrp_rows[0][0]
        if rrp_bn < 5:
            _warn("warn", f"RRP {rrp_bn}Bn 见底 (无短端流动性缓冲)", "rrp_drained")
    #  TGA (Treasury General Account): 财政部现金. 增 = 吸金 (紧), 减 = 放钱 (松).
    tga_rows = _fetch_fred_series("WTREGEN", days=90)
    if tga_rows:
        tga_bn = round(tga_rows[0][1] / 1000, 1)  # FRED 单位是 $M
        macro_context["tga_bn"] = tga_bn
        macro_context["tga_asof"] = tga_rows[0][0]
        if len(tga_rows) >= 4:
            tga_4w = tga_rows[3][1] / 1000
            delta = round(tga_bn - tga_4w, 1)
            macro_context["tga_4w_delta_bn"] = delta
            # TGA 增加 = 财政部吸金 = 变相收紧
            if delta > 100:
                _warn("warn", f"TGA 4周 +${delta}Bn (吸金, 变相收紧)", "tga_absorbing")
            elif delta < -100:
                _warn("warn", f"TGA 4周 ${delta}Bn (放钱, 影子 QE 释放)", "tga_releasing")
    #  WALCL (Fed 资产负债表): 缩表=QT, 扩表=QE, 平=停止紧缩
    walcl_rows = _fetch_fred_series("WALCL", days=90)
    if walcl_rows:
        walcl_tn = round(walcl_rows[0][1] / 1_000_000, 3)  # $M → $T
        macro_context["walcl_tn"] = walcl_tn
        macro_context["walcl_asof"] = walcl_rows[0][0]
        if len(walcl_rows) >= 12:
            walcl_12w = walcl_rows[11][1] / 1_000_000
            delta_tn = round(walcl_tn - walcl_12w, 3)
            macro_context["walcl_12w_delta_bn"] = round(delta_tn * 1000, 1)
            # 12w 扩表 > 20B = QT 实质停止
            if delta_tn > 0.02:
                _warn("warn", f"Fed 12周扩表 +${delta_tn*1000:.0f}Bn (QT 已停止)", "qt_paused")
            elif delta_tn < -0.05:
                _warn("warn", f"Fed 12周缩表 ${delta_tn*1000:.0f}Bn (QT 仍在跑)", "qt_active")

    # 8c) 流动性危机三大早期预警 (T-2周) — 机构级监测框架
    #     MOVE (bond vol, T-1天预警, 比 VIX 早)
    #     SOFR - IORB (融资市场地板, T-2周预警)
    #     KBE/SPY (银行相对表现, SVB 前 2 周就跑输)
    #     每个都存 90d 时序 (chart 用) + 历史危机峰值参考
    try:
        import yfinance as yf
        # 8c.i MOVE index — bond 隐含波动率, ICE BofA MOVE
        move_hist = yf.Ticker("^MOVE").history(period="90d")
        if move_hist is not None and not move_hist.empty:
            move_val = round(float(move_hist["Close"].iloc[-1]), 1)
            macro_context["move_index"] = move_val
            # 90d 时序 (每 3 天采样一次控制大小, ~30 点)
            hist_series = [round(float(v), 1) for v in move_hist["Close"].tolist()[::3]]
            macro_context["move_90d_history"] = hist_series
            if len(move_hist) >= 21:
                move_20d = float(move_hist["Close"].iloc[-21])
                macro_context["move_20d_delta"] = round(move_val - move_20d, 1)
            # 阈值: <80 calm, 80-100 normal, 100-140 elevated, 140+ crisis, 180+ extreme
            if move_val >= 180:
                _warn("bad",  f"MOVE {move_val} 债市极端恐慌 (2020-03 / 2023-SVB 级别)", "move_extreme")
            elif move_val >= 140:
                _warn("bad",  f"MOVE {move_val} 债券波动率进入 crisis 区 (>140)", "move_crisis")
            elif move_val >= 100:
                _warn("warn", f"MOVE {move_val} 债券波动率抬升 (>100 = 期限对冲变贵)", "move_elevated")
    except Exception as e:
        logger.warning(f"[bond_monitor] MOVE 拉取失败: {e}")

    # 8c.ii SOFR - IORB spread (Fed 行政地板破位 = 融资市场紧)
    try:
        sofr_rows = _fetch_fred_series("SOFR", days=90)
        iorb_rows = _fetch_fred_series("IORB", days=90)
        if sofr_rows and iorb_rows:
            sofr_v = sofr_rows[0][1]
            iorb_v = iorb_rows[0][1]
            spread_bps = round((sofr_v - iorb_v) * 100, 1)
            macro_context["sofr_pct"] = round(sofr_v, 3)
            macro_context["iorb_pct"] = round(iorb_v, 3)
            macro_context["sofr_iorb_spread_bps"] = spread_bps
            # 90d 时序: 按日期对齐两个序列, 算每日差 (每 3 天采样)
            sofr_map = {d: v for d, v in sofr_rows}
            iorb_map = {d: v for d, v in iorb_rows}
            spread_series = []
            for d in sorted(set(sofr_map) & set(iorb_map)):
                spread_series.append(round((sofr_map[d] - iorb_map[d]) * 100, 1))
            macro_context["sofr_iorb_90d_history"] = spread_series[::3][-30:]
            # 阈值: <0 normal (SOFR 应在 IORB 下), 0-5 warn (地板破位), >5 alert, >15 extreme
            if spread_bps >= 15:
                _warn("bad",  f"SOFR-IORB +{spread_bps}bps 融资市场极端紧 (2019-09 repo 级)", "sofr_iorb_extreme")
            elif spread_bps >= 5:
                _warn("bad",  f"SOFR-IORB +{spread_bps}bps 地板破位, 融资开始紧", "sofr_iorb_alert")
            elif spread_bps >= 1:
                # 0bps 是常态 (SOFR 有时正好=IORB), 只有明显破位 (>=1bp) 才提示
                _warn("warn", f"SOFR-IORB +{spread_bps}bps 短端流动性收紧 (SOFR 超越 IORB 地板)", "sofr_iorb_warn")
    except Exception as e:
        logger.warning(f"[bond_monitor] SOFR/IORB 拉取失败: {e}")

    # 8c.iii KBE/SPY 银行压力 proxy (银行相对大盘, SVB 前 2 周 -8% 就已跌破)
    try:
        import yfinance as yf
        kbe_hist = yf.Ticker("KBE").history(period="120d")  # 120d 才有 90d 的 20d 差
        spy_hist = yf.Ticker("SPY").history(period="120d")
        if (kbe_hist is not None and spy_hist is not None
                and not kbe_hist.empty and not spy_hist.empty
                and len(kbe_hist) >= 21 and len(spy_hist) >= 21):
            ratio_now = float(kbe_hist["Close"].iloc[-1]) / float(spy_hist["Close"].iloc[-1])
            ratio_20d = float(kbe_hist["Close"].iloc[-21]) / float(spy_hist["Close"].iloc[-21])
            delta_20d = round((ratio_now / ratio_20d - 1) * 100, 2)
            macro_context["kbe_spy_ratio"] = round(ratio_now, 5)
            macro_context["kbe_spy_20d_delta_pct"] = delta_20d
            # 90d 时序: 每日算 20d 滚动相对变化 (每 3 天采样)
            kbe_close = kbe_hist["Close"].tolist()
            spy_close = spy_hist["Close"].tolist()
            n = min(len(kbe_close), len(spy_close))
            series = []
            for i in range(20, n):
                r_now = kbe_close[i] / spy_close[i]
                r_20 = kbe_close[i - 20] / spy_close[i - 20]
                series.append(round((r_now / r_20 - 1) * 100, 2))
            macro_context["kbe_spy_90d_history"] = series[::3][-30:]
            # 阈值: >-3% normal, -3~-6% warn (跑输), <-6% alert (SVB 前情), <-10% extreme
            if delta_20d <= -10:
                _warn("bad",  f"KBE/SPY 20d {delta_20d}% 银行严重跑输 (>-10% = SVB/2008 级危机前情)", "bank_stress_extreme")
            elif delta_20d <= -6:
                _warn("bad",  f"KBE/SPY 20d {delta_20d}% 银行明显跑输 (2023-SVB 前 2 周就是这个)", "bank_stress_alert")
            elif delta_20d <= -3:
                _warn("warn", f"KBE/SPY 20d {delta_20d}% 银行相对走弱 (需持续观察)", "bank_stress_warn")
    except Exception as e:
        logger.warning(f"[bond_monitor] KBE/SPY 拉取失败: {e}")

    # 9) Credit spreads — BAML IG + HY OAS (FRED 免费，卖方 credit desk 首选)
    #    IG >120bps = 收紧, >150bps = stress
    #    HY >400bps = 收紧, >500bps = risk-off
    for label, sid, key_low, key_high, thr_low, thr_high in [
        ("IG", "BAMLC0A0CM",     "ig_widening", "ig_stress",  120, 150),
        ("HY", "BAMLH0A0HYM2",   "hy_widening", "hy_stress",  400, 500),
    ]:
        rows = _fetch_fred_series(sid, days=30)
        if not rows:
            continue
        # FRED BAML OAS 单位是 %，乘 100 → bps
        v_bps, asof_d = rows[0][1] * 100, rows[0][0]
        macro_context[f"cdx_{label.lower()}_bps"] = round(v_bps, 1)
        if v_bps > thr_high:
            _warn("bad",  f"{label} 信用利差 {v_bps:.0f}bps (>{thr_high} = {'risk-off' if label=='HY' else 'stress'}, {asof_d})", key_high)
        elif v_bps > thr_low:
            _warn("warn", f"{label} 信用利差 {v_bps:.0f}bps (>{thr_low} = 收紧, {asof_d})", key_low)

    # 10) 全球美元流动性 + 稳定币虹吸 (阶段 1) —— AI 解读的宏观 context
    #     不进决策评分，只加进 macro_context 让 AI 能看到"美元潮汐+稳定币"视角
    try:
        import yfinance as yf
        # DXY 美元指数 (60d 用于 z-score + 20d 变化)
        dxy_hist = yf.Ticker("DX-Y.NYB").history(period="90d")
        if dxy_hist is not None and not dxy_hist.empty and len(dxy_hist) >= 21:
            dxy_val = round(float(dxy_hist["Close"].iloc[-1]), 2)
            dxy_20d_ago = float(dxy_hist["Close"].iloc[-21])
            dxy_pct_20d = round((dxy_val / dxy_20d_ago - 1) * 100, 2)
            dxy_mean = float(dxy_hist["Close"].mean())
            dxy_std = float(dxy_hist["Close"].std())
            dxy_z = round((dxy_val - dxy_mean) / dxy_std, 2) if dxy_std > 0 else 0
            macro_context["dxy"] = dxy_val
            macro_context["dxy_pct_20d"] = dxy_pct_20d
            macro_context["dxy_z_60d"] = dxy_z
            if dxy_val >= 108:
                _warn("bad",  f"DXY {dxy_val} 极强美元 (2022 危机水平, EM 严重承压)", "dxy_extreme")
            elif dxy_val >= 105:
                _warn("warn", f"DXY {dxy_val} 强美元 (>105, EM 股市历史承压)", "dxy_high")

        # === 亚洲 cash indices 直接抓（EWJ/EWY 是 T+1 US-listed ETF, 抓不到当日 Asian close）===
        asia_indices = [
            ("N225",       "^N225",     "Nikkei 225 (日)"),
            ("HSI",        "^HSI",      "Hang Seng (港)"),
            ("SSE",        "000001.SS", "Shanghai Composite (沪)"),
            ("SZSE",       "399001.SZ", "Shenzhen Composite (深)"),
            ("KOSPI",      "^KS11",     "KOSPI (韩)"),
        ]
        asia_moves: dict = {}
        for key, sym, label in asia_indices:
            try:
                h = yf.Ticker(sym).history(period="10d")
                if h is None or h.empty or len(h) < 6:
                    continue
                last = float(h["Close"].iloc[-1])
                prev = float(h["Close"].iloc[-2])
                d5 = float(h["Close"].iloc[max(0, len(h) - 6)])
                chg_1d = round((last / prev - 1) * 100, 2)
                chg_5d = round((last / d5 - 1) * 100, 2)
                asia_moves[key] = {
                    "symbol": sym, "label": label,
                    "close": round(last, 2), "chg_1d": chg_1d, "chg_5d_pct": chg_5d,
                    "asof": h.index[-1].strftime("%Y-%m-%d"),
                }
                if chg_1d <= -3 or chg_5d <= -5:
                    _warn("warn", f"{label} {chg_1d:+.1f}% (5d {chg_5d:+.1f}%) 明显走弱", f"asia_{key.lower()}_weak")
            except Exception:
                pass
        if asia_moves:
            macro_context["asia_indices"] = asia_moves

        # === US futures (24h, 抓 Sunday 夜盘 → Monday 早盘 gap) ===
        us_futures = [
            ("es_f",    "ES=F", "S&P 500 futures"),
            ("nq_f",    "NQ=F", "Nasdaq futures"),
            ("cl_f",    "CL=F", "WTI crude futures"),
            ("gc_f",    "GC=F", "Gold futures"),
        ]
        futures_moves: dict = {}
        for key, sym, label in us_futures:
            try:
                h = yf.Ticker(sym).history(period="3d")
                if h is None or h.empty or len(h) < 2:
                    continue
                last = float(h["Close"].iloc[-1])
                prev = float(h["Close"].iloc[-2])
                chg = round((last / prev - 1) * 100, 2)
                futures_moves[key] = {
                    "symbol": sym, "label": label,
                    "price": round(last, 2), "chg_pct": chg,
                }
            except Exception:
                pass
        if futures_moves:
            macro_context["us_futures"] = futures_moves

        # 油价 (WTI + USO) — Trump 降息 thesis 关键变量之一 (压油价 → 通胀降 → Fed 有借口降息)
        try:
            oil_hist = yf.Ticker("USO").history(period="30d")  # WTI 原油 ETF
            wti_hist = yf.Ticker("CL=F").history(period="5d")  # WTI 期货
            if oil_hist is not None and not oil_hist.empty and len(oil_hist) >= 21:
                uso_val = round(float(oil_hist["Close"].iloc[-1]), 2)
                uso_pct_20d = round((float(oil_hist["Close"].iloc[-1]) / float(oil_hist["Close"].iloc[-21]) - 1) * 100, 2)
                macro_context["oil_uso"] = uso_val
                macro_context["oil_pct_20d"] = uso_pct_20d
                if wti_hist is not None and not wti_hist.empty:
                    macro_context["oil_wti"] = round(float(wti_hist["Close"].iloc[-1]), 2)
                if uso_pct_20d <= -8:
                    _warn("info", f"油价 20d {uso_pct_20d:+.1f}% 明显下跌 (Trump 压油价降通胀 thesis 支持)", "oil_falling")
                elif uso_pct_20d >= 8:
                    _warn("warn", f"油价 20d {uso_pct_20d:+.1f}% 明显上涨 (通胀反弹压力, 阻碍 Fed 降息)", "oil_rising")
        except Exception:
            pass

        # EEM 新兴市场股 (20d 表现 vs SPX 相对强弱)
        eem_hist = yf.Ticker("EEM").history(period="30d")
        spx_hist = yf.Ticker("SPY").history(period="30d")
        if (eem_hist is not None and not eem_hist.empty and len(eem_hist) >= 21
                and spx_hist is not None and not spx_hist.empty and len(spx_hist) >= 21):
            eem_20d = round((float(eem_hist["Close"].iloc[-1]) / float(eem_hist["Close"].iloc[-21]) - 1) * 100, 2)
            spx_20d = round((float(spx_hist["Close"].iloc[-1]) / float(spx_hist["Close"].iloc[-21]) - 1) * 100, 2)
            eem_rel_spx = round(eem_20d - spx_20d, 2)
            macro_context["eem_pct_20d"] = eem_20d
            macro_context["eem_vs_spx_20d"] = eem_rel_spx
            if eem_20d <= -10:
                _warn("bad",  f"EEM 20d {eem_20d:+.1f}% 恐慌抛售 (EM 股市剧烈跑输)", "em_panic")
            elif eem_rel_spx <= -5:
                _warn("warn", f"EEM 20d {eem_20d:+.1f}% 明显跑输 SPX {spx_20d:+.1f}% ({eem_rel_spx:+.1f}pp)", "em_underperform")
    except Exception:
        pass

    # RRP + TGA (Fed 流动性抽干指标)
    rrp_rows = _fetch_fred_series("RRPONTSYD", days=15)
    if rrp_rows:
        rrp_bn = round(rrp_rows[0][1], 1)  # 单位已是 billions USD
        macro_context["rrp_bn"] = rrp_bn
        if rrp_bn <= 100:
            _warn("warn", f"Fed 逆回购 (RRP) 仅剩 ${rrp_bn}B (2023 峰值 $2500B, 流动性明显抽干)", "rrp_drained")

    tga_rows = _fetch_fred_series("WTREGEN", days=15)
    if tga_rows:
        # WTREGEN 单位是 Millions of Dollars → 转 Billions
        tga_bn = round(tga_rows[0][1] / 1000, 1)
        macro_context["tga_bn"] = tga_bn
        if tga_bn >= 900:
            _warn("info", f"Treasury 一般账户 (TGA) ${tga_bn}B (>900B, 债券发行/税收流入抽干流动性)", "tga_high")

    # 各节点 ETF proxy 量能 z-score (20d) —— 价格信号的复证
    # 价涨没量 = 假突破; 价跌没量 = 假恐慌; 价平量放大 = 蓄势待发
    def _vol_z_score(sym: str, days: int = 25) -> Optional[float]:
        try:
            import yfinance as yf
            h = yf.Ticker(sym).history(period=f"{days}d")
            if h is None or h.empty or "Volume" not in h.columns or len(h) < 21:
                return None
            vols = h["Volume"].astype(float).tolist()
            latest = vols[-1]
            baseline = vols[-21:-1]  # 前 20 天
            import statistics
            mean_v = statistics.mean(baseline)
            std_v = statistics.stdev(baseline) if len(baseline) > 1 else 0
            if std_v <= 0:
                return None
            return round((latest - mean_v) / std_v, 2)
        except Exception:
            return None

    volume_confirm: dict = {}
    # 各节点 proxy ETF 映射
    _vol_proxies = [
        ("rates",       "TLT",  "20+Y 美债"),
        ("real_rates",  "TIP",  "TIPS ETF"),
        ("dxy",         "UUP",  "美元 ETF"),
        ("credit_ig",   "LQD",  "投资级信用 ETF"),
        ("credit_hy",   "HYG",  "高收益信用 ETF"),
        ("vol",         "VXX",  "VIX 期货 ETF"),
        ("em",          "EEM",  "新兴市场 ETF"),
        # 各国股市 volume proxy (^N225 / ^KS11 yahoo 不给 index volume, 用 US-listed ETF)
        ("us_equity",   "SPY",  "S&P 500 ETF"),
        ("us_qqq",      "QQQ",  "Nasdaq 100 ETF"),
        ("jp_equity",   "EWJ",  "iShares JP ETF (代 N225)"),
        ("kr_equity",   "EWY",  "iShares KR ETF (代 KOSPI)"),
        ("oil",         "USO",  "WTI 原油 ETF"),
    ]
    for key, sym, label in _vol_proxies:
        z = _vol_z_score(sym)
        if z is not None:
            volume_confirm[key] = {
                "proxy": sym,
                "label": label,
                "vol_z_20d": z,
                # confirmation: |z| >= 1 = 显著放量/缩量，值得关注
                "notable": abs(z) >= 1.0,
            }
    if volume_confirm:
        macro_context["volume_confirm"] = volume_confirm

    # 通胀 (CPI) + 就业 (unemployment rate) - Fed 政策的核心 input
    # 月度数据, 每月中旬更新一次
    cpi_rows = _fetch_fred_series("CPIAUCSL", days=400)   # 头条 CPI (季调)
    core_cpi_rows = _fetch_fred_series("CPILFESL", days=400)  # 核心 CPI (剔除食品能源)
    if cpi_rows and len(cpi_rows) >= 13:
        latest = cpi_rows[0][1]; ago_12m = cpi_rows[12][1]
        cpi_yoy = round((latest / ago_12m - 1) * 100, 2)
        macro_context["cpi_yoy_pct"] = cpi_yoy
        macro_context["cpi_asof"] = cpi_rows[0][0]
        if cpi_yoy >= 4.0:
            _warn("warn", f"CPI YoY +{cpi_yoy}% 通胀偏高 (>4%, Fed 有加息压力)", "cpi_high")
        elif cpi_yoy >= 3.0:
            _warn("info", f"CPI YoY +{cpi_yoy}% 通胀高于 Fed 目标 2%", "cpi_above_target")
    if core_cpi_rows and len(core_cpi_rows) >= 13:
        latest = core_cpi_rows[0][1]; ago_12m = core_cpi_rows[12][1]
        core_yoy = round((latest / ago_12m - 1) * 100, 2)
        macro_context["core_cpi_yoy_pct"] = core_yoy

    unrate_rows = _fetch_fred_series("UNRATE", days=90)   # 失业率
    if unrate_rows:
        unrate = round(unrate_rows[0][1], 1)
        macro_context["unemployment_pct"] = unrate
        macro_context["unemployment_asof"] = unrate_rows[0][0]
        if unrate >= 5.0:
            _warn("warn", f"失业率 {unrate}% 偏高 (>5%, 经济放缓信号)", "unemployment_high")

    # 稳定币市值 (CoinGecko free API) - USDT + USDC 影子美元
    try:
        import urllib.request as _u, json as _j
        _url = "https://api.coingecko.com/api/v3/simple/price?ids=tether,usd-coin&vs_currencies=usd&include_market_cap=true"
        _req = _u.Request(_url, headers={"User-Agent": "Mozilla/5.0"})
        with _u.urlopen(_req, timeout=10) as _r:
            _sc = _j.loads(_r.read())
        _usdt = (_sc.get("tether") or {}).get("usd_market_cap")
        _usdc = (_sc.get("usd-coin") or {}).get("usd_market_cap")
        if _usdt and _usdc:
            stablecoin_bn = round((_usdt + _usdc) / 1e9, 1)
            macro_context["stablecoin_total_bn"] = stablecoin_bn
            macro_context["usdt_bn"] = round(_usdt / 1e9, 1)
            macro_context["usdc_bn"] = round(_usdc / 1e9, 1)
            if stablecoin_bn >= 250:
                _warn("info", f"稳定币影子美元 ${stablecoin_bn}B (USDT ${round(_usdt/1e9,0):.0f}B + USDC ${round(_usdc/1e9,0):.0f}B, 抽血 EM 通胀国 + 压低美债短端)", "stablecoin_large")
    except Exception:
        pass
    #     参照 BofA Hartnett 的 BBI，用免费数据近似（原版含 flow+positioning 需付费）
    #     4 项 z-score 平均（stress 越高 → BBI 越低 → contrarian bull）
    #     Historical baseline (2015-2024 exclude COVID):
    #       VIX:  mean 18, std 6
    #       IG:   mean 130bps, std 35
    #       HY:   mean 425bps, std 130
    #       ERP:  mean 3.0%,  std 1.0%
    try:
        import yfinance as yf
        vix_val = None
        try:
            vh = yf.Ticker("^VIX").history(period="5d")
            if vh is not None and not vh.empty:
                vix_val = round(float(vh["Close"].iloc[-1]), 2)
        except Exception:
            pass
        # 拉 stress 因子（都已在上面算过或再拉）
        ig_bps = macro_context.get("cdx_ig_bps")
        hy_bps = macro_context.get("cdx_hy_bps")
        erp_pct = macro_context.get("erp_vs_tips_pct")

        # 4 项 z-score（正 = stress 高 = 熊）
        components = {}
        if vix_val is not None:
            components["vix"] = round((vix_val - 18) / 6, 2)
        if _is_finite_number(ig_bps):
            components["ig"] = round((ig_bps - 130) / 35, 2)
        if _is_finite_number(hy_bps):
            components["hy"] = round((hy_bps - 425) / 130, 2)
        if _is_finite_number(erp_pct):
            # ERP 低 = stress 高 → 反转符号
            components["erp"] = round((3.0 - erp_pct) / 1.0, 2)

        if len(components) >= 3:  # 至少 3 个原料才算，避免噪音
            avg_z = sum(components.values()) / len(components)
            # BBI: -avg_z * 3 → -3..+3 (低 = fear/contrarian bull, 高 = greed/contrarian bear)
            # 但为了直觉 (高 = 好, 低 = 差)，仍用 -avg_z 作为 BBI
            bbi = round(-avg_z * 3, 2)
            macro_context["vix"] = vix_val
            macro_context["bbi_score"] = bbi
            macro_context["bbi_components"] = components
            # 判读 (Hartnett 逻辑: extreme 时 contrarian 交易)
            if bbi <= -6:
                _warn("bad",  f"BBI {bbi:+.1f} 极度贪婪 (contrarian sell equity, 5 项 stress 都极低)", "bbi_extreme_greed")
            elif bbi >= 6:
                _warn("info", f"BBI {bbi:+.1f} 极度恐惧 (contrarian buy equity, 历史反弹信号)", "bbi_extreme_fear")
            elif bbi <= -3:
                _warn("warn", f"BBI {bbi:+.1f} 偏贪婪 (stress 组件低于均值)", "bbi_greed")
            elif bbi >= 3:
                _warn("info", f"BBI {bbi:+.1f} 偏恐惧 (可能 contrarian 买点)", "bbi_fear")
    except Exception:
        pass

    return _json_safe({
        "asof":            asof,
        "yields":          yields_out,
        "spreads":         spreads_out,
        "gld_correlation": gld_corr,
        "anomalies":       anomalies,
        "warnings":        warnings,
        "macro_context":   macro_context,  # 新增：卖方 rate desk 指标原始值
    })


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(get_bond_monitor(), ensure_ascii=False, indent=2, default=str))
