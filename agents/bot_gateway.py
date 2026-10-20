"""机器人只读接入层 (2026-10-19): 把模拟仓的所有操作整理成统一的事件流, 并列出系统全部接口.

只读: 本模块不下单、不审批、不改任何状态 (唯一写入的是自己的推送游标 signals/bot_feed_state.json).

  HTTP (挂在看板 webui 上, 127.0.0.1:8080):
    GET /api/bot                     接口目录 (本文件 ENDPOINTS, 机器人可自动发现)
    GET /api/bot/summary             一次拿全: 账户净值/回撤、持仓、今日操作、系统健康、AI 复核通道、待批提案
    GET /api/bot/events?since=ISO&kinds=order,execution&limit=200
                                     统一操作流 (按时间升序); 返回 next_since, 下次带上它只取新的
    GET /api/bot/brief?slot=morning  cn_brief 最新汇报 (morning/close/evening/now)
  命令行:  python bot_gateway.py events --since 2026-10-19T00:00:00+09:00
           python bot_gateway.py summary | index | tick
  推送:    看门狗任务 bot_feed_tick 每 ~5 分钟把新事件 POST 到 BOT_WEBHOOK_URL (secrets.local.json)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIGNALS = HERE / "signals"
JST = timezone(timedelta(hours=9))
STATE = SIGNALS / "bot_feed_state.json"


def _clip(s, n=300):
    s = str(s if s is not None else "")
    return s if len(s) <= n else s[:n] + "…"


def _ts(v, naive_tz=timezone.utc) -> datetime | None:
    if not v:
        return None
    import re
    sv = str(v).replace("Z", "+00:00")
    sv = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), sv, count=1)   # 小数秒补齐 6 位 (旧版 Python 只认 3/6 位)
    try:
        t = datetime.fromisoformat(sv)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=naive_tz)


# ── 事件来源: (kind, 相对路径, 时间字段, 无时区时按哪个时区, 摘要函数, 保留字段) ──────────
def _s_order(d):
    dr = " [试运行]" if d.get("dry_run") else ""
    win = f"（{d.get('window')}）" if d.get("window") else ""
    return f"下单 {d.get('side')} {d.get('ticker')} {d.get('qty')} 股 @ {d.get('price')} {d.get('tag', '')}{win}{dr}"


def _s_exec(d):
    ev = {"submitted": "提交", "filled": "成交", "cancelled": "撤单"}.get(d.get("event"), d.get("event"))
    px = d.get("average_fill_price") or d.get("order_price")
    qty = f"{d.get('dealt_qty')}/{d.get('requested_qty')}" if d.get("dealt_qty") is not None else d.get("requested_qty")
    return f"{ev} {d.get('side')} {d.get('ticker')} {qty} 股 @ {px}（订单 {d.get('order_id')}）"


def _s_alert(d):
    return f"[{d.get('level')}] " + _clip(str(d.get("msg", "")).replace("\n", " "), 200)


def _s_rebal(d):
    return (f"再平衡计划（{d.get('window')}{'，试运行' if d.get('dry_run') else ''}）：净值 {d.get('nav')}，现金 {d.get('cash')}，"
            f"回撤 {d.get('drawdown_pct')}%，regime {d.get('regime')}，{len(d.get('orders') or [])} 笔订单")


def _s_evo(d):
    if d.get("event") == "propose":
        return f"进化提案：新增 {d.get('added')}（{d.get('provider')}）"
    return f"进化 {d.get('id')} → {d.get('status') or d.get('event')}（{d.get('by', '')}）{_clip(d.get('note', ''), 120)}"


def _s_pm(d):
    return f"卖出复盘 {d.get('ticker')} {d.get('qty')} 股 @ {d.get('price')}：{d.get('nature', '')} {_clip(d.get('reason', ''), 120)}"


def _s_conflict(d):
    return f"规则与 AI 分歧 {d.get('ticker')}：规则 {d.get('rule_action')} / AI {d.get('ai_action')}；{_clip(d.get('notes', ''), 120)}"


def _s_guard(d):
    return f"止损线拦下买入 {d.get('ticker')} @ {d.get('price')}：线 {d.get('line')}（{d.get('kind')}）{_clip(d.get('tag', ''), 60)}"


def _s_brief(d):
    return f"汇报推送 {d.get('kind')}：{d.get('title')}（已发 {d.get('sent')}）"


SOURCES = [
    ("order", "trade_log.jsonl", "ts", timezone.utc, _s_order, None),
    ("execution", "execution_ledger.jsonl", "ts", timezone.utc, _s_exec, ("plan",)),
    ("alert", "critical_alerts.jsonl", "ts", timezone.utc, _s_alert, None),
    ("rebalance_plan", "rebalance_plan.jsonl", "ts", JST, _s_rebal, None),   # 实测: 无时区, 为日本时间
    ("evolution", "evolution/log.jsonl", "ts", timezone.utc, _s_evo, None),
    ("postmortem", "postmortem/archive.jsonl", "ts", timezone.utc, _s_pm, ("material",)),
    ("ai_conflict", "claude_conflict.jsonl", "ts", timezone.utc, _s_conflict, None),
    ("brief", "cn_brief/push_log.jsonl", "ts", JST, _s_brief, None),
    ("guard_block", "stop_line_blocks.jsonl", "ts", timezone.utc, _s_guard, None),   # 2026-10-19 止损线守卫
]
KINDS = [s[0] for s in SOURCES]


def events(since: str | None = None, kinds: list[str] | None = None, limit: int = 200,
           until: str | None = None) -> dict:
    t0 = _ts(since, JST).replace(microsecond=0) if since and _ts(since, JST) else None
    t1 = _ts(until, JST).replace(microsecond=0) if until and _ts(until, JST) else None
    want = set(kinds or KINDS)
    out, errors = [], []
    # 2026-10-19: 成交/撤单记录的 ts 是对账时刻 (可能晚几小时). 有券商时间用券商时间, 否则用下单时间.
    submitted_at = {}
    try:
        for line in (SIGNALS / "execution_ledger.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("event") == "submitted" and d.get("order_id"):
                submitted_at.setdefault(str(d["order_id"]), d.get("ts"))
    except OSError:
        pass
    for kind, rel, key, ntz, summ, drop in SOURCES:
        if kind not in want:
            continue
        p = SIGNALS / rel
        if not p.exists():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            errors.append(f"{rel}: {e}")
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_ts, time_source = d.get(key), None
            if kind == "execution" and d.get("event") != "submitted":
                if d.get("broker_time"):
                    raw_ts, time_source = d["broker_time"], "broker"
                elif submitted_at.get(str(d.get("order_id"))):
                    raw_ts, time_source = submitted_at[str(d.get("order_id"))], "submitted"
                else:
                    time_source = "recorded"
            t = _ts(raw_ts, ntz)
            if t is not None:
                t = t.replace(microsecond=0)        # 输出精确到秒, 比较也必须按秒, 否则分页会重复
            if t is None or (t0 and t <= t0) or (t1 and t > t1):
                continue
            data = {k: v for k, v in d.items() if not drop or k not in drop}
            ev = {"id": f"{kind}:{hashlib.sha1(line.encode('utf-8')).hexdigest()[:16]}", "kind": kind,
                  "ts": t.astimezone(JST).isoformat(timespec="seconds"), "summary": summ(d), "data": data}
            if time_source:
                ev["time_source"] = time_source     # broker=券商成交时间 / submitted=按下单时间 / recorded=对账时刻
            out.append(ev)
    _rank = {"submitted": 0}           # 同一秒内: 提交排在成交/撤单前面
    out.sort(key=lambda e: (e["ts"], e["kind"], _rank.get(e["data"].get("event"), 1), e["id"]))
    truncated = len(out) > limit
    if truncated:
        # 截断点上同一秒的事件要么全给要么全不给, 否则下次 since=next_since (严格大于) 会漏掉
        cut = out[limit - 1]["ts"]
        head = [e for e in out[:limit] if e["ts"] < cut]
        out = head if head else [e for e in out if e["ts"] == cut]
    return {"events": out, "count": len(out), "truncated": truncated,
            "next_since": out[-1]["ts"] if out else (since or None), "kinds": sorted(want), "errors": errors}


def _read_json(rel: str):
    try:
        return json.loads((SIGNALS / rel).read_text(encoding="utf-8"))
    except Exception:
        return None


def summary(positions: dict | None = None, nav: dict | None = None, health: dict | None = None) -> dict:
    """webui 传入它自己的 api_positions / api_nav / api_health 结果; 命令行调用时这三项为空."""
    now = datetime.now(JST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ev = events(since=today.isoformat(), limit=1000)["events"]
    counts = {}
    for e in ev:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    last_nav = None
    try:
        last_nav = json.loads((SIGNALS / "nav_history.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    except Exception:
        pass
    proposals = _read_json("evolution/proposals.json") or []
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "account": {"mode": "SIMULATE (moomoo 模拟盘)", "nav_latest": last_nav, "nav_api": nav},
        "positions": positions,
        "today": {"since": today.isoformat(), "counts": counts,
                  "operations": [e["summary"] for e in ev if e["kind"] in ("order", "execution")][-30:]},
        "health": health,
        "ai_gate_health": _read_json("ai_gate_health.json"),
        "evolution_pending": [{"id": p.get("id"), "status": p.get("status"), "title": p.get("title")}
                              for p in proposals if p.get("status") in ("proposed", "ready_for_review")],
        "latest_alerts": [e["summary"] for e in events(kinds=["alert"], limit=100000)["events"][-5:]],
    }


def brief(slot: str = "morning") -> dict:
    slot = slot if slot in ("morning", "close", "evening", "now") else "morning"
    d = _read_json(f"cn_brief/latest_{slot}.json")
    return d if d else {"slot": slot, "error": "尚未生成", "markdown": ""}


# ── 接口目录 (机器人可读 /api/bot 自动发现) ─────────────────────────────────
R, AI, W = "read", "read_ai_cost", "write_forbidden"
ENDPOINTS = [
    # 机器人专用
    ("/api/bot", R, "接口目录（本表）"),
    ("/api/bot/summary", R, "一次拿全：净值/回撤、持仓、今日操作、系统健康、AI 复核通道、待批提案、最近告警"),
    ("/api/bot/events", R, "统一操作流。参数 since=ISO时间 kinds=逗号分隔(" + ",".join(KINDS) + ") limit=默认200；返回 next_since"),
    ("/api/bot/brief", R, "A股研报/经济数据汇报。参数 slot=morning|close|evening|now"),
    ("/api/bot/radar", R, "公开重大新闻与机会线索缓存；附原文证据/时间/链接。无持仓，无原始Codex报告；GET不调用AI。"),
    ("/api/bot/research", R, "免费公开A股研报题摘与宏观日历；机构/日期/原链独立归因，未读正文。仅缓存，无AI分析/账户/交易。"),
    ("/api/bot/forecasts", R, "事前锁定的公开研报假设与到期前向检验；只读账本，不计算、不下载、不调用AI。"),
    # 看板原有 (只读)
    ("/api/health", R, "系统健康：OpenD、看板、各进程"),
    ("/api/nav", R, "净值历史。参数 days=30"),
    ("/api/equity_curve", R, "净值曲线 vs 基准"),
    ("/api/positions", R, "moomoo 实时持仓 + 入场信息（缓存 60 秒）"),
    ("/api/fills", R, "最近成交（执行台账）"),
    ("/api/trades", R, "交易日志。参数 n=20"),
    ("/api/log", R, "运行日志尾部。参数 n=100"),
    ("/api/postmortem", R, "卖出复盘（5 条理由 + 3 条局限）"),
    ("/api/evolution", R, "自我进化提案列表与状态"),
    ("/api/institutional", R, "机构级风险/绩效指标"),
    ("/api/benchmark", R, "基准对比报告"),
    ("/api/signals", R, "各标的最新信号"),
    ("/api/banners", R, "看板顶部横幅（提醒/告警）"),
    ("/api/hmm", R, "市场状态 (HMM regime)"),
    ("/api/sectors", R, "板块轮动"),
    ("/api/top_picks", R, "选股候选。参数 n=10"),
    ("/api/usd_theme", R, "强美元主线读数"),
    ("/api/thesis_state", R, "交易主线状态。参数 as_of=YYYY-MM-DD"),
    ("/api/thesis_forecast", R, "主线预测。参数 days=45"),
    ("/api/thesis_history", R, "主线历史。参数 days=180"),
    ("/api/events", R, "事件日历（财报/宏观）"),
    ("/api/oil", R, "原油相关指标"),
    ("/api/bond_monitor", R, "债券监控"),
    ("/api/fed_watch", R, "美联储观察"),
    ("/api/policy_toolkit", R, "政策工具跟踪"),
    ("/api/liquidity_history", R, "流动性历史"),
    ("/api/option_walls_chart", R, "期权墙"),
    ("/api/ticker_options", R, "个股期权数据"),
    ("/api/options_flow", R, "期权流"),
    ("/api/fundamentals", R, "基本面。参数 ticker= period=year|quarter（联网取数）"),
    ("/api/capital_flow", R, "资金流。参数 ticker="),
    ("/api/tostnet_hits", R, "日股 ToSTNeT 大宗。参数 ticker= days=10"),
    ("/api/jp_watch", R, "日股观察"),
    ("/api/trump_attribution", R, "特朗普信号归因"),
    ("/api/ai_targets", R, "AI 目标价"),
    ("/api/ai_analysis", R, "AI 主分析（读缓存）"),
    # 会调用 AI (消耗 Claude/Codex 额度) — 仅在用户明确要求时调用
    ("/api/ticker_ai", AI, "个股 AI 实时分析（耗额度）"),
    ("/api/supply_chain", AI, "供应链 AI 分析。参数 ticker=（耗额度）"),
    ("/api/supply_chain_graph", AI, "供应链图。参数 ticker= depth=2（可能耗额度）"),
    ("/api/jp_guidance", AI, "日股业绩指引 AI 提取。参数 ticker=（耗额度）"),
    ("/api/equity_outlook", AI, "个股展望。参数 ticker=（可能耗额度）"),
    ("/api/bond_ai_interpret", AI, "债券 AI 解读（可能耗额度）"),
    ("/api/trump_verify", AI, "特朗普言论核验。参数 text=（耗额度）"),
    # 写操作 — 机器人不得调用
    ("/api/evolution_token", W, "进化审批口令（拿到即可审批，机器人禁止调用）"),
    ("/api/evolution_action", W, "审批/驳回进化提案（机器人禁止调用）"),
]
FILES = {
    "signals/trade_log.jsonl": "下单记录（每行一笔，含决策原因）",
    "signals/execution_ledger.jsonl": "执行台账：提交/成交/撤单，带哈希链防篡改",
    "signals/broker_history_fills.jsonl": "从券商历史导入的成交",
    "signals/nav_history.jsonl": "净值历史",
    "signals/rebalance_plan.jsonl": "再平衡计划（时间为日本时间、无时区标记）",
    "signals/critical_alerts.jsonl": "告警（交易/危机/警告）",
    "signals/postmortem/archive.jsonl": "卖出复盘存档",
    "signals/evolution/proposals.json": "进化提案",
    "signals/ai_gate_health.json": "AI 复核通道健康（Claude/Codex）",
    "signals/ai_calls.jsonl": "所有 AI 调用审计（模型、耗时、状态）",
    "signals/cn_brief/latest_<slot>.json": "A股研报/经济数据汇报",
    "signals/stop_lines.json": "止损线 (未收复前不买回)",
    "signals/stop_line_blocks.jsonl": "被止损线拦下的买单",
    "signals/backtest_verdicts/*.json": "所有预注册回测结论",
}


def index() -> dict:
    return {"name": "fsi-skills 美股模拟盘量化系统 · 机器人接口", "base": "http://127.0.0.1:8080",
            "policy": {"read": "可随时调用", "read_ai_cost": "会调用 AI、消耗额度：仅在用户明确要求时调用",
                       "write_forbidden": "写操作：机器人禁止调用（用户选择只读）"},
            "endpoints": [{"path": p, "class": c, "desc": d} for p, c, d in ENDPOINTS],
            "event_kinds": KINDS, "files": FILES, "files_root": str(HERE),
            "push": "新操作每 ~5 分钟 POST 到 BOT_WEBHOOK_URL；A股汇报同样推到这里（若未单独配置 CN_BRIEF_WEBHOOK_URL）",
            "docs": str(HERE / "BOT_INTEGRATION.md")}


# ── 推送 (看门狗 bot_feed_tick 调用) ───────────────────────────────────────
def _cfg(key: str) -> str:
    try:
        from config import _cfg as c
        return c(key, "") or ""
    except Exception:
        return os.environ.get(key, "")


def tick(post=None) -> dict:
    url = _cfg("BOT_WEBHOOK_URL")
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    now = datetime.now(JST).isoformat(timespec="seconds")
    if not st.get("since"):                       # 第一次: 从现在开始, 不把历史一股脑推过去
        st = {"since": now, "sent": [], "initialized_at": now}
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"initialized": now}
    if not url:
        return {"skipped": "BOT_WEBHOOK_URL 未配置", "since": st["since"]}
    r = events(since=st["since"], limit=50)
    sent = set(st.get("sent", []))
    new = [e for e in r["events"] if e["id"] not in sent]
    if not new:
        return {"new": 0, "since": st["since"]}
    payload = {"source": "fsi-skills", "type": "events", "count": len(new),
               "markdown": "\n".join(f"- {e['ts'][5:16]} {e['summary']}" for e in new), "events": new}
    if post is None:
        import requests
        post = lambda u, p: requests.post(u, json=p, timeout=10).ok      # noqa: E731
    ok = False
    try:
        ok = bool(post(url, payload))
    except Exception as e:      # noqa: BLE001
        return {"error": f"推送失败: {e}", "pending": len(new)}
    if ok:
        st["since"] = r["next_since"]
        st["sent"] = (list(sent) + [e["id"] for e in new])[-500:]
        st["last_push"] = now
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"pushed": len(new) if ok else 0, "ok": ok, "since": st["since"]}


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = argv[1] if len(argv) > 1 else "index"
    args = dict(a.split("=", 1) for a in argv[2:] if "=" in a)
    if cmd == "events":
        res = events(since=args.get("--since") or args.get("since"), kinds=(args.get("kinds") or "").split(",") if args.get("kinds") else None,
                     limit=int(args.get("limit", 200)))
    elif cmd == "summary":
        res = summary()
    elif cmd == "tick":
        res = tick()
    else:
        res = index()
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
