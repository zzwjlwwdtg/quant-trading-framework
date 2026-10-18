"""
PCA + Marchenko-Pastur — SOX 半导体板块协方差谱分析。

核心问题：SOXL 今日的涨跌，多少是板块系统性因子（NVDA/AMD/TSM 同涨同跌），
多少是 SOXL 杠杆自身的 alpha？

数学基础：
  随机矩阵理论（Marchenko-Pastur 定理）：
    - T × N 个 iid 标准化收益做相关矩阵 C
    - 在 N → ∞、T → ∞、Q = N/T 固定下，C 的特征值分布
      上下界为 λ± = (1 ± √Q)²
    - 超出 λ+ 的特征值 = 真实信号（市场/行业/轮动因子）
    - 在 [λ-, λ+] 区间内的 = 采样噪声，无信息

应用：
  · 解释方差最大的 1 个特征值 = "市场/板块共有因子"
  · 第 2-5 个 = 轮动/行业内子板块（设计 vs 制造 vs 设备）
  · 今日 PC1 投影 → 板块整体强弱
  · SOXL 收益 - PC1 解释 = SOXL 自身 alpha
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from i18n import t


# ── SOX 30（PHLX Semiconductor Sector）成分股 ─────────────────────────────────
# 已剔除 ARM（2023 上市，历史不足）。N=28，T=252 时 Q=0.111，MP bounds ≈ [0.44, 1.78]。
SOX_TICKERS = [
    "NVDA", "AMD", "INTC", "TSM", "AVGO", "QCOM", "MU", "AMAT",
    "LRCX", "KLAC", "TXN", "ASML", "MRVL", "ADI", "ON", "MCHP",
    "NXPI", "MPWR", "COHR", "SWKS", "QRVO", "WOLF", "ENPH", "FSLR",
    "STM", "IPGP", "SLAB", "RMBS",
]


# 业务板块归类（用于残差动量分析的主题聚合）
SOX_THEME = {
    "NVDA": "AI/GPU",         "AMD":  "AI/GPU",
    "AVGO": "AI/网络",         "MRVL": "AI/网络",
    "TSM":  "代工",            "INTC": "代工/CPU",
    "MU":   "存储",            "RMBS": "存储/IP",
    "QCOM": "手机/无线",       "SWKS": "手机/射频",   "QRVO": "手机/射频",
    "AMAT": "设备(WFE)",       "LRCX": "设备(WFE)",  "KLAC": "设备(WFE)",  "ASML": "设备(WFE)",
    "TXN":  "模拟/工业",       "ADI":  "模拟/工业",   "NXPI": "模拟/汽车",
    "MCHP": "MCU/工业",        "ON":   "模拟/汽车",   "STM":  "模拟/汽车",   "SLAB": "MCU/物联网",
    "MPWR": "电源管理",        "WOLF": "电源/碳化硅",
    "ENPH": "太阳能/能源",     "FSLR": "太阳能/能源",
    "COHR": "光通信",          "IPGP": "光纤激光",
}


def fetch_returns(tickers: list[str] = SOX_TICKERS,
                  period: str = "2y",
                  *,
                  min_assets: int = 5) -> Optional[pd.DataFrame]:
    """
    用 yfinance 批量拉日 K 收盘价，转 log return 矩阵 (T × N)。
    成功的 ticker 才保留；任何错误返回 None。

    EVO-20261014-3 (2026-10-19): 退市/长期 404 的 proxy 符号 (如 6488.TW /
    3688.T) 从 retired list 剔除后再拉, 不再因为个别死票整轮跳过; 剔除后
    仍 ≥ min_assets (默认 5) 就继续用 N-k 维跑, 否则才返回 None.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None

    # 先读 retired list, 过滤掉已退市/长期 404 的符号
    try:
        from _data_source_health import list_retired, record_fetch_result
        retired = set(list_retired())
    except Exception:
        retired = set()
        record_fetch_result = None  # noqa: F841

    live_tickers = [tk for tk in tickers if tk not in retired]
    if len(live_tickers) < min_assets:
        return None

    try:
        data = yf.download(
            live_tickers, period=period, interval="1d",
            auto_adjust=True, progress=False, group_by="ticker",
            threads=True,
        )
    except Exception:
        return None
    if data is None or data.empty:
        return None

    closes: dict[str, pd.Series] = {}
    missing: list[str] = []
    for tk in live_tickers:
        try:
            # yfinance 多 ticker 时是 MultiIndex columns
            col = data[tk]["Close"] if (tk, "Close") in data.columns else data["Close"][tk]
            if col is None or col.empty:
                missing.append(tk)
                continue
            closes[tk] = col.dropna()
        except (KeyError, AttributeError):
            try:
                col = data["Close"][tk]
                if col is None or col.empty:
                    missing.append(tk)
                    continue
                closes[tk] = col.dropna()
            except Exception:
                missing.append(tk)
                continue

    # 登记本轮每只票的拉取结果; 连续失败 N 天自动 retire, 不再刷日志
    try:
        from _data_source_health import record_fetch_result as _rec
        for tk in closes:
            _rec(tk, True)
        for tk in missing:
            _rec(tk, False, reason="yfinance_empty")
    except Exception:
        pass

    if len(closes) < min_assets:
        return None

    px = pd.DataFrame(closes).dropna()
    if len(px) < 60:
        return None
    returns = np.log(px / px.shift(1)).dropna()
    return returns


def compute_spectrum(returns: pd.DataFrame) -> dict:
    """
    计算相关矩阵特征值谱 + Marchenko-Pastur 噪音边界。

    返回:
      eigenvalues      : 降序特征值数组
      eigenvectors     : 对应特征向量矩阵 (N × N)
      mp_upper/lower   : MP 噪音上下界
      n_signal         : 超出 λ+ 的特征值数量
      tickers          : 对应的股票列表（按 returns.columns 顺序）
      T, N, Q          : 样本维度
      total_var        : 所有特征值之和（应等于 N）
      pc_variance_pct  : 每个 PC 解释的方差占比
    """
    T, N = returns.shape
    Q = N / T

    # 标准化：z-score，使得相关矩阵 = 协方差矩阵
    standardized = (returns - returns.mean()) / returns.std(ddof=1)

    # 相关矩阵 (N × N)
    corr = standardized.cov().values  # 已经标准化，cov = corr

    # 特征值分解（对称矩阵用 eigh 更稳）
    evals, evecs = np.linalg.eigh(corr)
    # 降序排列
    order = np.argsort(evals)[::-1]
    evals = evals[order]
    evecs = evecs[:, order]

    # MP 噪音边界
    sqrt_Q = math.sqrt(Q)
    mp_upper = (1 + sqrt_Q) ** 2
    mp_lower = (1 - sqrt_Q) ** 2

    # 真信号数量
    n_signal = int(np.sum(evals > mp_upper))

    total_var = float(np.sum(evals))
    pc_variance_pct = (evals / total_var * 100)

    return {
        "eigenvalues":     evals,
        "eigenvectors":    evecs,
        "mp_upper":        mp_upper,
        "mp_lower":        mp_lower,
        "n_signal":        n_signal,
        "tickers":         list(returns.columns),
        "T":               T,
        "N":               N,
        "Q":               Q,
        "total_var":       total_var,
        "pc_variance_pct": pc_variance_pct,
    }


# ── 因子模型：Fama-French 思路应用到 SOX ─────────────────────────────────────
# 构造 4 个 SOX 专属因子（区别于 FF 的 SMB/HML，因为 SOX 全是中大盘且科技股）：
#   MKT   : 板块市场因子（28 只等权平均）
#   MOM   : 动量因子（过去12月赢家组合 - 输家组合，Carhart 风格）
#   AI    : AI 暴露因子（NVDA/AVGO/AMD/TSM/MRVL - 其他）
#   CAPEX : 设备周期因子（WFE - chipmaker，半导体资本支出周期代理）

_AI_TICKERS    = ["NVDA", "AVGO", "AMD", "TSM", "MRVL"]
_WFE_TICKERS   = ["AMAT", "LRCX", "KLAC", "ASML"]


def _fetch_vix_changes(start_date, end_date) -> Optional[pd.Series]:
    """
    拉 VIX 历史日**相对**变化 = (VIX_t - VIX_{t-1}) / VIX_{t-1}。
    用百分比变化（而不是点变化）与股票百分比收益保持量纲一致，
    回归系数 β_dVIX 才有直观意义（VIX 涨 1% → 股票变化 β%）。
    """
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX").history(
            start=start_date, end=end_date, interval="1d",
            auto_adjust=False,
        )
        if vix is None or vix.empty:
            return None
        delta_pct = vix["Close"].pct_change().dropna()
        delta_pct.index = delta_pct.index.tz_localize(None) if delta_pct.index.tz is not None else delta_pct.index
        return delta_pct
    except Exception:
        return None


def compute_factors(returns: pd.DataFrame, mom_lookback: int = 252,
                    mom_skip: int = 21) -> pd.DataFrame:
    """
    构造 5 因子时间序列 (T × 5)：
      MKT   : 板块等权平均（市场因子）
      MOM   : 过去 12 月赢家 - 输家（Carhart 动量）
      AI    : AI 暴露 - 非 AI
      CAPEX : WFE - chip makers
      ΔVIX  : VIX 日变化（波动率冲击因子）
    """
    # MKT: 板块等权平均
    MKT = returns.mean(axis=1)

    # MOM: 过去 ~12 月赢家 - 输家
    avail = returns.columns
    if len(returns) > mom_lookback + mom_skip:
        cum = (1 + returns).cumprod()
        past_ret = cum.iloc[-mom_skip - 1] / cum.iloc[-mom_lookback - mom_skip] - 1
    else:
        past_ret = (1 + returns.iloc[:-1]).prod() - 1
    sorted_tk = past_ret.sort_values(ascending=False).index.tolist()
    n_third = max(len(sorted_tk) // 3, 3)
    winners = sorted_tk[:n_third]
    losers  = sorted_tk[-n_third:]
    MOM = returns[winners].mean(axis=1) - returns[losers].mean(axis=1)

    # AI: AI 暴露 - 非 AI
    ai_present = [tk for tk in _AI_TICKERS if tk in avail]
    non_ai     = [tk for tk in avail if tk not in ai_present]
    if ai_present and non_ai:
        AI = returns[ai_present].mean(axis=1) - returns[non_ai].mean(axis=1)
    else:
        AI = pd.Series(0.0, index=returns.index)

    # CAPEX: WFE - chip makers
    wfe_present  = [tk for tk in _WFE_TICKERS if tk in avail]
    non_wfe      = [tk for tk in avail if tk not in wfe_present and tk not in ai_present]
    if wfe_present and non_wfe:
        CAPEX = returns[wfe_present].mean(axis=1) - returns[non_wfe].mean(axis=1)
    else:
        CAPEX = pd.Series(0.0, index=returns.index)

    factors = pd.DataFrame({"MKT": MKT, "MOM": MOM, "AI": AI, "CAPEX": CAPEX})

    # ΔVIX: VIX 日变化（波动率冲击因子）
    # 注意：VIX 单位是「点」（如 18.5），ΔVIX 单位是「点变化」（如 +1.5）
    # 与其他因子（百分比小数）量纲不同，回归时系数自动消化
    # 对齐时区/索引
    start = returns.index.min() - pd.Timedelta(days=2)
    end   = returns.index.max() + pd.Timedelta(days=1)
    # 去除 returns 索引的时区，方便比对
    if returns.index.tz is not None:
        returns_idx_naive = returns.index.tz_localize(None)
    else:
        returns_idx_naive = returns.index
    vix_delta = _fetch_vix_changes(start.tz_localize(None) if start.tz is not None else start,
                                   end.tz_localize(None) if end.tz is not None else end)
    if vix_delta is not None:
        vix_delta_aligned = vix_delta.reindex(returns_idx_naive).fillna(0.0)
        vix_delta_aligned.index = returns.index
        factors["dVIX"] = vix_delta_aligned
    else:
        factors["dVIX"] = pd.Series(0.0, index=returns.index)

    return factors


def run_factor_regression(stock_returns: pd.Series, factors: pd.DataFrame) -> dict:
    """
    对单只股票跑 OLS 回归：r_i = α + β_MKT·MKT + β_MOM·MOM + β_AI·AI + β_CAPEX·CAPEX + ε
    返回 α、各 β、对应 t 统计量、R²、残差 σ。
    优先用 statsmodels，缺失时退化到 numpy.linalg.lstsq + 手算 t 检验。
    """
    y = stock_returns.values
    X_raw = factors.values
    n, k = X_raw.shape
    X = np.hstack([np.ones((n, 1)), X_raw])   # 加截距列

    try:
        import statsmodels.api as sm
        model = sm.OLS(stock_returns, sm.add_constant(factors), missing="drop").fit()
        params = model.params
        tvals  = model.tvalues
        out = {
            "alpha":   float(params.iloc[0]),
            "alpha_t": float(tvals.iloc[0]),
            "betas":   {factors.columns[i]: float(params.iloc[i+1]) for i in range(k)},
            "tstats":  {factors.columns[i]: float(tvals.iloc[i+1])  for i in range(k)},
            "r2":      float(model.rsquared),
            "resid_sd": float(model.resid.std(ddof=1)),
            "resid":   model.resid,
        }
        return out
    except ImportError:
        # numpy 退化版
        beta_hat, *_ = np.linalg.lstsq(X, y, rcond=None)
        y_hat = X @ beta_hat
        resid = y - y_hat
        ssr   = np.sum(resid ** 2)
        sigma2 = ssr / (n - k - 1)
        # 协方差矩阵
        try:
            cov = sigma2 * np.linalg.inv(X.T @ X)
        except np.linalg.LinAlgError:
            cov = sigma2 * np.linalg.pinv(X.T @ X)
        se = np.sqrt(np.diag(cov))
        t_stats = beta_hat / se
        r2 = 1 - ssr / np.sum((y - y.mean()) ** 2)
        return {
            "alpha":   float(beta_hat[0]),
            "alpha_t": float(t_stats[0]),
            "betas":   {factors.columns[i]: float(beta_hat[i+1]) for i in range(k)},
            "tstats":  {factors.columns[i]: float(t_stats[i+1])  for i in range(k)},
            "r2":      float(r2),
            "resid_sd": float(np.std(resid, ddof=1)),
            "resid":   pd.Series(resid, index=stock_returns.index),
        }


def ewma_volatility(returns: pd.DataFrame, lambda_: float = 0.94) -> pd.Series:
    """
    EWMA 估计每只股票的时变波动率（RiskMetrics 标准 λ=0.94，日频）。
    σ_t² = λ·σ_{t-1}² + (1-λ)·r_{t-1}²
    返回 Series：每只股票的最新一日年化波动率（%）。
    """
    var_t = returns.ewm(alpha=1 - lambda_, adjust=False).var().iloc[-1]
    daily_sd = np.sqrt(var_t) * 100
    return daily_sd


def regime_aware_label(z: float, regime: str) -> str:
    """根据 regime 给残差动量 z 分数贴方向性建议。"""
    if regime == "bull_trending":
        if z > 2.0:    return t("跟（顺动量）",         "順張り（モメンタム順）")
        if z < -2.0:   return t("避（弱势）",           "回避（弱気）")
        return t("中性", "中立")
    if regime in ("overheated", "crisis"):
        if z > 2.0:    return t("减仓警示（反转）",      "利確警告（反転）")
        if z < -2.0:   return t("反弹候选（反转）",      "反発候補（反転）")
        return t("中性", "中立")
    # neutral
    if abs(z) > 3.0:   return t("极端偏离，关注",       "極端な乖離、要注目")
    return t("中性", "中立")


def compute_residual_momentum(returns: pd.DataFrame, spectrum: dict,
                              lookback: int = 5) -> dict:
    """
    残差动量：剔除前 K 个真信号因子后，每只股票过去 N 天的累计残差 z 分数。

    z > +2σ：动量持续(强者恒强)，跟
    z < -2σ：动量衰减，避开
    """
    n_signal = max(spectrum["n_signal"], 1)
    evecs    = spectrum["eigenvectors"][:, :n_signal]   # N × K

    # 标准化收益（与谱分析一致）
    standardized = (returns - returns.mean()) / returns.std(ddof=1)
    Z = standardized.values   # T × N

    # 投影到前 K 因子空间得到每日 PC 得分: T × K
    pc_scores = Z @ evecs

    # 用因子重构每日收益: T × N
    Z_hat = pc_scores @ evecs.T

    # 残差（标准化空间）
    residuals = Z - Z_hat                       # T × N

    # 每只股票残差标准差（历史 baseline）
    res_std = residuals.std(axis=0, ddof=1)
    res_std[res_std == 0] = 1.0

    # 过去 lookback 天累计残差 → z 分数
    last_n = residuals[-lookback:]
    cum_z  = last_n.sum(axis=0) / (res_std * math.sqrt(lookback))

    return {
        "tickers":      list(returns.columns),
        "cumulative_z": cum_z,
        "lookback":     lookback,
    }


def analyze_today(returns: pd.DataFrame, spectrum: dict) -> dict:
    """
    用今日收益向量对前 K 个特征向量做投影，分解为：
      - PC1 (板块系统性因子) 解释的部分
      - PC2-K (轮动/子板块) 解释的部分
      - 残差 (个股 idiosyncratic)

    返回:
      today_return     : N 维今日收益向量
      pc1_score        : 在 PC1 上的投影（标准化）
      pc1_explained    : 今日 PC1 贡献占总方差的百分比
      sector_avg       : 板块平均日收益（简单代表）
      top_drag         : 今日跌最多的 5 只
      top_resist       : 今日抗跌的 5 只
      avg_corr         : 板块内部平均相关系数
    """
    if returns.empty:
        return {}
    today = returns.iloc[-1]   # 最近一根 K 的收益
    today_arr = today.values

    # 用第一特征向量计算 PC1 投影
    evec1 = spectrum["eigenvectors"][:, 0]
    # 标准化今日收益（z-score 相对历史）
    mean = returns.mean()
    std  = returns.std(ddof=1).replace(0, 1)
    z_today = ((today - mean) / std).values

    pc1_score = float(np.dot(z_today, evec1))   # 标准化分数（约服从 N(0,1)）

    # 第一特征向量是「市场因子」——所有股票的载荷正负反映对该因子的暴露
    # SOX 里所有股票应该都正暴露于市场因子，所以 evec1 的元素应大部分同号
    # 如果 evec1 大部分是正，则 PC1>0 意味着板块整体上涨
    sign = 1 if np.sign(evec1).sum() >= 0 else -1
    pc1_score *= sign

    # 板块平均日收益（简单代理）
    sector_avg = float(today.mean() * 100)  # %

    # 今日表现极端的成分股
    today_pct = (today * 100).round(2)
    sorted_today = today_pct.sort_values()
    top_drag    = [(idx, val) for idx, val in sorted_today.head(5).items()]
    top_resist  = [(idx, val) for idx, val in sorted_today.tail(5).iloc[::-1].items()]

    # 板块内部平均相关性
    corr_mat = returns.corr()
    n = len(corr_mat)
    if n > 1:
        upper_tri = corr_mat.values[np.triu_indices(n, k=1)]
        avg_corr = float(np.mean(upper_tri))
    else:
        avg_corr = 0.0

    return {
        "today_date":   str(today.name)[:10] if hasattr(today, "name") else "?",
        "today_return": today_arr,
        "pc1_score":    pc1_score,
        "sector_avg":   sector_avg,
        "top_drag":     top_drag,
        "top_resist":   top_resist,
        "avg_corr":     avg_corr,
    }


def format_pca_report() -> list[str]:
    """生成可直接传给 logger 的多行 PCA 报告。"""
    returns = fetch_returns()
    if returns is None or returns.empty:
        # EVO-20261014-3: 加上 retired 列表, 让"剔除后仍不够样本"与"全部跌掉"可区分
        try:
            from _data_source_health import list_retired
            retired = list_retired()
        except Exception:
            retired = []
        if retired:
            tail = "，".join(retired[:8])
            return [t(
                f"  [PCA] 剔除 retired ({tail}) 后 feature matrix < 最小样本, 跳过本轮分析",
                f"  [PCA] retired 除外 ({tail}) 後サンプル不足、本ラウンド分析スキップ",
            )]
        return [t("  [PCA] 数据拉取失败，跳过本轮分析",
                  "  [PCA] データ取得失敗、本ラウンドの分析スキップ")]

    spec = compute_spectrum(returns)
    today = analyze_today(returns, spec)

    W = 76
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = t("SOX 半导体板块 PCA 分析", "SOX 半導体セクター PCA 解析")

    lines = [
        "+" + "=" * (W - 2) + "+",
        f"|  {title}  |  {now}".ljust(W - 1) + "|",
        "+" + "=" * (W - 2) + "+",
    ]

    # 样本信息
    sample_label = t(
        f"  样本: {spec['N']} 只成分股 × {spec['T']} 个交易日  "
        f"(Q = N/T = {spec['Q']:.3f})",
        f"  サンプル: 構成銘柄 {spec['N']} 銘柄 × 取引日 {spec['T']} 日  "
        f"(Q = N/T = {spec['Q']:.3f})",
    )
    lines.append(sample_label)
    lines.append(t(
        f"  Marchenko-Pastur 噪音边界: λ- = {spec['mp_lower']:.3f}, "
        f"λ+ = {spec['mp_upper']:.3f}",
        f"  Marchenko-Pastur ノイズ境界: λ- = {spec['mp_lower']:.3f}, "
        f"λ+ = {spec['mp_upper']:.3f}",
    ))
    lines.append("")

    # 特征值谱（前 10）
    lines.append(t("  特征值谱 (前 10 大):", "  固有値スペクトル (上位10):"))
    lines.append(t(
        f"  {'#':<4} {'特征值 λ':>10} {'解释方差':>10}  状态",
        f"  {'#':<4} {'固有値 λ':>10} {'寄与率':>10}  状態",
    ))
    lines.append(f"  {'-'*4} {'-'*10} {'-'*10}  ----")

    signal_label = t("真信号", "真シグナル")
    noise_label  = t("噪音",   "ノイズ")
    for i in range(min(10, len(spec["eigenvalues"]))):
        lam = spec["eigenvalues"][i]
        pct = spec["pc_variance_pct"][i]
        if lam > spec["mp_upper"]:
            status = f"★ {signal_label}"
        elif lam < spec["mp_lower"]:
            status = f"· {noise_label}↓"
        else:
            status = f"· {noise_label}"
        lines.append(f"  {i+1:<4} {lam:>10.3f} {pct:>9.1f}%  {status}")
    lines.append("")

    # 真信号汇总
    total_signal_pct = sum(
        spec["pc_variance_pct"][i] for i in range(spec["n_signal"])
    )
    lines.append(t(
        f"  非噪音特征值数: {spec['n_signal']} 个  →  解释总方差 {total_signal_pct:.1f}%",
        f"  非ノイズ固有値数: {spec['n_signal']} 個  →  全体寄与率 {total_signal_pct:.1f}%",
    ))
    lines.append(t(
        f"  剩余 {spec['N'] - spec['n_signal']} 个特征值落在噪音区间内 (无统计意义)",
        f"  残り {spec['N'] - spec['n_signal']} 個の固有値はノイズ範囲内 (統計的意義なし)",
    ))
    lines.append("")

    # 今日板块状态
    lines.append(t(f"  今日板块状态 ({today.get('today_date', '?')}):",
                   f"  本日セクター状況 ({today.get('today_date', '?')}):"))
    pc1 = today.get("pc1_score", 0)
    if   pc1 >= 1.5:  pc1_desc = t("强势上行 (>1.5σ)",   "強い上昇 (>1.5σ)")
    elif pc1 >= 0.5:  pc1_desc = t("温和上行",           "緩やかに上昇")
    elif pc1 >= -0.5: pc1_desc = t("震荡",               "もみ合い")
    elif pc1 >= -1.5: pc1_desc = t("温和下行",           "緩やかに下落")
    else:             pc1_desc = t("强势下行 (<-1.5σ)",  "強い下落 (<-1.5σ)")
    lines.append(t(
        f"    PC1 板块因子分数: {pc1:+.2f}σ  →  {pc1_desc}",
        f"    PC1 セクター因子スコア: {pc1:+.2f}σ  →  {pc1_desc}",
    ))
    lines.append(t(
        f"    板块平均日收益: {today.get('sector_avg', 0):+.2f}%",
        f"    セクター平均日次収益: {today.get('sector_avg', 0):+.2f}%",
    ))
    lines.append(t(
        f"    成分股平均相关性 ρ = {today.get('avg_corr', 0):.2f}  "
        + (("(高度同步)" if today.get('avg_corr', 0) > 0.6 else "(中度联动)"
            if today.get('avg_corr', 0) > 0.3 else "(分化明显)")),
        f"    構成銘柄の平均相関 ρ = {today.get('avg_corr', 0):.2f}  "
        + (("(高度に同期)" if today.get('avg_corr', 0) > 0.6 else "(中程度連動)"
            if today.get('avg_corr', 0) > 0.3 else "(明確に分化)")),
    ))
    lines.append("")

    # 个股极端表现
    lines.append(t("  今日表现极端的个股:", "  本日の極端な銘柄パフォーマンス:"))
    drag_label = t("最拖累", "最も下落")
    resist_label = t("最抗跌", "最も健闘")
    drag_str = "  ".join([f"{tk} {pct:+.2f}%" for tk, pct in today.get("top_drag", [])])
    resist_str = "  ".join([f"{tk} {pct:+.2f}%" for tk, pct in today.get("top_resist", [])])
    lines.append(f"    {drag_label}: {drag_str}")
    lines.append(f"    {resist_label}: {resist_str}")
    lines.append("")

    # 残差动量（剔除真信号因子后，谁还在偏离 → 强者恒强 / 弱者恒弱）
    # 20 日窗口 = 1 个月，是 quant 圈中期动量标准窗口
    res_mom = compute_residual_momentum(returns, spec, lookback=20)
    cum_z = res_mom["cumulative_z"]
    res_tk = res_mom["tickers"]
    idx_sorted = np.argsort(-cum_z)

    lines.append(t(
        f"  残差动量 (过去 {res_mom['lookback']} 日累计 z 分数，剔除 {spec['n_signal']} 个真信号因子):",
        f"  残差モメンタム (過去 {res_mom['lookback']} 日累積 z スコア、{spec['n_signal']} 個の真シグナル因子除去):",
    ))
    # 强势组 (z > +2σ)
    strong = [(res_tk[i], cum_z[i]) for i in idx_sorted if cum_z[i] > 2.0][:8]
    if strong:
        strong_str = "  ".join(f"{tk}({z:+.1f}σ)" for tk, z in strong)
        lines.append(t(
            f"    强势 (>+2σ，强者恒强): {strong_str}",
            f"    強勢 (>+2σ、強さ持続): {strong_str}",
        ))
    else:
        lines.append(t("    强势 (>+2σ): 无", "    強勢 (>+2σ): なし"))
    # 弱势组 (z < -2σ)
    weak = [(res_tk[i], cum_z[i]) for i in idx_sorted[::-1] if cum_z[i] < -2.0][:8]
    if weak:
        weak_str = "  ".join(f"{tk}({z:+.1f}σ)" for tk, z in weak)
        lines.append(t(
            f"    弱势 (<-2σ，避开/反向): {weak_str}",
            f"    弱勢 (<-2σ、回避/逆張り): {weak_str}",
        ))
    else:
        lines.append(t("    弱势 (<-2σ): 无", "    弱勢 (<-2σ): なし"))

    # 主题聚合
    theme_z: dict[str, list] = {}
    for i, tk in enumerate(res_tk):
        theme = SOX_THEME.get(tk, "其他")
        theme_z.setdefault(theme, []).append(float(cum_z[i]))
    theme_avg = sorted(
        [(th, sum(v)/len(v), len(v)) for th, v in theme_z.items()],
        key=lambda x: -x[1],
    )
    lines.append(t("  主题聚合 (按业务板块平均残差动量):",
                   "  テーマ集約 (業務セクター別平均残差モメンタム):"))
    for th, z, n in theme_avg:
        arrow = "↑↑" if z > 1.5 else "↑" if z > 0.5 else "↓" if z < -0.5 else "→"
        if z < -1.5: arrow = "↓↓"
        lines.append(f"    {arrow} {th:<14}  {z:+.2f}σ  (n={n})")
    lines.append("")

    # ── 4 因子模型 + t 检验 + Regime 联动 ──────────────────────────────────
    # 思路（参考 Fama-French → Carhart）:
    #   1. 构造 4 因子: MKT (市场) / MOM (动量) / AI (AI暴露) / CAPEX (设备周期)
    #   2. 对每只股票跑 OLS 回归，得 α、各 β、t 统计量、R²
    #   3. t > 2 才算"真"，否则就是噪音
    #   4. 用 EWMA 估计时变波动，给残差 z 分数做正确归一化
    #   5. 用当前 regime 决定是「顺动量」还是「反转」策略
    try:
        factors = compute_factors(returns)

        # 因子层面：每个因子今日值 / 20日均值 / 显著性
        lines.append(t(
            "  ─── 多因子模型分析 (CAPM → 5因子，含 ΔVIX) ────────────────────",
            "  ─── マルチファクターモデル分析 (CAPM → 5ファクター、ΔVIX含む) ────",
        ))
        lines.append(t(
            f"  {'因子':<10}{'今日':>8}{'20日均':>10}{'年化σ':>10}  说明",
            f"  {'ファクター':<10}{'本日':>8}{'20日平均':>10}{'年化σ':>10}  説明",
        ))
        lines.append(f"  {'-'*10}{'-'*8}{'-'*10}{'-'*10}  ----")
        factor_desc = {
            "MKT":   t("板块市场",          "セクター市場"),
            "MOM":   t("动量(赢-输)",       "モメンタム"),
            "AI":    t("AI暴露",            "AI エクスポージャー"),
            "CAPEX": t("WFE 周期",          "WFE 設備サイクル"),
            "dVIX":  t("VIX 相对变化(恐慌)", "VIX 相対変化(恐怖)"),
        }
        for fname in factors.columns:
            today_v = factors[fname].iloc[-1] * 100
            avg_20  = factors[fname].tail(20).mean() * 100
            ann_sd  = factors[fname].std() * math.sqrt(252) * 100
            lines.append(
                f"  {fname:<10}{today_v:>+7.2f}%{avg_20:>+9.2f}%"
                f"{ann_sd:>9.1f}%  {factor_desc.get(fname, '')}"
            )
        lines.append("")

        # 各股回归：找出真 α（t > 2）的股票
        sig_alphas = []
        all_regs: dict[str, dict] = {}
        for stock in returns.columns:
            reg = run_factor_regression(returns[stock], factors)
            all_regs[stock] = reg
            sig_alphas.append((stock, reg["alpha"], reg["alpha_t"],
                               reg["r2"], reg["betas"]))

        # 按 |α t| 排序（最显著的 α 排前）
        sig_alphas.sort(key=lambda x: -abs(x[2]))

        lines.append(t(
            "  各股 α 显著性 (按 |α 的 t 值| 排序，t>2 才是 *真* 超额收益):",
            "  各銘柄 α 有意性 (|α の t 値| 順、t>2 が真の超過収益):",
        ))
        lines.append(t(
            f"  {'股票':<6}{'α(年化%)':>10}{'α-t':>7}{'MKT-β':>7}{'MOM-β':>7}{'AI-β':>7}{'CAP-β':>7}{'dVIX-β':>8}{'R²':>6}  {'判定':<10}",
            f"  {'銘柄':<6}{'α(年化%)':>10}{'α-t':>7}{'MKT-β':>7}{'MOM-β':>7}{'AI-β':>7}{'CAP-β':>7}{'dVIX-β':>8}{'R²':>6}  {'判定':<10}",
        ))
        lines.append(f"  {'-'*6}{'-'*10}{'-'*7}{'-'*7}{'-'*7}{'-'*7}{'-'*7}{'-'*8}{'-'*6}  ----------")
        for stk, a, at, r2, betas in sig_alphas[:12]:
            a_ann = a * 252 * 100
            if   abs(at) > 3:  verdict = t("★★★ 极显著", "★★★ 極めて有意")
            elif abs(at) > 2:  verdict = t("★★ 显著",    "★★ 有意")
            elif abs(at) > 1.5: verdict = t("★ 边缘",     "★ 境界")
            else:               verdict = t("─ 不显著",   "─ 非有意")
            lines.append(
                f"  {stk:<6}{a_ann:>+9.2f}%{at:>+6.2f}"
                f"{betas.get('MKT', 0):>+6.2f}{betas.get('MOM', 0):>+6.2f}"
                f"{betas.get('AI', 0):>+6.2f}{betas.get('CAPEX', 0):>+6.2f}"
                f"{betas.get('dVIX', 0):>+7.3f}"
                f"{r2:>5.2f}  {verdict}"
            )
        n_sig = sum(1 for _, _, at, _, _ in sig_alphas if abs(at) > 2)
        lines.append(t(
            f"  → {n_sig}/{len(sig_alphas)} 只股票有显著 α (t>2)，其余收益均可由 4 因子解释",
            f"  → {n_sig}/{len(sig_alphas)} 銘柄が有意な α (t>2)、残りは 4 ファクターで説明可",
        ))
        lines.append("")

        # 时变波动率（EWMA）
        vol = ewma_volatility(returns)
        top_vol = vol.nlargest(5)
        bot_vol = vol.nsmallest(5)
        lines.append(t(
            "  时变波动率 (EWMA λ=0.94, 日波动 %):",
            "  時変ボラティリティ (EWMA λ=0.94, 日次%):",
        ))
        lines.append(t(
            f"    高波动 Top 5: " + "  ".join(f"{tk}({v:.2f}%)" for tk, v in top_vol.items()),
            f"    高ボラ Top 5: " + "  ".join(f"{tk}({v:.2f}%)" for tk, v in top_vol.items()),
        ))
        lines.append(t(
            f"    低波动 Top 5: " + "  ".join(f"{tk}({v:.2f}%)" for tk, v in bot_vol.items()),
            f"    低ボラ Top 5: " + "  ".join(f"{tk}({v:.2f}%)" for tk, v in bot_vol.items()),
        ))
        lines.append("")

        # Regime 联动 — 直接读系统级单一源 regime_today
        try:
            from regime_today import get_today_regime
            regime = get_today_regime()
        except Exception:
            regime = "neutral"

        regime_label_map = {
            "bull_trending":  t("牛市延续",  "強気継続"),
            "overheated":     t("过热警戒",  "過熱警戒"),
            "recession_risk": t("衰退风险",  "景気後退リスク"),
            "crisis":         t("危机防御",  "危機モード"),
            "neutral":        t("中性",      "中立"),
        }

        # ── 决策矩阵：前提（Regime） + 规则 + 信号应用 ────────────────────────
        # 设计原则：Regime 是先验，信号按 regime 规则解读，背离另开警告
        lines.append(t(
            "  ─── 决策矩阵 (Regime 是前提，信号按规则解读) ─────────────────",
            "  ─── デシジョン・マトリクス (Regime が前提、シグナルはルール解釈) ────",
        ))
        lines.append(t(
            f"  当前 Regime: {regime_label_map.get(regime, regime)}",
            f"  現在 Regime: {regime_label_map.get(regime, regime)}",
        ))

        # 决策矩阵 — 5 种 regime 的解读规则
        matrix_lines = {
            "bull_trending": [
                t("  ┌─ 高残差 z > +2σ : 顺势跟（强者恒强）",
                  "  ┌─ 高残差 z > +2σ : 順張り（強さ持続）"),
                t("  ├─ 低残差 z < -2σ : 避开（弱势确认）",
                  "  ├─ 低残差 z < -2σ : 回避（弱気確認）"),
                t("  └─ |z| > 3σ      : 极端，关注事件",
                  "  └─ |z| > 3σ      : 極端、イベント注視"),
            ],
            "overheated": [
                t("  ┌─ 高残差 z > +2σ : 减仓警示（反转）",
                  "  ┌─ 高残差 z > +2σ : 利確警告（反転）"),
                t("  ├─ 低残差 z < -2σ : 反弹候选（反转）",
                  "  ├─ 低残差 z < -2σ : 反発候補（反転）"),
                t("  └─ |z| > 3σ      : 极端，等修正",
                  "  └─ |z| > 3σ      : 極端、修正待ち"),
            ],
            "recession_risk": [
                t("  ┌─ 反转策略 + 风控优先", "  ┌─ 反転戦略 + リスク管理優先"),
                t("  ├─ 仅 |z| > 2.5σ 才动", "  ├─ |z| > 2.5σ のみ取引"),
                t("  └─ 全仓减半", "  └─ 全ポジション半減"),
            ],
            "crisis": [
                t("  ┌─ 现金为王", "  ┌─ 現金保持"),
                t("  ├─ 不操作，等 VIX 回 20 以下", "  ├─ 取引停止、VIX 20 割れ待ち"),
                t("  └─ SOXL 直接退出", "  └─ SOXL 完全退出"),
            ],
            "neutral": [
                t("  ┌─ 混合策略", "  ┌─ ミックス戦略"),
                t("  ├─ 仅 |z| > 3σ 才入场", "  ├─ |z| > 3σ のみエントリー"),
                t("  └─ 等 regime 明朗", "  └─ Regime 明確化待ち"),
            ],
        }
        for ln in matrix_lines.get(regime, []):
            lines.append(ln)
        lines.append("")

        # 按当前 regime 规则给每只显著 α 股票贴建议
        lines.append(t(
            "  按上述矩阵应用 (|残差 z| > 1.5):",
            "  上記マトリクスを適用 (|残差 z| > 1.5):",
        ))
        z_sorted = sorted(
            [(res_tk[i], cum_z[i]) for i in range(len(res_tk))],
            key=lambda x: -abs(x[1]),
        )
        any_shown = False
        for tk, z in z_sorted:
            if abs(z) < 1.5:
                break
            label = regime_aware_label(z, regime)
            arrow = "▲" if z > 0 else "▼"
            lines.append(f"    {arrow} {tk:<6}  z={z:+.2f}σ  →  {label}")
            any_shown = True
        if not any_shown:
            lines.append(t("    (无显著偏离)", "    (顕著な乖離なし)"))
        lines.append("")

        # ── 因子背离警告 ──────────────────────────────────────────────────
        # 信号正常给，但当因子状态与 regime 策略反向时，单独发警告
        warnings_list = []
        mom_today = factors["MKT"].iloc[-1] - factors["MKT"].iloc[-1]  # placeholder
        mom_today = float(factors["MOM"].iloc[-1] * 100)
        mom_20d   = float(factors["MOM"].tail(20).mean() * 100)
        ai_today  = float(factors["AI"].iloc[-1] * 100)
        ai_20d    = float(factors["AI"].tail(20).mean() * 100)

        # 警告 1: bull_trending 但 MOM 因子今日急转负
        if regime == "bull_trending" and mom_today < -1.0:
            warnings_list.append(t(
                f"⚠ MOM 因子今日 {mom_today:+.2f}% (20日均 {mom_20d:+.2f}%)，"
                f"与「顺动量」策略反向 — 板块内「赢家组合」开始跑输，"
                f"动量崩盘早期信号。建议：执行高残差跟仓时仓位减半 + 设紧止损",
                f"⚠ MOM ファクター本日 {mom_today:+.2f}% (20日平均 {mom_20d:+.2f}%)、"
                f"「順モメンタム」戦略と逆方向 — 「勝者組合」が劣勢化、"
                f"モメンタム崩壊の初期シグナル。順張り信号は半分のポジ + タイト損切り推奨",
            ))

        # 警告 2: bull_trending 但 MOM 中期累计转负
        if regime == "bull_trending" and mom_20d < -0.5:
            warnings_list.append(t(
                f"⚠ MOM 因子 20 日累计 {mom_20d:+.2f}% (已转负)，"
                f"中期动量已被破坏 — 「强者恒强」叙事可能已过期",
                f"⚠ MOM ファクター 20日累計 {mom_20d:+.2f}% (マイナス転換)、"
                f"中期モメンタム崩壊 — 「強さ持続」ナラティブが期限切れの可能性",
            ))

        # 警告 3: overheated 但 MOM 仍强势
        if regime == "overheated" and mom_20d > 1.0:
            warnings_list.append(t(
                f"⚠ MOM 因子 20 日仍 {mom_20d:+.2f}% (强势)，"
                f"与「反转」策略反向 — 反转可能滞后，先观望勿急减仓",
                f"⚠ MOM ファクター 20日累計 {mom_20d:+.2f}% (依然強気)、"
                f"「反転」戦略と逆方向 — 反転は遅れる可能性、様子見推奨",
            ))

        # 警告 4: AI 因子与 regime 矛盾
        if regime == "bull_trending" and ai_20d < -0.5:
            warnings_list.append(t(
                f"⚠ AI 因子 20 日累计 {ai_20d:+.2f}% (跑输板块)，"
                f"AI 叙事可能在松动 — NVDA/AVGO/AMD/TSM 跟仓需谨慎",
                f"⚠ AI ファクター 20日累計 {ai_20d:+.2f}% (セクター劣勢)、"
                f"AI ナラティブ弱含み — NVDA/AVGO/AMD/TSM 順張り注意",
            ))

        # 警告 5: 板块内部相关性骤升 (恐慌前兆)
        if today.get("avg_corr", 0) > 0.65 and regime not in ("crisis",):
            warnings_list.append(t(
                f"⚠ 板块平均相关 ρ = {today.get('avg_corr', 0):.2f} (>0.65 高同步)，"
                f"个股 alpha 被吃掉 — 接近恐慌或重大事件前夕，警惕系统性风险",
                f"⚠ セクター平均相関 ρ = {today.get('avg_corr', 0):.2f} (>0.65 高同期)、"
                f"個別 alpha が消失 — パニックまたは重大イベント前兆、システミックリスク注意",
            ))

        if warnings_list:
            lines.append(t(
                "  ─── 因子背离警告 (不覆盖信号，但需参考) ──────────────────────",
                "  ─── ファクター乖離警告 (シグナル上書きなし、参考用) ──────",
            ))
            for w in warnings_list:
                lines.append(f"    {w}")
            lines.append("")
    except Exception as exc:
        lines.append(t(f"  因子分析失败: {exc}",
                       f"  ファクター分析失敗: {exc}"))
        lines.append("")

    # SOXL 重仓股残差动量综合（NVDA + AVGO + AMD + TSM + QCOM + MU）
    soxl_heavies = ["NVDA", "AVGO", "AMD", "TSM", "QCOM", "MU"]
    heavies_z = [cum_z[res_tk.index(tk)] for tk in soxl_heavies if tk in res_tk]
    if heavies_z:
        avg_z = float(np.mean(heavies_z))
        if   avg_z > 3.0:   verdict = t("强动量持续 ↑↑ SOXL 顺势持有", "強モメンタム継続 ↑↑ SOXL 順張り保有")
        elif avg_z > 1.0:   verdict = t("温和动量 ↑ SOXL 偏多",        "緩やかなモメンタム ↑ SOXL 強気寄り")
        elif avg_z > -1.0:  verdict = t("中性 → 看技术面",              "中立 → テクニカル次第")
        elif avg_z > -3.0:  verdict = t("动量衰减 ↓ SOXL 减仓警示",     "モメンタム減衰 ↓ SOXL 利確注意")
        else:               verdict = t("强动量崩坏 ↓↓ SOXL 止损",      "モメンタム崩壊 ↓↓ SOXL 損切り")
        lines.append(t(
            f"    SOXL 重仓股残差动量平均: {avg_z:+.2f}σ  →  {verdict}",
            f"    SOXL 主要保有銘柄の残差モメンタム平均: {avg_z:+.2f}σ  →  {verdict}",
        ))
    lines.append("")

    # SOXL 实际涨跌 vs 板块解释（板块 beta vs SOXL alpha 分解）
    soxl_actual = _fetch_soxl_today_pct()
    if soxl_actual is not None:
        # SOXL ≈ 3x SOX（理论上）；用板块平均×3作为板块系统性贡献的粗略估计
        sector_beta_part = today.get("sector_avg", 0) * 3.0
        soxl_alpha       = soxl_actual - sector_beta_part
        lines.append(t(
            f"  SOXL 板块 beta vs 个股 alpha 分解:",
            f"  SOXL のセクター beta vs 個別 alpha 分解:",
        ))
        lines.append(t(
            f"    SOXL 实际: {soxl_actual:+.2f}%  =  "
            f"板块解释 {sector_beta_part:+.2f}% (板块平均×3)  +  "
            f"SOXL alpha {soxl_alpha:+.2f}%",
            f"    SOXL 実績: {soxl_actual:+.2f}%  =  "
            f"セクター由来 {sector_beta_part:+.2f}% (セクター平均×3)  +  "
            f"SOXL 個別 alpha {soxl_alpha:+.2f}%",
        ))
        if abs(soxl_alpha) > 1.5:
            extra = t(
                f"⚠ SOXL 偏离板块明显（{soxl_alpha:+.1f}%），杠杆/流动性/分红除息等额外因素",
                f"⚠ SOXL がセクターから乖離大（{soxl_alpha:+.1f}%）、レバ/流動性/権利落ち等の追加要因",
            )
            lines.append(f"    {extra}")
        lines.append("")

    # 对 SOXL 的解读
    lines.append(t("  → 对 SOXL 的解读:", "  → SOXL への解釈:"))
    if abs(pc1) >= 1.5 and today.get("avg_corr", 0) > 0.5:
        msg = t(
            "板块系统性大行情，SOXL 涨跌主要由板块整体驱动，杠杆放大",
            "セクター全体の大相場、SOXL は主にセクター全体に駆動され、レバレッジ拡大",
        )
    elif abs(pc1) >= 0.5 and today.get("avg_corr", 0) > 0.4:
        msg = t(
            "板块温和方向性，SOXL 仍主要跟随板块",
            "セクターは緩やかな方向性、SOXL は引き続きセクター追従",
        )
    elif today.get("avg_corr", 0) < 0.3:
        msg = t(
            "板块内部分化严重，SOXL 个股层面波动可能放大",
            "セクター内が明確に分化、SOXL は個別株要因でボラ拡大の可能性",
        )
    else:
        msg = t(
            "板块震荡无明显方向，SOXL 受个股 + 杠杆双重影响",
            "セクターは方向感なし、SOXL は個別株 + レバレッジ両面の影響",
        )
    lines.append(f"    {msg}")

    lines.append("=" * W)
    return lines


def _fetch_soxl_today_pct() -> Optional[float]:
    """yfinance 拉 SOXL 最新日涨跌幅 (%)。失败返回 None。"""
    try:
        import yfinance as yf
        hist = yf.Ticker("SOXL").history(period="5d", interval="1d")
        if len(hist) < 2:
            return None
        prev = float(hist["Close"].iloc[-2])
        curr = float(hist["Close"].iloc[-1])
        return (curr - prev) / prev * 100 if prev else None
    except Exception:
        return None


if __name__ == "__main__":
    for line in format_pca_report():
        print(line)
