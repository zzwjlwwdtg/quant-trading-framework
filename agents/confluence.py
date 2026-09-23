"""
Confluence — 多指标共振分析。

取市场信号 dict（market_watch / futures_watch 输出），
统计当前各类技术指标多空方向，输出共振强度和明细。

共振越多 → 当前点位方向越明确 → 信号越可靠。

v0.3+: 支持回测加权（_calibrate_confidence.py 生成 signals/confidence_calibration.json）。
存在校准文件时，bull_weighted / bear_weighted 字段填充加权 raw（按命中率提升权重），
否则退化为 bull_count / bear_count（每信号 1 分）。
"""

from __future__ import annotations

import contextvars
import json
from pathlib import Path

from i18n import t

# ── 校准缓存（启动时一次性读，运行期间不变）─────────────────────────────
_CALIB_PATH = Path(__file__).parent / "signals" / "confidence_calibration.json"
try:
    _CALIB = json.loads(_CALIB_PATH.read_text(encoding="utf-8")) if _CALIB_PATH.exists() else None
except Exception:
    _CALIB = None

# F4 followup fix (2026-09-23): decision_agent 在处理历史 context 时可通过此
# ContextVar 覆盖 _CALIB, 避免 confluence 结果被今日 calibration file 污染.
# 值意义:
#   _UNSET          — 未覆盖 (fallback 模块 _CALIB, 兼容原行为)
#   dict            — 显式提供 calib snapshot
#   None            — 显式无 calibration (backtest 模式)
_CALIB_UNSET = object()
_ACTIVE_CALIB_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar(
    "confluence_active_calib_override", default=_CALIB_UNSET)


def _active_calib():
    """Return the effective calibration for the current call. Historical
    context overrides live module-level _CALIB via ContextVar."""
    override = _ACTIVE_CALIB_OVERRIDE.get()
    if override is _CALIB_UNSET:
        return _CALIB
    return override   # dict or None (explicit unavailable)


def _signal_weight(side: str, key: str, asset_class: str | None = None) -> float:
    """side: 'bull' | 'bear'；key: BULL_RULES / BEAR_RULES 里的稳定 key。
    asset_class: 'commodity'/'equity_leveraged'/'bond'/'equity_single'（None=用 default）
    查找顺序：per_class[asset_class] → default → 1.0（未校准 fallback）。"""
    calib = _active_calib()
    if calib is None:
        return 1.0
    weights_key = f"{side}_weights"
    if asset_class:
        cls_weights = calib.get("per_class", {}).get(asset_class, {}).get(weights_key)
        if cls_weights and key in cls_weights:
            return float(cls_weights[key].get("weight", 1.0))
    return float(calib.get(weights_key, {}).get(key, {}).get("weight", 1.0))


def get_confluence(market: dict) -> dict:
    """
    输入: market_watch / futures_watch 返回的 signal dict
    输出:
      bull_signals  : list[str]  触发的多头信号文字列表
      bear_signals  : list[str]  触发的空头信号文字列表
      bull_count    : int
      bear_count    : int
      net           : int        bull_count - bear_count
      strength      : str        文字描述
      stars         : str        ★★★ / ★★ / ★ / ─ / ▼ / ▼▼ / ▼▼▼
    """
    trend       = market.get("trend",     "neutral")
    ma_stack    = market.get("ma_stack",  "neutral")
    ma_cross    = market.get("ma_cross",  "none")
    rsi         = market.get("rsi_14")  or 50
    rsi_zone    = market.get("rsi_zone",  "neutral")
    rsi_cross   = market.get("rsi_cross", "none")
    cci         = market.get("cci_20")
    cci_zone    = market.get("cci_zone",  "neutral")
    cci_cross   = market.get("cci_cross", "none")
    vol_ratio   = market.get("vol_ratio", 1.0) or 1.0
    vol_zone    = market.get("vol_zone",  "normal")
    new_high    = market.get("is_new_52w_high", False)
    ma20        = market.get("ma20")
    ma50        = market.get("ma50")
    price       = market.get("price")
    bb_zone     = market.get("bb_zone",   "normal")
    bb_pct      = market.get("bb_pct")
    bb_upper    = market.get("bb_upper")
    bb_lower    = market.get("bb_lower")
    psar_signal = market.get("psar_signal", "none")
    psar_trend  = market.get("psar_trend",   1)
    pct_chg     = market.get("pct_chg", 0) or 0
    pct_zone    = market.get("pct_chg_zone", "normal")
    pre_pct     = market.get("pre_pct")
    pre_lbl     = market.get("pre_label", "盘前")
    pre_d       = market.get("pre_date", "")
    post_pct    = market.get("after_pct")
    post_lbl    = market.get("after_label", "盘后")
    post_d      = market.get("after_date", "")
    on_pct      = market.get("overnight_pct")
    on_lbl      = market.get("overnight_label", "夜盘")
    on_d        = market.get("overnight_date", "")
    # asset_class：优先 market["asset_class"]，否则从 ticker 推
    asset_class = market.get("asset_class")
    if not asset_class:
        try:
            from config import get_asset_class
            asset_class = get_asset_class(market.get("ticker", ""))
        except Exception:
            asset_class = None

    # 每条信号带一个 stable key（对应 confidence_calibration.json 中的键）；
    # key=None 表示无对应校准键（SAR 反转 / 盘前盘后夜盘），永不被过滤。
    bull_all: list[tuple[str, str | None]] = []
    bear_all: list[tuple[str, str | None]] = []

    # ── 趋势 / 均线 ──────────────────────────────────────────────────────────
    if trend == "up":
        note_zh = f"(价格{price:.2f} > MA20={ma20:.2f})" if price and ma20 else ""
        note_ja = f"(価格{price:.2f} > MA20={ma20:.2f})" if price and ma20 else ""
        bull_all.append((t(f"价格站上MA20{note_zh}", f"価格がMA20を上回る{note_ja}"), "trend_up"))
    elif trend == "down":
        note_zh = f"(价格{price:.2f} < MA20={ma20:.2f})" if price and ma20 else ""
        note_ja = f"(価格{price:.2f} < MA20={ma20:.2f})" if price and ma20 else ""
        bear_all.append((t(f"价格跌破MA20{note_zh}", f"価格がMA20を割込み{note_ja}"), "trend_down"))

    if ma_stack == "bull":
        note = f"(MA20={ma20:.2f} > MA50={ma50:.2f})" if ma20 and ma50 else ""
        bull_all.append((t(f"均线多排{note}", f"移動平均線が強気配列{note}"), "ma_stack_bull"))
    elif ma_stack == "bear":
        note = f"(MA20={ma20:.2f} < MA50={ma50:.2f})" if ma20 and ma50 else ""
        bear_all.append((t(f"均线空排{note}", f"移動平均線が弱気配列{note}"), "ma_stack_bear"))

    if ma_cross == "golden":
        bull_all.append((t("均线金叉(MA5上穿MA20，近3日)",
                           "ゴールデンクロス(MA5がMA20を上抜け、直近3日内)"), "ma_cross_golden"))
    elif ma_cross == "death":
        bear_all.append((t("均线死叉(MA5下穿MA20，近3日)",
                           "デッドクロス(MA5がMA20を下抜け、直近3日内)"), "ma_cross_death"))

    # ── RSI(14) ───────────────────────────────────────────────────────────────
    if rsi_zone == "oversold":
        bull_all.append((t(
            f"RSI超卖(RSI={rsi:.0f}，正常区间40-60，<35为超卖)",
            f"RSI売られ過ぎ(RSI={rsi:.0f}、通常レンジ40-60、<35で売られ過ぎ)",
        ), "rsi_oversold"))
    elif rsi_zone == "overbought":
        bear_all.append((t(
            f"RSI超买(RSI={rsi:.0f}，正常区间40-60，>70为超买)",
            f"RSI買われ過ぎ(RSI={rsi:.0f}、通常レンジ40-60、>70で買われ過ぎ)",
        ), "rsi_overbought"))

    if rsi_cross == "dn_30":
        bull_all.append((t(
            f"RSI极度超卖(近3日下穿30，当前{rsi:.0f}，历史极值反弹区)",
            f"RSI極端売られ過ぎ(直近3日内に30割込み、現在{rsi:.0f}、反発ゾーン)",
        ), "rsi_cross_dn30"))
    elif rsi_cross == "up_70":
        bear_all.append((t(
            f"RSI进入超买(近3日上穿70，当前{rsi:.0f}，动能过热)",
            f"RSI買われ過ぎ突入(直近3日内に70上抜け、現在{rsi:.0f}、モメンタム過熱)",
        ), "rsi_cross_up70"))

    # ── CCI(20) ───────────────────────────────────────────────────────────────
    cci_str = f"={cci:.0f}" if cci is not None else ""
    if cci_zone == "oversold":
        bull_all.append((t(
            f"CCI超卖(CCI{cci_str}，中性区间-100~+100，当前低于-100)",
            f"CCI売られ過ぎ(CCI{cci_str}、中立レンジ-100~+100、現在-100以下)",
        ), "cci_oversold"))
    elif cci_zone == "overbought":
        bear_all.append((t(
            f"CCI超买(CCI{cci_str}，中性区间-100~+100，当前高于+100)",
            f"CCI買われ過ぎ(CCI{cci_str}、中立レンジ-100~+100、現在+100以上)",
        ), "cci_overbought"))

    if cci_cross == "dn_100":
        bull_all.append((t(
            f"CCI极度超卖(近3日下穿-100，当前{cci_str})",
            f"CCI極端売られ過ぎ(直近3日内に-100割込み、現在{cci_str})",
        ), "cci_cross_dn100"))
    elif cci_cross == "up_100":
        bear_all.append((t(
            f"CCI进入超买(近3日上穿+100，当前{cci_str})",
            f"CCI買われ過ぎ突入(直近3日内に+100上抜け、現在{cci_str})",
        ), "cci_cross_up100"))

    # ── 量价 ──────────────────────────────────────────────────────────────────
    vr_str = t(f"量比={vol_ratio:.2f}", f"出来高比={vol_ratio:.2f}")
    if vol_zone == "expand" and trend == "up":
        bull_all.append((t(
            f"放量上涨({vr_str}，>1.5为放量，价量齐升)",
            f"出来高増の上昇({vr_str}、>1.5で出来高増、価格と出来高同調)",
        ), "vol_expand_up"))
    elif vol_zone == "expand" and trend == "down":
        bear_all.append((t(
            f"放量下跌({vr_str}，>1.5为放量,恐慌抛售)",
            f"出来高増の下落({vr_str}、>1.5で出来高増、パニック売り)",
        ), "vol_expand_down"))

    if new_high and rsi_zone == "overbought" and vol_zone == "shrink":
        bear_all.append((t(
            f"新高缩量背离({vr_str}<0.7，RSI={rsi:.0f}超买，主力出货警示)",
            f"新高値で出来高減の乖離({vr_str}<0.7、RSI={rsi:.0f}買われ過ぎ、機関売り警告)",
        ), "new_high_diverge"))
    elif new_high and rsi_zone != "overbought" and vol_zone != "shrink":
        bull_all.append((t(
            f"健康新高突破({vr_str}放量确认，RSI={rsi:.0f}未超买)",
            f"健全な新高値ブレイク({vr_str}出来高増で確認、RSI={rsi:.0f}買われ過ぎでない)",
        ), "new_high_healthy"))

    # ── Bollinger Bands (20, 2σ) ─────────────────────────────────────────────
    if bb_zone == "above":
        bp  = f"%B={bb_pct:.2f}" if bb_pct is not None else "%B>1"
        ubs_zh = f"上轨={bb_upper:.2f}" if bb_upper else "上轨"
        ubs_ja = f"上限={bb_upper:.2f}" if bb_upper else "上限"
        bear_all.append((t(
            f"突破布林上轨({bp}，{ubs_zh}，价格偏离均线2σ，统计均值回归区)",
            f"ボリンジャー上限突破({bp}、{ubs_ja}、価格が平均から2σ乖離、平均回帰ゾーン)",
        ), "bb_above"))
    elif bb_zone == "below":
        bp  = f"%B={bb_pct:.2f}" if bb_pct is not None else "%B<0"
        lbs_zh = f"下轨={bb_lower:.2f}" if bb_lower else "下轨"
        lbs_ja = f"下限={bb_lower:.2f}" if bb_lower else "下限"
        bull_all.append((t(
            f"跌破布林下轨({bp}，{lbs_zh}，极度超卖，历史回弹概率高)",
            f"ボリンジャー下限割込み({bp}、{lbs_ja}、極端売られ過ぎ、反発確率高)",
        ), "bb_below"))

    # ── Parabolic SAR (AF=0.02, max=0.20) ─── 无 calib key，永不过滤 ────────
    if psar_signal == "bear_flip":
        bear_all.append((t(
            "抛物线SAR转空(SAR从价格下方翻到上方，趋势反转信号，近1根K线)",
            "パラボリックSAR弱気転換(SAR が価格下方から上方へ反転、トレンド転換シグナル、直近1足)",
        ), None))
    elif psar_signal == "bull_flip":
        bull_all.append((t(
            "抛物线SAR转多(SAR从价格上方翻到下方，趋势反转信号，近1根K线)",
            "パラボリックSAR強気転換(SAR が価格上方から下方へ反転、トレンド転換シグナル、直近1足)",
        ), None))

    # ── 当日涨跌幅 ───────────────────────────────────────────────────────────
    if pct_zone == "crash":
        bear_all.append((t(
            f"当日暴跌({pct_chg:+.1f}%，<-5%，恐慌信号，历史规则不适用)",
            f"当日急落({pct_chg:+.1f}%、<-5%、パニックシグナル、過去ルール適用外)",
        ), "pct_crash"))
    elif pct_zone == "drop":
        bear_all.append((t(f"当日下跌({pct_chg:+.1f}%，<-2%)",
                           f"当日下落({pct_chg:+.1f}%、<-2%)"), "pct_drop"))
    elif pct_zone == "mild_drop":
        bear_all.append((t(f"当日小跌({pct_chg:+.1f}%，<-1%)",
                           f"当日小幅下落({pct_chg:+.1f}%、<-1%)"), "pct_mild_drop"))
    elif pct_zone == "surge":
        bull_all.append((t(
            f"当日暴涨({pct_chg:+.1f}%，>+5%)  ⚠超买风险",
            f"当日急騰({pct_chg:+.1f}%、>+5%)  ⚠買われ過ぎリスク",
        ), "pct_surge"))
    elif pct_zone == "pop":
        bull_all.append((t(f"当日上涨({pct_chg:+.1f}%，>+2%)",
                           f"当日上昇({pct_chg:+.1f}%、>+2%)"), "pct_pop"))
    elif pct_zone == "mild_pop":
        bull_all.append((t(f"当日小涨({pct_chg:+.1f}%，>+1%)",
                           f"当日小幅上昇({pct_chg:+.1f}%、>+1%)"), "pct_mild_pop"))

    # ── 盘前 / 盘后 / 夜盘异动（无 calib key，永不过滤）──────────────────────
    # 关键规则：**只把"进行中"或"待开始"的时段计入多空**——
    # "已结束"的时段是过去事件，已被当日 K 线反映，再计一次属于双重计数。
    # （之前 SOXL 共振显示 3 多 vs 2 空，其中"盘前 +5.01%"实际已结束，不该
    # 作为"开盘跳空高开"的预测信号，因为开盘早就过去了。）

    def _is_forward_looking(label: str) -> bool:
        """
        前瞻信号判定：'进行中'、'待开始'、'未开始' 都算未来时段。
        '已结束' / '已收' 已被当日 K 线吸收，不再二次计入。
        """
        if not label:
            return False
        forward_kw = ("进行中", "待开始", "未开始")
        backward_kw = ("已结束", "已收")
        if any(kw in label for kw in backward_kw):
            return False
        return any(kw in label for kw in forward_kw)

    if pre_pct is not None and abs(pre_pct) >= 1.0 and _is_forward_looking(pre_lbl):
        tag = f"[{pre_d} {pre_lbl}]"
        if pre_pct > 0:
            bull_all.append((t(f"{tag} 涨{pre_pct:+.2f}%  → 开盘大概率跳空高开",
                               f"{tag} 上昇{pre_pct:+.2f}%  → 寄付き高寄り確度高"), None))
        else:
            bear_all.append((t(f"{tag} 跌{pre_pct:+.2f}%  → 开盘大概率跳空低开",
                               f"{tag} 下落{pre_pct:+.2f}%  → 寄付き安寄り確度高"), None))

    if post_pct is not None and abs(post_pct) >= 1.0 and _is_forward_looking(post_lbl):
        tag = f"[{post_d} {post_lbl}]"
        if post_pct > 0:
            bull_all.append((t(f"{tag} 涨{post_pct:+.2f}%  → 次开盘动能延续",
                               f"{tag} 上昇{post_pct:+.2f}%  → 翌寄りモメンタム継続"), None))
        else:
            bear_all.append((t(f"{tag} 跌{post_pct:+.2f}%  → 次开盘承压",
                               f"{tag} 下落{post_pct:+.2f}%  → 翌寄り上値重い"), None))

    if on_pct is not None and abs(on_pct) >= 1.5 and _is_forward_looking(on_lbl):
        tag = f"[{on_d} {on_lbl}]"
        if on_pct > 0:
            bull_all.append((t(f"{tag} 涨{on_pct:+.2f}%  → 亚欧时段买盘",
                               f"{tag} 上昇{on_pct:+.2f}%  → 亜欧時間の買い"), None))
        else:
            bear_all.append((t(f"{tag} 跌{on_pct:+.2f}%  → 亚欧时段抛售",
                               f"{tag} 下落{on_pct:+.2f}%  → 亜欧時間の売り"), None))

    # ── B: 校准过滤 —— 有 calib key 且权重=0 的信号视为"历史零预测力"，
    #       从触发列表整体隐藏（PSAR/盘前盘后无 key 永久保留）
    #       权重查找按 asset_class 分类（commodity/equity_leveraged/bond/equity_single）
    calibrated = _active_calib() is not None
    dropped_bull: list[str] = []
    dropped_bear: list[str] = []
    if calibrated:
        kept_bull, kept_bear = [], []
        for txt, k in bull_all:
            if k is None or _signal_weight("bull", k, asset_class) > 0:
                kept_bull.append((txt, k))
            else:
                dropped_bull.append(k)
        for txt, k in bear_all:
            if k is None or _signal_weight("bear", k, asset_class) > 0:
                kept_bear.append((txt, k))
            else:
                dropped_bear.append(k)
        bull_all, bear_all = kept_bull, kept_bear

    bull = [txt for (txt, _) in bull_all]
    bear = [txt for (txt, _) in bear_all]
    _bull_signal_keys = [k for (_, k) in bull_all if k]
    _bear_signal_keys = [k for (_, k) in bear_all if k]

    bull_n = len(bull)
    bear_n = len(bear)
    net    = bull_n - bear_n

    bull_weighted = round(sum(_signal_weight("bull", k, asset_class) for k in _bull_signal_keys), 2)
    bear_weighted = round(sum(_signal_weight("bear", k, asset_class) for k in _bear_signal_keys), 2)

    # ── A: 校准时用加权净分决定 strength/stars（非校准回退 raw net）─────────
    if bull_n + bear_n == 0:
        strength, stars = t("无明确信号", "明確なシグナルなし"), "─"
    elif calibrated:
        wn = bull_weighted - bear_weighted
        if   wn >=  2.5: strength, stars = t("极强多头共振", "極強気共振"),   "★★★"
        elif wn >=  1.5: strength, stars = t("强多头共振",   "強気共振"),     "★★★"
        elif wn >=  1.0: strength, stars = t("多头共振",     "強気優勢"),     "★★"
        elif wn >=  0.4: strength, stars = t("弱多头共振",   "弱気優勢気味"), "★"
        elif wn >  -0.4: strength, stars = t("多空均衡",     "強弱拮抗"),     "─"
        elif wn >  -1.0: strength, stars = t("弱空头共振",   "弱気優勢気味"), "▼"
        elif wn >  -1.5: strength, stars = t("空头共振",     "弱気共振"),     "▼▼"
        elif wn >  -2.5: strength, stars = t("强空头共振",   "強弱気共振"),   "▼▼▼"
        else:            strength, stars = t("极强空头共振", "極弱気共振"),   "▼▼▼"
    elif net >= 5:  strength, stars = t("极强多头共振", "極強気共振"),   "★★★"
    elif net >= 3:  strength, stars = t("强多头共振",   "強気共振"),     "★★★"
    elif net >= 2:  strength, stars = t("多头共振",     "強気優勢"),     "★★"
    elif net == 1:  strength, stars = t("弱多头共振",   "弱気優勢気味"), "★"
    elif net == 0:  strength, stars = t("多空均衡",     "強弱拮抗"),     "─"
    elif net == -1: strength, stars = t("弱空头共振",   "弱気優勢気味"), "▼"
    elif net >= -2: strength, stars = t("空头共振",     "弱気共振"),     "▼▼"
    elif net >= -3: strength, stars = t("强空头共振",   "強弱気共振"),   "▼▼▼"
    else:           strength, stars = t("极强空头共振", "極弱気共振"),   "▼▼▼"

    return {
        "bull_signals":  bull,
        "bear_signals":  bear,
        "bull_count":    bull_n,
        "bear_count":    bear_n,
        "bull_weighted": bull_weighted,
        "bear_weighted": bear_weighted,
        "bull_keys":     _bull_signal_keys,
        "bear_keys":     _bear_signal_keys,
        "dropped_bull_keys": dropped_bull,
        "dropped_bear_keys": dropped_bear,
        "asset_class":   asset_class,
        "calibrated":    calibrated,
        "net":           net,
        "strength":      strength,
        "stars":         stars,
    }


def format_confluence(cf: dict, compact: bool = False) -> list[str]:
    """
    返回可直接传给 logger.info 的行列表。
    compact=True: 单行摘要；False: 展开多空信号列表。
    """
    b, s = cf["bull_count"], cf["bear_count"]
    stars = cf["stars"]
    strength = cf["strength"]

    none_zh = "无"
    none_ja = "なし"
    if compact:
        bull_str = " · ".join(cf["bull_signals"]) or t(none_zh, none_ja)
        bear_str = " · ".join(cf["bear_signals"]) or t(none_zh, none_ja)
        return [t(
            f"  共振: 多{b} 空{s}  {strength} {stars}  [多:{bull_str}]  [空:{bear_str}]",
            f"  共振: 強{b} 弱{s}  {strength} {stars}  [強:{bull_str}]  [弱:{bear_str}]",
        )]

    header = t(
        f"  共振: 多头({b}) vs 空头({s})  →  {strength} {stars}",
        f"  共振: 強気({b}) vs 弱気({s})  →  {strength} {stars}",
    )
    lines = [header]
    bull_tag = t("[多]", "[強]")
    bear_tag = t("[空]", "[弱]")
    if cf["bull_signals"]:
        for sig in cf["bull_signals"]:
            lines.append(f"    {bull_tag} {sig}")
    if cf["bear_signals"]:
        for sig in cf["bear_signals"]:
            lines.append(f"    {bear_tag} {sig}")
    return lines
