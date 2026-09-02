"""
Notifier — Windows 11 toast + 中文日志输出。
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path

from atomic_io import atomic_write_json
from config import SIGNALS_DIR, get_today_log_path

os.makedirs(SIGNALS_DIR, exist_ok=True)
import io


class _DailyLogHandler(logging.Handler):
    """跨午夜自动切换 run_YYYYMMDD.log 的 FileHandler 代理。

    每条记录写入前比对当前日期，与底层 FileHandler 的日期不同则关闭旧句柄、
    打开新文件。避免 orchestrator 长跑数日后所有日志仍堆在启动日。
    """

    def __init__(self):
        super().__init__()
        self._current_date = None
        self._inner: logging.FileHandler | None = None
        self._rotate_if_needed()

    def _rotate_if_needed(self):
        today = datetime.now().strftime("%Y%m%d")
        if today != self._current_date:
            if self._inner is not None:
                try:
                    self._inner.close()
                except Exception:
                    pass
            self._inner = logging.FileHandler(get_today_log_path(), encoding="utf-8")
            if self.formatter is not None:
                self._inner.setFormatter(self.formatter)
            self._current_date = today

    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        if self._inner is not None:
            self._inner.setFormatter(fmt)

    def emit(self, record):
        self._rotate_if_needed()
        if self._inner is not None:
            self._inner.emit(record)

    def close(self):
        if self._inner is not None:
            try:
                self._inner.close()
            except Exception:
                pass
        super().close()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        _DailyLogHandler(),
        logging.StreamHandler(stream=io.TextIOWrapper(
            __import__("sys").stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )),
    ],
)
logger = logging.getLogger("agents")

from i18n import t, pick

# regime label (zh, ja)
_REGIME = pick({
    "bull_trending":  ("牛市延续",  "強気継続"),
    "bull_extended":  ("牛市延伸",  "強気伸長"),
    "bull_pulling":   ("牛市回调",  "強気押し目"),
    "bull_chop":      ("偏多震荡",  "強気レンジ"),
    "neutral_chop":   ("中性震荡",  "中立レンジ"),
    "risk_off":       ("风险收缩",  "リスクオフ"),
    "overheated":     ("过热警戒",  "過熱警戒"),
    "recession_risk": ("衰退风险",  "景気後退リスク"),
    "crisis":         ("危机防御",  "危機モード"),
    "neutral":        ("中性",      "中立"),
})

_ICONS = pick({
    "BUY":       ("🟢 买入",      "🟢 買い"),
    "SELL":      ("🔴 卖出",      "🔴 売り"),
    "REDUCE":    ("⚠️ 减仓",      "⚠️ ポジション縮小"),
    "CAUTION":   ("🔶 警示",      "🔶 警戒"),
    "WATCH_BUY": ("🟡 关注买入",   "🟡 押し目買い検討"),
    "WATCH_BUY_PROBE": ("🟠 主动试探仓", "🟠 アクティブ試し買い"),
    "WATCH_BUY_LONG_HOLD": ("🟠 V反弹长持仓提示(系统不下单)",
                            "🟠 V字反発・長期保有候補(自動執行なし)"),
    "HOLD":      ("⬜ 持仓观望",   "⬜ 様子見"),
    "ERROR":     ("❌ 错误",       "❌ エラー"),
})

# Decision reason mapping (zh, ja)
_REASON = pick({
    "RSI overbought":                          ("RSI超买",                       "RSI買われ過ぎ"),
    "RSI extreme + vol shrink":                ("RSI极度超买 + 成交量萎缩",       "RSI極端買い + 出来高減"),
    "new high + vol diverge + RSI extreme":    ("创新高 + 量价背离 + RSI极值",    "新高値 + 価格量乖離 + RSI極値"),
    "breaking news":                           ("突发新闻",                       "速報ニュース"),
    "major event tomorrow":                    ("重大事件在即",                   "重要イベント直前"),
    "downtrend confirmed":                     ("下降趋势确认",                   "下落トレンド確認"),
    "oversold + uptrend":                      ("超卖 + 上升趋势",                "売られ過ぎ + 上昇トレンド"),
    "no clear signal":                         ("无明确信号",                     "明確なシグナルなし"),
    "breaking bullish catalyst for gold":      ("突发黄金看涨催化剂",             "金強気の速報触媒"),
    "breaking bearish catalyst for gold":      ("突发黄金看跌催化剂",             "金弱気の速報触媒"),
    "breaking news — wait for direction":      ("突发新闻，等待方向确认",          "速報、方向確認待ち"),
    "RSI extreme + vol divergence":            ("RSI极值 + 量价背离",             "RSI極値 + 価格量乖離"),
    "new high overbought, no bullish catalyst":("创新高但超买，无看涨催化剂",      "新高値だが買われ過ぎ、強気材料なし"),
    "RSI extreme oversold":                    ("RSI极度超卖",                   "RSI極端売られ過ぎ"),
    "extreme oversold + bullish catalyst":     ("极度超卖 + 看涨催化剂",          "極端売られ過ぎ + 強気材料"),
    "bearish news + downtrend":                ("看跌新闻 + 下降趋势",            "弱気ニュース + 下落トレンド"),
    "bullish news + uptrend":                  ("看涨新闻 + 上升趋势",            "強気ニュース + 上昇トレンド"),
    "bullish trend + momentum":                ("强趋势 + 动能延续",              "強トレンド + モメンタム継続"),
    "uptrend + positive confluence":           ("上升趋势 + 多指标共振",          "上昇トレンド + 複数指標の一致"),
    "breaking news + tech extreme bear":       ("突发新闻 + 技术极度看空",         "速報 + テクニカル極端弱気"),
    "oversold but news uncertain":             ("超卖但新闻不确定",               "売られ過ぎだがニュース不透明"),
})

# Detailed explanation by reason (zh, ja)
_REASON_DETAIL = pick({
    "RSI overbought": (
        "RSI(14)={rsi} > 78，动能过热，历史上此区间回调概率较高",
        "RSI(14)={rsi} > 78、モメンタム過熱、過去この水準では調整確率が高い",
    ),
    "RSI extreme + vol shrink": (
        "RSI(14)={rsi} > 82 且量比={vol:.2f} < 0.80，新高缩量=量价背离，主力分发信号",
        "RSI(14)={rsi} > 82 かつ出来高比={vol:.2f} < 0.80、新高値で出来高減=価格量乖離、機関の利確シグナル",
    ),
    "new high + vol diverge + RSI extreme": (
        "价格创52周新高，但成交量仅为均值{vol:.0%}，RSI={rsi} 极度超买，三重顶部信号",
        "価格は52週新高値、しかし出来高は平均の{vol:.0%}のみ、RSI={rsi} 極端買われ過ぎ、三重トップシグナル",
    ),
    "breaking bullish catalyst for gold": (
        "RSS检测到黄金看涨关键词（地缘风险/通胀/避险），事件驱动优先于技术面",
        "RSS で金強気キーワード検出（地政学リスク/インフレ/リスクオフ）、イベント駆動がテクニカルに優先",
    ),
    "RSI extreme + vol divergence": (
        "RSI(14)={rsi} > 80 且量比={vol:.2f} < 0.75，动能枯竭迹象明显",
        "RSI(14)={rsi} > 80 かつ出来高比={vol:.2f} < 0.75、モメンタム枯渇の兆候明白",
    ),
    "no clear signal": (
        "RSI={rsi} 位于中性区间，趋势={trend}，无触发条件，维持观望",
        "RSI={rsi} 中立圏、トレンド={trend}、トリガー条件なし、様子見継続",
    ),
    "major event tomorrow": (
        "距重大宏观事件≤1天，建议降低持仓等待数据落地",
        "重要マクロイベントまで1日以内、ポジション縮小しデータ着地を待つ推奨",
    ),
    "uptrend + positive confluence": (
        "趋势={trend}，多项指标共振偏多；RSI(14)={rsi} 未处于超卖区，属于顺势关注而非抄底",
        "トレンド={trend}、複数指標が強気で一致。RSI(14)={rsi} は売られ過ぎではなく、押し目ではなく順張り候補",
    ),
    "oversold + uptrend": (
        "RSI(14)={rsi} < 38，处于超卖区间，趋势向上，可关注买入机会",
        "RSI(14)={rsi} < 38、売られ過ぎ圏、上昇トレンド、押し目買いの機会注目",
    ),
})

try:
    from winotify import Notification
    _WINOTIFY_OK = True
except ImportError:
    _WINOTIFY_OK = False

# TOAST_ENABLE: 是否弹 Windows 桌面通知. 默认 OFF (0) — 全部走 log,
# 用户开 watch.bat 一个 cmd 窗口最小化即可看到所有信号, 不被桌面通知打断.
# 若确实需要弹窗提醒 (如去卫生间时想被打断), export TOAST_ENABLE=1.
_TOAST_ENABLED = os.environ.get("TOAST_ENABLE", "0") == "1"


def _toast(title: str, body: str) -> None:
    """默认静默 (只 log), TOAST_ENABLE=1 时才弹 Windows 通知."""
    logger.info(f"[TOAST] {title} | {body.replace(chr(10), ' · ')}")
    if not _TOAST_ENABLED or not _WINOTIFY_OK:
        return
    try:
        Notification(app_id="TradingAgents", title=title, msg=body, duration="short").show()
    except Exception:
        pass


def emit(market: dict, events: dict, decision: dict) -> None:
    ticker     = market.get("ticker", "?")
    price      = market.get("price", 0)
    rsi        = market.get("rsi_14", "?")
    vol_ratio  = market.get("vol_ratio", 1.0)
    trend      = market.get("trend", "?")
    action     = decision.get("action", "HOLD")
    confidence = decision.get("confidence", 0)
    reason_en  = decision.get("reason", "")
    engine     = decision.get("engine", "?")
    regime     = _REGIME.get(decision.get("regime", "neutral"), t("中性", "中立"))
    next_ev        = events.get("next_event", "?")
    next_ev_name   = events.get("next_event_name", "")
    next_ev_date   = events.get("next_event_date", "")
    next_ev_impact = events.get("next_event_impact", "normal")
    ev_verify_src   = events.get("event_verify_src", "")
    days_ev        = events.get("days_to_event", 99)
    breaking       = events.get("breaking_news", False)
    gold_bias      = events.get("gold_bias", "")

    icon      = _ICONS.get(action, action)
    reason_zh = _REASON.get(reason_en, reason_en)

    # 详细依据（填入动态数值）
    detail_tpl = _REASON_DETAIL.get(reason_en, "")
    try:
        detail = detail_tpl.format(rsi=rsi, vol=vol_ratio, trend=trend) if detail_tpl else ""
    except Exception:
        detail = ""

    # ── 主信号行 ──────────────────────────────────────────────────────────────
    logger.info(t(
        f"【{ticker}】价格:{price:.2f}  RSI:{rsi}  量比:{vol_ratio}  趋势:{trend}",
        f"【{ticker}】価格:{price:.2f}  RSI:{rsi}  出来高比:{vol_ratio}  トレンド:{trend}",
    ))

    # 重大事件提示（临近 3 天时加重显示）
    if next_ev_name and days_ev <= 3 and next_ev_impact == "high":
        if days_ev == 0:
            timing = t("今日发布", "本日発表")
        elif days_ev == 1:
            timing = t("明日发布", "明日発表")
        else:
            timing = t(f"{days_ev}天后发布", f"{days_ev}日後発表")
        verified_tag = t(
            f"  [{ev_verify_src}已验证:未落地]" if ev_verify_src else "",
            f"  [{ev_verify_src}検証済:未着地]" if ev_verify_src else "",
        )
        logger.info(t(
            f"  *** 重大事件 *** {next_ev_name}  {next_ev_date}  {timing}{verified_tag}",
            f"  *** 重要イベント *** {next_ev_name}  {next_ev_date}  {timing}{verified_tag}",
        ))
    else:
        gold_suffix = (
            t(f"  黄金偏向:{gold_bias}", f"  金バイアス:{gold_bias}") if gold_bias else ""
        )
        logger.info(t(
            f"  事件: {next_ev}（{days_ev}天后）{gold_suffix}",
            f"  イベント: {next_ev}（{days_ev}日後）{gold_suffix}",
        ))

    if breaking:
        gold_suffix = (
            t(f"  黄金偏向:{gold_bias}", f"  金バイアス:{gold_bias}") if gold_bias else ""
        )
        logger.info(t(f"  ⚡ 突发新闻{gold_suffix}", f"  ⚡ 速報ニュース{gold_suffix}"))
    if gold_bias and not breaking:
        logger.info(t(f"  黄金偏向: {gold_bias}", f"  金バイアス: {gold_bias}"))
    from decision_agent import _conf_scale
    _scale = _conf_scale()
    logger.info(t(
        f"  信号: {icon}  置信度:{confidence}/{_scale}  依据:{reason_zh}  行情:{regime}  [{engine}]",
        f"  シグナル: {icon}  信頼度:{confidence}/{_scale}  根拠:{reason_zh}  地合い:{regime}  [{engine}]",
    ))
    if detail:
        logger.info(t(f"  解释: {detail}", f"  解説: {detail}"))

    # 评分明细：让置信度有依据可查
    sb = decision.get("score_breakdown")
    if sb:
        if "raw" in sb:
            # TECHNICAL_ONLY: event 分数显示但不计入总分（event_in_total=0 时加 [参考]）
            ev_disp = sb.get('event', 0)
            ev_in_total = sb.get('event_in_total', ev_disp)
            ev_suffix = "[参考]" if ev_in_total == 0 and ev_disp > 0 else ""
            # v0.3+ 加权后缀
            tw = sb.get("tech_weighted")
            tech_str = f"{sb['tech']}"
            if sb.get("calibrated") and tw is not None and tw != sb["tech"]:
                tech_str = f"{sb['tech']}(加权{tw})"
            logger.info(t(
                f"  评分: 技术{tech_str} + 宏观{sb['macro']} + 事件{ev_disp}{ev_suffix}"
                f" = 原始{sb['raw']}  →  置信度{confidence}/{_scale}",
                f"  スコア: テクニカル{tech_str} + マクロ{sb['macro']} + イベント{ev_disp}{ev_suffix}"
                f" = 素点{sb['raw']}  →  信頼度{confidence}/{_scale}",
            ))
        elif "bear_raw" in sb:
            logger.info(t(
                f"  评分: 空{sb['bear_raw']} vs 多{sb['bull_raw']} (无明确方向)",
                f"  スコア: 弱気{sb['bear_raw']} vs 強気{sb['bull_raw']} (方向感なし)",
            ))

    option_flow = decision.get("options_flow")
    if isinstance(option_flow, dict) and int(option_flow.get("score") or 0) >= 40:
        flow_dir = {
            "bullish": "看多", "bearish": "看空", "mixed": "分歧",
            "neutral": "中性",
        }.get(option_flow.get("direction"), option_flow.get("direction", "?"))
        quality = option_flow.get("data_quality", "snapshot")
        cap = option_flow.get("score_cap", "?")
        sources = "+".join(option_flow.get("sources") or []) or "proxy"
        stale = " [STALE]" if option_flow.get("stale") else ""
        logger.info(
            f"  期权流: {flow_dir} {option_flow.get('score')}/100 "
            f"({sources}, {quality}, cap={cap}){stale}"
        )

    # ── 多指标共振 ────────────────────────────────────────────────────────────
    cg = decision.get("claude_gate")
    if isinstance(cg, dict):
        provider = cg.get("provider", "Codex")
        verdict = cg.get("verdict", "?")
        status = cg.get("status", "")
        reason = cg.get("reason", "")
        demoted = cg.get("demoted_from")
        line = f"  AI Gate: {provider} {verdict}"
        if demoted:
            line += f"  demoted_from={demoted}"
        if status and status != "ok":
            line += f"  status={status}"
        if reason:
            line += f"  reason={reason}"
        logger.info(line)

    cf = decision.get("confluence")
    if cf:
        try:
            from confluence import format_confluence
            for line in format_confluence(cf, compact=False):
                logger.info(line)
        except Exception:
            pass

    # MACD + ADX 观察行（不参与决策，只供人工对照）
    macd_dif  = market.get("macd_dif")
    macd_dea  = market.get("macd_dea")
    macd_zone = market.get("macd_zone", "neutral")
    macd_sig  = market.get("macd_signal", "none")
    adx_14    = market.get("adx_14")
    adx_zone  = market.get("adx_zone", "weak")
    if macd_dif is not None and adx_14 is not None:
        zone_label = {"bull":"零轴上(多)","bear":"零轴下(空)","neutral":"中性"}.get(macd_zone, macd_zone)
        sig_label  = {"golden":" · ★金叉","death":" · ★死叉","none":""}.get(macd_sig, "")
        adx_label  = {"weak":"无趋势","moderate":"弱趋势","strong":"强趋势","extreme":"极强"}.get(adx_zone, adx_zone)
        logger.info(t(
            f"  [仅观察] MACD: DIF {macd_dif:+.2f} / DEA {macd_dea:+.2f} ({zone_label}){sig_label}  ·  ADX {adx_14:.1f} ({adx_label})",
            f"  [観察] MACD: DIF {macd_dif:+.2f} / DEA {macd_dea:+.2f} ({zone_label}){sig_label}  ·  ADX {adx_14:.1f} ({adx_label})",
        ))

    h1_ctx = decision.get("h1_context", "")
    if h1_ctx:
        logger.info(t(f"  入场参考(60分钟): {h1_ctx}", f"  エントリー参考(60分足): {h1_ctx}"))

    # 15分钟K 短线信号
    m15_ctx   = decision.get("m15_context", "")
    m15_alert = decision.get("m15_alert", "")
    if m15_alert:
        logger.info(t(f"  ⚡ 短线警示: {m15_alert}", f"  ⚡ 短期警告: {m15_alert}"))
    if m15_ctx and "无信号" not in m15_ctx and "シグナルなし" not in m15_ctx:
        logger.info(t(
            f"  短线参考(15分钟): {m15_ctx}",
            f"  短期参考(15分足): {m15_ctx}",
        ))

    # ── 突发新闻 URL ──────────────────────────────────────────────────────────
    if breaking:
        for item in events.get("top_headlines", []):
            if isinstance(item, dict) and item.get("title"):
                logger.info(f"  📰 {item['title']}")
                if item.get("url"):
                    logger.info(f"     🔗 {item['url']}")
    logger.info("")   # 空行分隔

    # ── 写 JSON ───────────────────────────────────────────────────────────────
    signal_path = Path(SIGNALS_DIR) / f"{ticker}_latest.json"
    atomic_write_json(
        signal_path,
        {"ts": datetime.now().isoformat(), "market": market,
         "events": events, "decision": decision},
    )

    # ── 追加信号历史（供每日复盘使用）──────────────────────────────────────────
    try:
        from daily_review import append_signal
        append_signal(ticker, action, price, confidence, reason_en, market)
    except Exception:
        pass

    # ── 桌面通知（仅"真下单"级信号）────────────────────────────────────────
    # 之前 bug: `action != "HOLD"` 把 WATCH_BUY/CAUTION/HOLD_CROSS 等一切非-HOLD 都弹
    # → 20 只票 × 5 时段/天 = 每天几十弹窗. 修: 只弹真下单 + 突发 + 高信心 BUY/SELL.
    _TRADE_ACTIONS = ("BUY", "SELL", "SELL_ALL", "REDUCE", "REDUCE_RISK")
    should_toast = (
        action in _TRADE_ACTIONS
        or breaking
        or (action in ("WATCH_BUY", "WATCH_BUY_PROBE") and confidence >= 8)
    )
    if should_toast:
        entry = decision.get("entry_ref")
        stop  = decision.get("stop_ref")
        lines = [f"RSI {rsi} | {reason_zh}"]
        if entry: lines.append(f"入场: {entry:.2f}")
        if stop:  lines.append(f"止损: {stop:.2f}")
        lines.append(f"{next_ev}（{days_ev}天）")
        _toast(f"{icon}  {ticker} ({price:.2f})", "\n".join(lines))
