"""
ReportGenerator — 综合报告生成器。
整合 moomoo OpenD（技术面）+ Yahoo Finance（行情）+ CNN F&G（情绪）。
在美股开盘后和收盘后各生成一次，保存至 signals/report_YYYYMMDD_HHMM.txt。
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from config import SIGNALS_DIR
from data_feeds import (
    fetch_market_data, fetch_fear_greed,
    vix_label, fg_label, yield_label,
)
from fred_feeds import fetch_fred, yield_curve_label, inflation_exp_label
from events_watch import get_events_signal
from i18n import t

ET = ZoneInfo("America/New_York")
LINE = "-" * 64


def _pct_arrow(pct) -> str:
    if pct is None: return "  N/A  "
    sign = "▲" if pct >= 0 else "▼"
    return f"{sign}{abs(pct):5.2f}%"


def _signal_icon(action: str) -> str:
    icons = {
        "BUY":       t("🟢买入",  "🟢買い"),
        "SELL":      t("🔴卖出",  "🔴売り"),
        "REDUCE":    t("⚠️减仓",  "⚠️ポジション縮小"),
        "CAUTION":   t("🔶警示",  "🔶警戒"),
        "WATCH_BUY": t("🟡关注",  "🟡注目"),
        "WATCH_BUY_PROBE": t("🟠试探仓", "🟠試し買い"),
        "HOLD":      t("⬜观望",  "⬜様子見"),
    }
    return icons.get(action, action)


def generate_report(session_type: str = "定时") -> str:
    now_et = datetime.now(ET)
    now_str = now_et.strftime("%Y-%m-%d %H:%M ET")

    lines = []
    W = 66

    def box(text):
        lines.append("+" + "=" * (W - 2) + "+")
        lines.append("|  " + text.ljust(W - 4) + "|")
        lines.append("+" + "=" * (W - 2) + "+")

    def section(title):
        lines.append(f"\n【{title}】")
        lines.append(LINE)

    box(t(f"📊 美股市场综合报告  |  {session_type}",
          f"📊 米国株市場サマリーレポート  |  {session_type}"))
    lines.append(t(f"  生成时间: {now_str}", f"  生成時刻: {now_str}"))

    # ── 1. 公开行情数据 ────────────────────────────────────────────────────────
    section(t("一、核心标的行情", "一、コア銘柄相場"))
    mkt = fetch_market_data()

    focus = ["TQQQ", "SOXL", "GLD", "QQQ", "SPY"]
    hdr_sym   = t("标的", "銘柄")
    hdr_name  = t("名称", "名称")
    hdr_price = t("价格", "価格")
    hdr_chg   = t("涨跌", "騰落")
    hdr_vol   = t("量比", "出来高比")
    lines.append(f"  {hdr_sym:<10} {hdr_name:<10} {hdr_price:>8}  {hdr_chg:>8}  {hdr_vol:>5}")
    lines.append(f"  {'-'*10} {'-'*10} {'-'*8}  {'-'*8}  {'-'*5}")
    # 标的中文名 → 日語名
    _name_ja = {
        "3x纳指":    "ナス3倍",
        "3x半导体":  "半導体3倍",
        "黄金ETF":   "金 ETF",
        "纳斯达克100": "ナスダック100",
        "标普500":   "S&P 500",
    }
    for sym in focus:
        d = mkt.get(sym, {})
        price  = f"${d['price']:.2f}"  if d.get("price")    else "  N/A"
        pct    = _pct_arrow(d.get("pct_chg"))
        vol    = f"{d['vol_ratio']:.2f}" if d.get("vol_ratio") else " N/A"
        name   = d.get("name", sym)
        name   = t(name, _name_ja.get(name, name))
        lines.append(f"  {sym:<10} {name:<10} {price:>8}  {pct:>8}  {vol:>5}")

    # ── 2. 市场情绪 ────────────────────────────────────────────────────────────
    section(t("二、市场情绪", "二、市場センチメント"))
    fg = fetch_fear_greed()
    vix_d = mkt.get("^VIX", {})
    vix_v = vix_d.get("price")

    lines.append(t(
        f"  VIX 恐慌指数    {vix_v or 'N/A':>6}   → {vix_label(vix_v)}",
        f"  VIX 恐怖指数    {vix_v or 'N/A':>6}   → {vix_label(vix_v)}",
    ))
    if fg.get("score") is not None:
        delta_str = t(
            f"（较昨日 {'↑' if fg['delta'] >= 0 else '↓'}{abs(fg['delta'])}）",
            f"（前日比 {'↑' if fg['delta'] >= 0 else '↓'}{abs(fg['delta'])}）",
        )
        lines.append(t(
            f"  CNN 贪婪指数   {fg['score']:>5}/100  {fg['rating']} {delta_str}",
            f"  CNN 強欲指数   {fg['score']:>5}/100  {fg['rating']} {delta_str}",
        ))
        lines.append(t(f"  情绪解读: {fg_label(fg['score'])}",
                       f"  センチメント: {fg_label(fg['score'])}"))
    else:
        lines.append(t("  CNN 贪婪指数   获取失败",
                       "  CNN 強欲指数   取得失敗"))

    btc = mkt.get("BTC-USD", {})
    if btc.get("price"):
        lines.append(t(
            f"  比特币 BTC     ${btc['price']:,.0f}  {_pct_arrow(btc.get('pct_chg'))}",
            f"  ビットコイン BTC ${btc['price']:,.0f}  {_pct_arrow(btc.get('pct_chg'))}",
        ))

    # ── 3. 宏观指标 ────────────────────────────────────────────────────────────
    section(t("三、宏观指标", "三、マクロ指標"))
    tnx = mkt.get("^TNX", {})
    dxy = mkt.get("DX-Y.NYB", {})
    oil = mkt.get("CL=F", {})

    if tnx.get("price"):
        lines.append(t(
            f"  10Y美债收益率  {tnx['price']:.2f}%  {_pct_arrow(tnx.get('pct_chg'))}",
            f"  10年米国債利回り {tnx['price']:.2f}%  {_pct_arrow(tnx.get('pct_chg'))}",
        ))
        lines.append(t(f"  解读: {yield_label(tnx['price'])}",
                       f"  解説: {yield_label(tnx['price'])}"))
    if dxy.get("price"):
        lines.append(t(
            f"  美元指数 DXY   {dxy['price']:.1f}  {_pct_arrow(dxy.get('pct_chg'))}",
            f"  ドル指数 DXY   {dxy['price']:.1f}  {_pct_arrow(dxy.get('pct_chg'))}",
        ))
    if oil.get("price"):
        lines.append(t(
            f"  WTI 原油       ${oil['price']:.1f}  {_pct_arrow(oil.get('pct_chg'))}",
            f"  WTI 原油       ${oil['price']:.1f}  {_pct_arrow(oil.get('pct_chg'))}",
        ))

    fred = fetch_fred()
    if fred:
        lines.append("")
        lines.append(t("  -- FRED 宏观数据 --", "  -- FRED マクロデータ --"))
        t10y2y  = (fred.get("T10Y2Y")   or {}).get("value")
        t10yie  = (fred.get("T10YIE")   or {}).get("value")
        fedfunds = (fred.get("FEDFUNDS") or {}).get("value")
        if t10y2y is not None:
            lines.append(t(
                f"  10Y-2Y利差      {t10y2y:+.2f}%  {yield_curve_label(t10y2y)}",
                f"  10年-2年スプレッド {t10y2y:+.2f}%  {yield_curve_label(t10y2y)}",
            ))
        if t10yie is not None:
            lines.append(t(
                f"  盈亏平衡通胀率  {t10yie:.2f}%  {inflation_exp_label(t10yie)}",
                f"  ブレイクイーブン・インフレ率 {t10yie:.2f}%  {inflation_exp_label(t10yie)}",
            ))
        if fedfunds is not None:
            lines.append(t(f"  联邦基金利率    {fedfunds:.2f}%",
                           f"  FFレート         {fedfunds:.2f}%"))
    else:
        lines.append(t("  (FRED数据: 未配置FRED_API_KEY，跳过)",
                       "  (FRED データ: FRED_API_KEY 未設定、スキップ)"))

    # ── 4. 近期重大事件 ────────────────────────────────────────────────────────
    section(t("四、近期重大事件", "四、直近の重要イベント"))
    ev = get_events_signal()
    lines.append(t(
        f"  ⚡ 最近事件: {ev['next_event']}（{ev['days_to_event']}天后）",
        f"  ⚡ 直近イベント: {ev['next_event']}（{ev['days_to_event']}日後）",
    ))
    risk_suffix = (t("  ← 突发新闻已触发", "  ← 速報ニュース発火") if ev["breaking_news"] else "")
    lines.append(t(
        f"  风险等级: {ev['risk_level'].upper()}{risk_suffix}",
        f"  リスクレベル: {ev['risk_level'].upper()}{risk_suffix}",
    ))

    from events_watch import EQUITY_CALENDAR
    from datetime import date
    today = date.today()
    upcoming = sorted(
        [e for e in EQUITY_CALENDAR
         if (datetime.strptime(e["date"], "%Y-%m-%d").date() - today).days <= 21
         and (datetime.strptime(e["date"], "%Y-%m-%d").date() - today).days >= 0],
        key=lambda x: x["date"]
    )[:5]
    for e in upcoming:
        days = (datetime.strptime(e["date"], "%Y-%m-%d").date() - today).days
        tag = "⚡" if days <= 3 else "📋"
        days_label = t(f"{days:>2}天后", f"{days:>2}日後")
        lines.append(f"  {tag} {days_label}  {e['event']} ({e['date']})")

    # ── 5. 综合风险提示 ────────────────────────────────────────────────────────
    section(t("五、风险提示", "五、リスク警告"))
    risks = []
    if vix_v and vix_v < 16:
        risks.append(t(
            f"VIX {vix_v} 偏低，市场自满情绪需警惕，下行风险被低估",
            f"VIX {vix_v} 低水準、市場が楽観過ぎ、下落リスク過小評価",
        ))
    if vix_v and vix_v > 25:
        risks.append(t(
            f"VIX {vix_v} 偏高，市场恐慌情绪较重，波动风险大",
            f"VIX {vix_v} 高水準、市場の恐怖感強く、ボラ・リスク大",
        ))
    if fg.get("score") and fg["score"] > 75:
        risks.append(t(
            f"CNN贪婪指数 {fg['score']} 进入极度贪婪区，历史上此区间回调概率高",
            f"CNN 強欲指数 {fg['score']} は極度の強欲圏、過去この水準では調整確率高",
        ))
    if tnx.get("price") and tnx["price"] > 4.3:
        risks.append(t(
            f"10Y美债 {tnx['price']:.2f}% 处于高位，持续压制成长股估值",
            f"10年米国債 {tnx['price']:.2f}% は高水準、グロース株バリュエーション圧迫継続",
        ))
    if ev["days_to_event"] <= 3:
        risks.append(t(
            f"重大事件 '{ev['next_event']}' 仅{ev['days_to_event']}天后，建议降低杠杆仓位",
            f"重要イベント '{ev['next_event']}' まで{ev['days_to_event']}日、レバレッジ縮小推奨",
        ))
    if ev["breaking_news"]:
        risks.append(t("RSS检测到突发新闻，请核实是否影响持仓",
                       "RSS で速報ニュース検知、保有銘柄への影響確認を"))
    tqqq = mkt.get("TQQQ", {})
    if tqqq.get("pct_chg") and tqqq["pct_chg"] > 5:
        risks.append(t(
            f"TQQQ 单日涨幅 {tqqq['pct_chg']:.1f}%，警惕追高风险",
            f"TQQQ 当日上昇 {tqqq['pct_chg']:.1f}%、高値追いリスクに警戒",
        ))
    if not risks:
        risks.append(t("暂无突出风险信号，维持正常仓位管理",
                       "顕著なリスクシグナルなし、通常のポジション管理維持"))

    for i, r in enumerate(risks[:4], 1):
        lines.append(f"  {'⚠️' if i <= 2 else '📌'} {r}")

    # 仅展示核对材料，不采集进程、不修改决策、不根据 merged 推定验收通过。
    section("六、运行版本与验收证据（只读）")
    try:
        from model_versions import runtime_acceptance_lines
        lines.extend(runtime_acceptance_lines(
            Path(SIGNALS_DIR) / "runtime_acceptance_evidence.json"))
    except Exception:
        # 证据展示失败不影响原报告，必须明确保留未知状态。
        lines.append("  告警：运行证据展示失败；运行身份与验收状态未知")

    # ── 尾部 ────────────────────────────────────────────────────────────────────
    lines.append("\n" + "=" * W)
    lines.append(t(
        f"  数据来源: moomoo OpenD + Yahoo Finance + CNN Money",
        f"  データソース: moomoo OpenD + Yahoo Finance + CNN Money",
    ))
    lines.append(t(
        f"  生成时间: {now_str}  |  仅供参考，不构成投资建议",
        f"  生成時刻: {now_str}  |  参考情報、投資勧誘ではありません",
    ))
    lines.append("=" * W)

    report = "\n".join(lines)

    # 保存到文件
    fname = now_et.strftime("report_%Y%m%d_%H%M.txt")
    fpath = os.path.join(SIGNALS_DIR, fname)
    Path(fpath).write_text(report, encoding="utf-8")

    return report, fpath
