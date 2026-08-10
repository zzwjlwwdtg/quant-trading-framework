from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from .models import ContentItem
from .settings import DEFAULT_LOOKBACK_HOURS, SIGNAL_DIR, ensure_dirs
from .sources import collect_inbox_items


JST = ZoneInfo("Asia/Tokyo")
REPORT_DIR = SIGNAL_DIR / "reports"


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _jst_day(value: str) -> str:
    dt = _parse_dt(value)
    if not dt:
        return "unknown-date"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST).date().isoformat()


def _now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(JST)


def _shorten(text: str, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _table_cell(text: Any, limit: int = 220) -> str:
    return _shorten(str(text or ""), limit).replace("|", "/")


def _clean_llm_markdown(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _creator_source_index(items: list[ContentItem]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for it in sorted(items, key=lambda x: x.published_at or "", reverse=True):
        grouped[it.creator].append({
            "date": _jst_day(it.published_at),
            "title": it.title or it.item_id,
            "url": it.url,
            "source_type": it.source_type,
            "excerpt": _shorten(it.text, 1200),
        })
    return dict(grouped)


def _recommendations_by_date(sig: dict[str, Any],
                             price_checks: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    recs = list(sig.get("recommendations") or [])
    rows = []
    for idx, rec in enumerate(recs):
        code = str(rec.get("code") or "").strip()
        rec_key = f"{idx}|{code}|{rec.get('published_at') or ''}|{rec.get('item_id') or ''}"
        rows.append({
            "date": _jst_day(rec.get("published_at") or ""),
            "creator": rec.get("creator") or "unknown",
            "code": rec.get("moomoo_code") or f"JP.{rec.get('code', '')}",
            "name": rec.get("company_name") or rec.get("code", ""),
            "side": rec.get("side") or "analysis",
            "conviction": rec.get("conviction"),
            "score": rec.get("score"),
            "thesis": _shorten(rec.get("thesis") or "", 360),
            "evidence": _shorten(rec.get("evidence") or "", 360),
            "risks": rec.get("risks") or [],
            "url": rec.get("source_url") or "",
            "price_check": (price_checks or {}).get(rec_key),
        })
    rows.sort(key=lambda r: (r.get("date") or "", r.get("score") or 0), reverse=True)
    return rows


def _report_payload(sig: dict[str, Any],
                    items: list[ContentItem],
                    *,
                    include_backtest: bool = True) -> dict[str, Any]:
    price_checks: dict[str, dict[str, Any]] = {}
    if include_backtest:
        try:
            from .backtest import price_moves_for_recommendations
            price_checks = price_moves_for_recommendations(list(sig.get("recommendations") or []))
        except Exception as exc:
            price_checks = {"__error__": {"status": f"backtest_error: {exc}"}}
    rec_rows = _recommendations_by_date(sig, price_checks)
    creator_accuracy: dict[str, Any] = {}
    if include_backtest:
        try:
            from .backtest import summarize_creator_accuracy
            creator_accuracy = summarize_creator_accuracy(rec_rows)
        except Exception as exc:
            creator_accuracy = {"error": f"creator_accuracy_error: {exc}"}
    return {
        "generated_at_jst": _now_jst().isoformat(timespec="seconds"),
        "lookback_hours": sig.get("lookback_hours", DEFAULT_LOOKBACK_HOURS),
        "source_items_count": sig.get("source_items_count", 0),
        "recommendation_count": sig.get("recommendation_count", 0),
        "extraction_status": sig.get("extraction_status", ""),
        "opend_checked": sig.get("opend_checked", False),
        "tickers": (sig.get("tickers") or [])[:18],
        "recommendations_by_date": rec_rows,
        "creator_sources": _creator_source_index(items),
        "creator_accuracy": creator_accuracy,
        "price_check_note": "baseline is prediction-day close; non-trading days use the latest close before that day; 1d/3d/5d/20d/60d are actual trading-day bars after baseline; sell/avoid succeed when price falls",
        "price_check_errors": price_checks.get("__error__"),
    }


def _build_claude_prompt(payload: dict[str, Any]) -> str:
    packed = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""你是一个谨慎的日股社交媒体研究助理。请根据下面 JSON，写一份中文 Markdown 周报。

要求：
- 只使用 JSON 中的信息，不要补充外部事实，不要编造价格或财务数据。
- 输出 Markdown 正文，不要代码块。
- 全文说明必须使用中文。可以保留日股公司名/标的名的日语原名，但不要直接复制日语字幕句子；日语证据需要翻译或概括成中文。
- 结构必须包含：
  # 日股博主近一周总结
  ## 核心结论
  ## 按日期整理
  ## 博主观点摘要
  ## 重点关注标的
  ## 简单回测
  ## 回避与风险
  ## 数据说明
- “按日期整理”要按日期列出：某某博主在某日分析了什么股票、方向、理由。
- “重点关注标的”优先放多次提及、跨博主提及、分数高的股票。
- “简单回测”要解释 1d/3d/5d/20d/60d 的价格矩阵；这些周期全部指实际交易日，非交易日不计入；sell/avoid 需要按股价下跌为命中，上涨为失败。
- 对字幕识别错字，使用中文意译和概括，不要把错误字幕原样当事实；价格和命中统计以 JSON 数值为准。
- 对 `creator_accuracy` 和每条 `price_check.horizons` 做中文统计解释；不要自行重新计算涨跌幅，只解读 JSON 中已经算好的结果。
- 不要生成完整的“观点明细表”和“博主准确率总表”；这些表格和图表会由脚本在正文后自动追加。
- 如果观点是长期/中线，短期 1d/3d/5d 交易日不命中不要直接判失败；如果没有明确期限，只看 1d/3d/5d 交易日，至少两个周期命中才算综合成功。
- 明确区分 buy/watch/avoid/sell，不要把 watch 写成强买入。
- 语言简洁，适合给交易总框架或人工复核使用。

JSON:
{packed}
"""


def _call_claude_summary(payload: dict[str, Any], timeout: int = 420) -> tuple[str | None, str]:
    try:
        from ai_prompt import query_ai_cli
    except Exception as exc:
        return None, f"claude_import_error: {exc}"
    prompt = _build_claude_prompt(payload)
    out, status, _, _ = query_ai_cli(prompt, timeout=timeout)
    if out:
        return _clean_llm_markdown(out), status
    return None, status


def _pct_text(value: Any) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value):+.2f}%"
    except Exception:
        return "-"


def _outcome_cn(value: str) -> str:
    return {
        "success": "成功",
        "pending": "待定",
        "fail": "失败",
    }.get(value or "", "待定")


def _status_mark(value: str) -> str:
    return {
        "success": "成功",
        "pending": "待定",
        "fail": "失败",
    }.get(value or "", "待定")


def _side_cn(value: str) -> str:
    return {
        "buy": "看多/买入",
        "watch": "观察偏多",
        "sell": "看空/卖出",
        "avoid": "回避/看跌",
    }.get((value or "").strip().lower(), value or "分析")


def _scope_cn(value: str) -> str:
    return {
        "short": "短期",
        "long": "长期/中线",
        "unspecified": "未给出期限",
    }.get((value or "").strip().lower(), value or "未给出期限")


def _horizon_cell(counts: dict[str, Any]) -> str:
    success = int(counts.get("success") or 0)
    fail = int(counts.get("fail") or 0)
    pending = int(counts.get("pending") or 0)
    total = int(counts.get("total") or 0)
    judged = int(counts.get("judged") or success + fail)
    if judged:
        acc = counts.get("accuracy_pct")
        return f"{success}/{judged} 命中({acc:.1f}%)，待{pending}，总{total}"
    return f"0/0，待{pending}，总{total}"


def _opinion_horizon_text(chk: dict[str, Any]) -> str:
    parts = []
    for key in ("1d", "3d", "5d", "20d", "60d"):
        row = (chk.get("horizons") or {}).get(key) or {}
        outcome = row.get("outcome", "pending")
        pct = _pct_text(row.get("change_pct"))
        if outcome == "pending":
            parts.append(f"{key}:待定")
        else:
            parts.append(f"{key}:{pct}/{_outcome_cn(outcome)}")
    return "; ".join(parts)


def _configure_chart_fonts() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for path in (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\NotoSansJP-VF.ttf"),
        Path(r"C:\Windows\Fonts\meiryo.ttc"),
    ):
        if path.exists():
            try:
                font_manager.fontManager.addfont(str(path))
                prop = font_manager.FontProperties(fname=str(path))
                plt.rcParams["font.family"] = prop.get_name()
                break
            except Exception:
                continue
    plt.rcParams["axes.unicode_minus"] = False


def _create_backtest_charts(payload: dict[str, Any], report_dir: Path, stem: str) -> dict[str, str]:
    try:
        _configure_chart_fonts()
        import matplotlib.pyplot as plt
    except Exception as exc:
        return {"error": f"chart_error: {exc}"}

    chart_dir = report_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    recs = payload.get("recommendations_by_date") or []
    chart_paths: dict[str, str] = {}

    rows = []
    for rec in recs:
        chk = rec.get("price_check") or {}
        h1 = (chk.get("horizons") or {}).get("1d") or {}
        if h1.get("status") != "ok":
            continue
        raw_pct = float(h1.get("change_pct") or 0.0)
        direction = chk.get("direction") or "up"
        adjusted_pct = -raw_pct if direction == "down" else raw_pct
        code = str(rec.get("code") or "").replace("JP.", "")
        name = _shorten(rec.get("name") or "", 16)
        label = f"{rec.get('creator')} {code} {name} {rec.get('side') or ''}"
        rows.append({
            "label": label,
            "pct": adjusted_pct,
            "raw_pct": raw_pct,
            "outcome": h1.get("outcome") or "pending",
            "side": rec.get("side") or "",
        })
    if rows:
        rows = sorted(rows, key=lambda r: r["pct"])[:30]
        fig_h = max(4.2, 0.3 * len(rows) + 1.4)
        fig, ax = plt.subplots(figsize=(10, fig_h), dpi=150)
        colors = ["#2563eb" if r["outcome"] == "success" else "#9ca3af" for r in rows]
        ax.barh([r["label"] for r in rows], [r["pct"] for r in rows], color=colors)
        ax.axvline(0, color="#333333", linewidth=0.8)
        ax.set_title("1 个交易日方向调整后表现（右侧=预测方向正确，左侧=方向错误）")
        ax.set_xlabel("方向调整后涨跌幅 %")
        ax.grid(axis="x", color="#D5DCE4", linewidth=0.6)
        fig.tight_layout()
        path = chart_dir / f"{stem}_1d_returns.png"
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        chart_paths["one_day_returns"] = str(path)

    acc = payload.get("creator_accuracy") or {}
    creators = acc.get("creators") or []
    if creators:
        horizon_keys = ["1d", "3d", "5d", "20d", "60d"]
        labels = [c.get("creator") or "unknown" for c in creators]
        x = list(range(len(labels)))
        width = 0.14
        fig, ax = plt.subplots(figsize=(10, 4.8), dpi=150)
        palette = ["#1d4ed8", "#60a5fa", "#93c5fd", "#7e22ce", "#a78bfa"]
        for i, key in enumerate(horizon_keys):
            values = []
            for creator in creators:
                counts = ((creator.get("horizons") or {}).get(key) or {})
                values.append(float(counts.get("accuracy_pct") or 0.0))
            offset = (i - 2) * width
            ax.bar([v + offset for v in x], values, width=width, label=key, color=palette[i])
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=15, ha="right")
        ax.set_ylim(0, 105)
        ax.set_ylabel("已判定命中率 %")
        ax.set_title("博主分交易周期命中率（待定样本不计入命中率）")
        ax.legend(ncol=5, fontsize=8)
        ax.grid(axis="y", color="#D5DCE4", linewidth=0.6)
        fig.tight_layout()
        path = chart_dir / f"{stem}_creator_accuracy.png"
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        chart_paths["creator_accuracy"] = str(path)

    return chart_paths


def _append_backtest_sections(markdown: str, payload: dict[str, Any], chart_paths: dict[str, str]) -> str:
    lines: list[str] = []
    if chart_paths and not chart_paths.get("error"):
        lines.append("")
        lines.append("## 回测图表")
        lines.append("")
        lines.append("第一张图使用“方向调整后表现”：buy/watch 取原始涨跌幅，sell/avoid 取反向涨跌幅。因此柱子在右侧表示预测方向正确，左侧表示方向错误；蓝色为已命中，灰色为未命中或待定。")
        if chart_paths.get("one_day_returns"):
            rel = Path(chart_paths["one_day_returns"]).name
            lines.append("")
            lines.append(f'<img src="charts/{rel}" alt="1个交易日价格变化柱状图" width="900">')
        if chart_paths.get("creator_accuracy"):
            rel = Path(chart_paths["creator_accuracy"]).name
            lines.append("")
            lines.append(f'<img src="charts/{rel}" alt="博主分交易周期命中率柱状图" width="900">')
    elif chart_paths.get("error"):
        lines.append("")
        lines.append("## 回测图表")
        lines.append(f"- 图表生成失败：{chart_paths.get('error')}")

    recs = payload.get("recommendations_by_date") or []
    if recs:
        lines.append("")
        lines.append("## 观点明细")
        lines.append("")
        lines.append("sell/avoid 按股价下跌为命中；buy/watch 按股价上涨为命中。1d/3d/5d/20d/60d 均指实际交易日，非交易日不计入。长期/中线观点优先等待 20d/60d，短期或未给出期限的观点按 1d/3d/5d，至少两个交易周期命中才算综合成功。")
        lines.append("")
        lines.append("| 日期 | 博主 | 标的 | 措辞/期限 | 周期结果 | 综合 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for rec in recs[:40]:
            chk = rec.get("price_check") or {}
            opinion_status = (chk.get("opinion_status") or {}).get("status", "pending")
            wording = f"{_side_cn(rec.get('side') or '')} / {_scope_cn(chk.get('horizon_scope') or '')}"
            ticker_label = f"{rec.get('code')} {rec.get('name')}"
            lines.append(
                f"| {_table_cell(rec.get('date'), 20)} | "
                f"{_table_cell(rec.get('creator'), 32)} | "
                f"{_table_cell(ticker_label, 70)} | "
                f"{_table_cell(wording, 110)} | "
                f"{_table_cell(_opinion_horizon_text(chk), 180)} | "
                f"{_status_mark(opinion_status)} |"
            )

    acc = payload.get("creator_accuracy") or {}
    creators = acc.get("creators") or []
    if creators:
        lines.append("")
        lines.append("## 博主准确率总表")
        lines.append("")
        lines.append("表内 `n/m` 表示成功数/已判定数；待定样本另列，不从分母里消失。所有周期都是实际交易日，综合列按观点期限规则累计。")
        lines.append("")
        lines.append("| 博主 | 观点数 | 1d | 3d | 5d | 20d | 60d | 综合成功/待定/失败 |")
        lines.append("| --- | ---: | --- | --- | --- | --- | --- | --- |")
        for creator in creators:
            horizons = creator.get("horizons") or {}
            overall = creator.get("overall") or {}
            overall_txt = (
                f"{overall.get('success', 0)}/"
                f"{overall.get('pending', 0)}/"
                f"{overall.get('fail', 0)}，累计成功 {overall.get('success', 0)}/{overall.get('total', 0)}"
            )
            lines.append(
                f"| {_table_cell(creator.get('creator'), 40)} | "
                f"{creator.get('total_opinions', 0)} | "
                f"{_table_cell(_horizon_cell(horizons.get('1d') or {}), 80)} | "
                f"{_table_cell(_horizon_cell(horizons.get('3d') or {}), 80)} | "
                f"{_table_cell(_horizon_cell(horizons.get('5d') or {}), 80)} | "
                f"{_table_cell(_horizon_cell(horizons.get('20d') or {}), 80)} | "
                f"{_table_cell(_horizon_cell(horizons.get('60d') or {}), 80)} | "
                f"{_table_cell(overall_txt, 80)} |"
            )
    if not lines:
        return markdown
    return markdown.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n"


def _fallback_markdown(payload: dict[str, Any]) -> str:
    generated = payload.get("generated_at_jst", "")
    lookback = payload.get("lookback_hours", DEFAULT_LOOKBACK_HOURS)
    tickers = payload.get("tickers") or []
    recs = payload.get("recommendations_by_date") or []

    lines: list[str] = []
    lines.append("# 日股博主近一周总结")
    lines.append("")
    lines.append(f"- 生成时间: {generated} JST")
    lines.append(f"- 统计窗口: 最近 {lookback} 小时")
    lines.append(f"- 来源条目: {payload.get('source_items_count', 0)}")
    lines.append(f"- 识别到的荐股/分析记录: {payload.get('recommendation_count', 0)}")
    lines.append(f"- 抽取状态: {payload.get('extraction_status', '')}")
    lines.append(f"- OpenD 校验: {'已启用' if payload.get('opend_checked') else '未启用'}")
    lines.append("")
    lines.append("## 核心结论")
    if tickers:
        top = tickers[:5]
        themes = "、".join(f"{r.get('moomoo_code')} {r.get('company_name')}" for r in top)
        lines.append(f"近一周信号主要集中在 {themes}。排序依据是博主提及、方向、置信度与聚合分数。")
        avoid = [r for r in tickers if str(r.get("direction")) == "bearish" or int(r.get("aggregate_score") or 0) < 0]
        if avoid:
            names = "、".join(f"{r.get('moomoo_code')} {r.get('company_name')}" for r in avoid[:4])
            lines.append(f"需要谨慎或回避的标的包括 {names}。")
    else:
        lines.append("本窗口内没有识别到可行动的日股荐股信号。")
    lines.append("")

    lines.append("## 按日期整理")
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in recs:
        by_day[rec.get("date") or "unknown-date"].append(rec)
    for day in sorted(by_day.keys(), reverse=True):
        lines.append(f"### {day}")
        by_creator: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in by_day[day]:
            by_creator[rec.get("creator") or "unknown"].append(rec)
        for creator in sorted(by_creator):
            lines.append(f"- **{creator}**")
            for rec in sorted(by_creator[creator], key=lambda r: abs(int(r.get("score") or 0)), reverse=True):
                reason = rec.get("evidence") or rec.get("thesis") or ""
                side = rec.get("side")
                if side == "buy":
                    action_cn = "偏多/买入"
                elif side == "watch":
                    action_cn = "观察"
                elif side == "avoid":
                    action_cn = "回避"
                elif side == "sell":
                    action_cn = "偏空/卖出"
                else:
                    action_cn = "分析"
                lines.append(
                    f"  - {rec.get('code')} {rec.get('name')} - {rec.get('side')} "
                    f"(score {rec.get('score')}, conviction {rec.get('conviction')}): "
                    f"系统从该博主内容中识别为{action_cn}信号，需结合原视频语境复核。"
                )
        lines.append("")

    lines.append("## 博主观点摘要")
    by_creator2: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in recs:
        by_creator2[rec.get("creator") or "unknown"].append(rec)
    if by_creator2:
        for creator in sorted(by_creator2):
            rows = by_creator2[creator]
            names = "、".join(
                f"{r.get('code')} {r.get('name')}({r.get('side')})" for r in rows[:8]
            )
            lines.append(f"- **{creator}**: {names}")
    else:
        lines.append("- 没有足够的荐股记录可汇总。")
    lines.append("")

    lines.append("## 重点关注标的")
    if tickers:
        lines.append("| 标的 | 方向 | 分数 | 提及次数 | 博主 | 主要理由 |")
        lines.append("| --- | --- | ---: | ---: | --- | --- |")
        for row in tickers[:12]:
            creators = ", ".join(row.get("creators") or [])
            ticker_label = f"{row.get('moomoo_code')} {row.get('company_name')}"
            reason_cn = (
                f"聚合方向为 {row.get('direction')}，提及 {row.get('mentions')} 次，"
                f"主要来源: {creators or row.get('top_creator') or 'unknown'}。"
            )
            lines.append(
                f"| {_table_cell(ticker_label, 80)} | "
                f"{_table_cell(row.get('direction'), 40)} | {row.get('aggregate_score')} | "
                f"{row.get('mentions')} | {_table_cell(creators, 120)} | {_table_cell(reason_cn, 180)} |"
            )
    else:
        lines.append("无。")
    lines.append("")

    lines.append("## 简单回测")
    lines.append("基准价使用博主发布日期当天收盘价；如果当天休市，则使用该日期之前最近一个交易日收盘价。最新价优先使用 yfinance 当前价，缺失时使用最新历史收盘价。")
    checked = [r for r in recs if r.get("price_check")]
    if checked:
        lines.append("")
        lines.append("| 标的 | 博主/日期 | 方向 | 基准收盘 | 最新价格 | 变动 | 状态 |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: | --- |")
        for rec in checked[:18]:
            chk = rec.get("price_check") or {}
            if chk.get("status") == "ok":
                baseline = f"{chk.get('baseline_close')} ({chk.get('baseline_date')})"
                latest = f"{chk.get('latest_price')} ({chk.get('latest_date')})"
                change = f"{chk.get('change'):+.2f} / {chk.get('change_pct'):+.2f}%"
            else:
                baseline = "-"
                latest = "-"
                change = "-"
            ticker_label = f"{rec.get('code')} {rec.get('name')}"
            creator_day = f"{rec.get('creator')} / {rec.get('date')}"
            lines.append(
                f"| {_table_cell(ticker_label, 80)} | "
                f"{_table_cell(creator_day, 80)} | "
                f"{_table_cell(rec.get('side'), 40)} | {baseline} | {latest} | {change} | "
                f"{_table_cell(chk.get('status') or 'missing', 80)} |"
            )
    else:
        err = payload.get("price_check_errors") or {}
        status = err.get("status") if isinstance(err, dict) else ""
        lines.append(f"- 未取得可用的价格回测数据。{status}")
    lines.append("")

    lines.append("## 回避与风险")
    risk_rows = [r for r in tickers if int(r.get("aggregate_score") or 0) < 0 or r.get("direction") == "bearish"]
    if risk_rows:
        for row in risk_rows[:8]:
            lines.append(
                f"- {row.get('moomoo_code')} {row.get('company_name')}: "
                f"聚合方向 {row.get('direction')}，分数 {row.get('aggregate_score')}，"
                f"属于本周内容中需要谨慎处理的标的。"
            )
    else:
        lines.append("- 未识别到明显的 sell/avoid 聚合标的；仍需人工复核原视频语境。")
    lines.append("")

    lines.append("## 数据说明")
    lines.append("- 本报告来自最近一周已下载的 YouTube 字幕、Whisper 转写或手动 inbox 内容。")
    lines.append("- 结论是内容分析信号，不构成投资建议。")
    lines.append("- 若 OpenD 未启用或查询失败，报告不包含实时行情校验。")
    return "\n".join(lines).strip() + "\n"


def _inline_markup(text: str) -> str:
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def _register_pdf_fonts() -> tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular_name = "JPReportRegular"
    bold_name = "JPReportBold"
    font_candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\NotoSansJP-VF.ttf"),
        Path(r"C:\Windows\Fonts\meiryo.ttc"),
        Path(r"C:\Windows\Fonts\msgothic.ttc"),
    ]
    bold_candidates = [
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
        Path(r"C:\Windows\Fonts\meiryob.ttc"),
        Path(r"C:\Windows\Fonts\YuGothB.ttc"),
        Path(r"C:\Windows\Fonts\NotoSansJP-VF.ttf"),
    ]

    def register(name: str, paths: list[Path]) -> str:
        try:
            pdfmetrics.getFont(name)
            return name
        except Exception:
            pass
        last_error = None
        for path in paths:
            if not path.exists():
                continue
            try:
                pdfmetrics.registerFont(TTFont(name, str(path)))
                return name
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"could not register CJK PDF font: {last_error}")

    regular = register(regular_name, font_candidates)
    bold = register(bold_name, bold_candidates)
    return regular, bold


def markdown_to_pdf(markdown: str, pdf_path: Path, *, title: str = "日股博主近一周总结") -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except Exception as exc:
        raise RuntimeError("reportlab is required to create PDF reports") from exc

    regular, bold = _register_pdf_fonts()
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "CJKBase",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=9.2,
        leading=13,
        spaceAfter=5,
    )
    h1 = ParagraphStyle(
        "CJKH1",
        parent=base,
        fontName=bold,
        fontSize=18,
        leading=24,
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    h2 = ParagraphStyle(
        "CJKH2",
        parent=base,
        fontName=bold,
        fontSize=13,
        leading=18,
        spaceBefore=8,
        spaceAfter=6,
        textColor=colors.HexColor("#17324D"),
    )
    h3 = ParagraphStyle(
        "CJKH3",
        parent=base,
        fontName=bold,
        fontSize=10.5,
        leading=15,
        spaceBefore=5,
        spaceAfter=4,
    )
    bullet = ParagraphStyle(
        "CJKBullet",
        parent=base,
        leftIndent=9,
        firstLineIndent=0,
        bulletIndent=0,
        spaceAfter=3,
    )
    small = ParagraphStyle(
        "CJKSmall",
        parent=base,
        fontSize=7.4,
        leading=10,
        spaceAfter=2,
    )

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=13 * mm,
        leftMargin=13 * mm,
        topMargin=12 * mm,
        bottomMargin=13 * mm,
        title=title,
        author="jp_social_reco",
    )

    story = []
    lines = markdown.splitlines()
    i = 0
    width = A4[0] - 26 * mm
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            story.append(Spacer(1, 3))
            i += 1
            continue
        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line.strip())
        html_image_match = re.fullmatch(r"<img\s+[^>]*src=[\"']([^\"']+)[\"'][^>]*>", line.strip(), flags=re.I)
        if image_match or html_image_match:
            src = image_match.group(2).strip() if image_match else html_image_match.group(1).strip()
            if src.lower().startswith("file:///"):
                src = src[8:]
            image_path = Path(src)
            if not image_path.is_absolute():
                image_path = pdf_path.parent / image_path
            if image_path.exists():
                try:
                    img = Image(str(image_path))
                    max_w = width
                    max_h = 115 * mm
                    scale = min(max_w / img.imageWidth, max_h / img.imageHeight, 1.0)
                    img.drawWidth = img.imageWidth * scale
                    img.drawHeight = img.imageHeight * scale
                    story.append(img)
                    story.append(Spacer(1, 6))
                except Exception:
                    story.append(Paragraph(_inline_markup(f"[image unavailable: {image_path}]"), base))
            i += 1
            continue
        if line.startswith("|") and "|" in line[1:]:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            rows: list[list[str]] = []
            for raw in table_lines:
                cells = [c.strip() for c in raw.strip("|").split("|")]
                if cells and all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells):
                    continue
                rows.append(cells)
            if rows:
                col_count = max(len(r) for r in rows)
                for row in rows:
                    row.extend([""] * (col_count - len(row)))
                col_widths = [width / col_count] * col_count
                data = [[Paragraph(_inline_markup(c), small) for c in row] for row in rows]
                tbl = Table(data, colWidths=col_widths, repeatRows=1)
                tbl.setStyle(TableStyle([
                    ("FONTNAME", (0, 0), (-1, -1), regular),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EEF4")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#172033")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C7D0DA")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                story.append(tbl)
                story.append(Spacer(1, 5))
            continue
        if line.startswith("# "):
            story.append(Paragraph(_inline_markup(line[2:].strip()), h1))
        elif line.startswith("## "):
            story.append(Paragraph(_inline_markup(line[3:].strip()), h2))
        elif line.startswith("### "):
            story.append(Paragraph(_inline_markup(line[4:].strip()), h3))
        elif line.lstrip().startswith("- "):
            indent = len(line) - len(line.lstrip())
            style = bullet if indent <= 1 else ParagraphStyle(
                "CJKBulletNested",
                parent=bullet,
                leftIndent=18,
                bulletIndent=9,
            )
            story.append(Paragraph(_inline_markup(line.lstrip()[2:].strip()), style, bulletText="-"))
        else:
            story.append(Paragraph(_inline_markup(line), base))
        i += 1

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(regular, 7)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawRightString(A4[0] - 13 * mm, 7 * mm, f"{document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return pdf_path


def write_weekly_report(*,
                        sig: dict[str, Any],
                        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
                        max_items: int = 80,
                        output_dir: str | Path | None = None,
                        use_llm: bool = True,
                        llm_timeout: int = 420,
                        include_backtest: bool = True,
                        write_pdf: bool = True,
                        write_md: bool = True) -> dict[str, Any]:
    """Create Markdown/PDF weekly report from the latest JP social signal."""
    ensure_dirs()
    report_dir = Path(output_dir) if output_dir else REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    items = collect_inbox_items(lookback_hours=lookback_hours, max_items=max_items)
    payload = _report_payload(sig, items, include_backtest=include_backtest)
    report_day = _now_jst().date().isoformat()
    stem = f"jp_social_weekly_{report_day}"
    chart_paths: dict[str, str] = {}
    if include_backtest:
        chart_paths = _create_backtest_charts(payload, report_dir, stem)
        payload["chart_paths"] = chart_paths

    llm_status = "disabled"
    markdown = ""
    if use_llm:
        markdown, llm_status = _call_claude_summary(payload, timeout=llm_timeout)
    if not markdown:
        markdown = _fallback_markdown(payload)
        if use_llm:
            llm_status = f"{llm_status}; fallback_template"
    if include_backtest:
        markdown = _append_backtest_sections(markdown, payload, chart_paths)

    md_path = report_dir / f"{stem}.md"
    pdf_path = report_dir / f"{stem}.pdf"
    payload_path = report_dir / f"{stem}.payload.json"

    if write_md:
        md_path.write_text(markdown, encoding="utf-8")
    else:
        md_path = Path("")
    if write_pdf:
        markdown_to_pdf(markdown, pdf_path)
    else:
        pdf_path = Path("")
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "generated_at_jst": payload["generated_at_jst"],
        "markdown_path": str(md_path) if write_md else "",
        "pdf_path": str(pdf_path) if write_pdf else "",
        "payload_path": str(payload_path),
        "report_llm_status": llm_status,
        "source_items_count": payload["source_items_count"],
        "recommendation_count": payload["recommendation_count"],
        "chart_paths": chart_paths,
    }
