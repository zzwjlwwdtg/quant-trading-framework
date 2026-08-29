"""
policy_toolkit_tracker.py — 美债救援政策工具追踪

抓 Treasury + Fed 官方新闻 → CLI 结构化为 policy_action → 匹配到预定义 toolkit →
计算 30Y 收益率反应 (T+0/T+1d) → 汇总每个工具的最新状态 + 历史事件。

输出 signals/policy_toolkit_latest.json 供 dashboard 消费。

主入口:
  build_policy_toolkit() -> dict   # 全量刷新, 24h TTL
  load_latest() -> dict            # 只读磁盘上的最新聚合

CLI:
  python policy_toolkit_tracker.py         # 全量刷新
  python policy_toolkit_tracker.py --show  # 打印最新聚合
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from config import SIGNALS_DIR
from news_analyzer import _fetch_rss, _extract_json_from_cli_output


CACHE_DIR = Path(SIGNALS_DIR)
LATEST_PATH = CACHE_DIR / "policy_toolkit_latest.json"
HISTORY_PATH = CACHE_DIR / "policy_toolkit_history.jsonl"


# ── 预定义工具箱 ─────────────────────────────────────────────────────────────
# 每个工具的静态描述 + 可行性 + 触发/失效条件, 让 dashboard 即使没有事件也能显示
TOOLKIT = {
    "long_end_buyback": {
        "zh": "长端回购扩容",
        "owner": "Treasury",
        "feasibility": "high",
        "canonical_desc": "定期回购 10-30Y 老券, 压制长端 term premium",
        "invalidation": "结构性发债需求未减 → 通常 T+1d 收益率反弹回来",
    },
    "fx_intervention": {
        "zh": "汇市干预 (支撑日元)",
        "owner": "Treasury/G7",
        "feasibility": "medium",
        "canonical_desc": "联合日本央行买日元, 减少日本抛售美债换美元",
        "invalidation": "只缓解流量, 存量抛压 (JGB 收益率高企) 未变",
    },
    "fima_repo": {
        "zh": "FIMA 回购便利",
        "owner": "Fed",
        "feasibility": "high",
        "canonical_desc": "外国央行以美债抵押向 Fed 借美元, 避免直接抛售美债",
        "invalidation": "只对央行有效, 私人机构抛压不减",
    },
    "qra_wording": {
        "zh": "季度再融资声明措辞",
        "owner": "Treasury",
        "feasibility": "high",
        "canonical_desc": "把 '增加发债' 改成 '变化', 暗示可能减少长债供给",
        "invalidation": "只是预期管理, 未实际减少融资需求",
    },
    "slr_exemption": {
        "zh": "SLR 豁免 (银行持债)",
        "owner": "Fed/OCC/FDIC",
        "feasibility": "medium",
        "canonical_desc": "豁免银行 SLR 里的国债, 让银行可大量吸纳长债",
        "invalidation": "放松金融监管的道德风险 + Fed 内部意见分歧",
    },
    "duration_shortening": {
        "zh": "缩短发债久期",
        "owner": "Treasury",
        "feasibility": "high",
        "canonical_desc": "多发 T-bill 少发长债, 压制长端供给",
        "invalidation": "短端供给爆棚, 靠 MMF/RRP 吸收; RRP 见底就断供",
    },
    "tga_deployment": {
        "zh": "TGA 财政存款回购",
        "owner": "Treasury",
        "feasibility": "medium",
        "canonical_desc": "动用 TGA (~$940B) 回购老券, 一次性释放流动性",
        "invalidation": "一次性弹药, 用完就没了",
    },
    "ycc": {
        "zh": "收益率曲线控制 (YCC)",
        "owner": "Fed",
        "feasibility": "low",
        "canonical_desc": "Fed 承诺买入长债直到收益率降到目标",
        "invalidation": "破坏央行独立性 + USD 崩盘风险 → 核弹级",
    },
    "qe_restart": {
        "zh": "重启 QE / 扩表",
        "owner": "Fed",
        "feasibility": "low",
        "canonical_desc": "Fed 直接买国债扩表, 压制收益率",
        "invalidation": "与抗通胀目标直接冲突, Warsh 明确反对",
    },
    "fed_dovish_signal": {
        "zh": "Fed 官员鸽派信号",
        "owner": "Fed (口头)",
        "feasibility": "high",
        "canonical_desc": "官员讲话暗示降息倾向, 前瞻指引",
        "invalidation": "口头 → 若数据不配合, 反被 hawkish surprise 抹平",
    },
    "fed_hawkish_signal": {
        "zh": "Fed 官员鹰派信号",
        "owner": "Fed (口头)",
        "feasibility": "high",
        "canonical_desc": "官员讲话暗示维持高利率或加息",
        "invalidation": "会推高长端收益率 → 反救市信号",
    },
}


_ACTION_TYPES = {"activated", "expanded", "signaled", "opposed",
                 "announced", "reversed"}


# ── RSS 源 ──────────────────────────────────────────────────────────────────
# 官方 .gov 源大多是行政/银行审批噪音, 真正的政策叙事在通讯社上。
# 用 Google News RSS 定题搜索, 各主题拉最新 ≤ 10 条 → CLI 结构化过滤。
_GNEWS_QUERIES = [
    "Bessent Treasury buyback",
    "Treasury bond market intervention Bessent",
    "Federal Reserve rate cut dovish hawkish",
    "SLR exemption bank Treasury Federal Reserve",
    "yield curve control QE Federal Reserve",
    # 2026: Powell 已被 Warsh 换. 保留 Powell 是为了历史向后兼容 + 有时新闻仍提
    "Warsh Federal Reserve rate hike inflation",
    "Warsh Powell Jackson Hole dovish hawkish",
    "Fed Chair speech rate policy",
    "FOMC minutes rate expectations",
    "TGA Treasury General Account debt buyback",
]

_GNEWS_URL = ("https://news.google.com/rss/search"
              "?q={q}&hl=en-US&gl=US&ceid=US:en")

_RSS_SOURCES = [
    ("https://www.federalreserve.gov/feeds/press_all.xml",
     "Federal Reserve"),
]


def _fetch_all_rss() -> list[dict]:
    import urllib.parse
    items: list[dict] = []
    seen_titles = set()
    # Google News 定题搜索 (主力信源)
    for q in _GNEWS_QUERIES:
        url = _GNEWS_URL.format(q=urllib.parse.quote_plus(q))
        for it in _fetch_rss(url, source=f"GoogleNews: {q}",
                             timeout=10, browser_ua=True):
            title = it.get("title", "")[:120]
            if title in seen_titles:
                continue
            seen_titles.add(title)
            items.append(it)
    # 官方源 (补充)
    for url, src in _RSS_SOURCES:
        for it in _fetch_rss(url, source=src, timeout=10, browser_ua=True):
            title = it.get("title", "")[:120]
            if title in seen_titles:
                continue
            seen_titles.add(title)
            items.append(it)
    return items


# ── CLI 结构化 ──────────────────────────────────────────────────────────────
_PROMPT = """你是美国财政/联储政策工具追踪器。把每条 RSS item 分类为 "属于哪个救债市工具" +
"发生了什么动作" + "1-2 句中文点评"。

预定义的工具 key (必须从这些里选一个, 或返回 "none"):
{toolkit_keys_json}

action_type 枚举 (必须从中选):
- activated: 首次启动某工具 (例: 首次汇市干预)
- expanded:  已在用的工具规模翻倍/扩容 (例: 回购规模 $2B→$4B)
- signaled:  官方口头暗示未来会用 (例: QRA 措辞改 "变化")
- opposed:   Fed 官员反对 (例: Warsh 反对 QE) — 对救市是负面
- announced: 正式公告某未来动作
- reversed:  撤回/失效 (罕见)

对每条 item 输出:
- tool_key: 从上面 toolkit_keys 选, 或 "none" (不相关就 none, 别硬套)
- action_type: 从上面枚举选
- date: YYYY-MM-DD, 优先从 pubDate 提取, 提不到就 "{today}"
- actor: 谁做的 (Bessent | Powell | Fed | FOMC | Treasury | Warsh | ...); 提不到 ""
- amount_desc: 具体金额/规模描述 (例: "$4B/操作 从$2B翻倍"); 无就 ""
- commentary: 1-2 句中文简评 (为什么这个动作重要, 直接影响债市什么, ≤ 60 字)
- verbatim: 原文关键句 (英文原文, ≤ 150 字符)
- confidence: 0-10 (是否明确匹配工具)

**输出规则 (严格)**:
1. 严格 JSON, 只一个根对象 {{"items":[...]}}
2. 不要 markdown / 围栏 / 前言后语
3. items 长度 = 输入 items 长度, 顺序一致
4. 与救债市无关的 → tool_key: "none", confidence: 0

今日: {today}

待解析 RSS items:
{rss_items_json}
"""


def _parse_via_cli(items: list[dict], timeout: int = 120,
                   batch_size: int = 20) -> list[dict]:
    """CLI-batch 结构化。分批发送避免超时。返回与输入等长的 items 数组;
    单批失败该批用 {} 占位以保持索引对齐。"""
    if not items:
        return []
    try:
        from ai_prompt import query_ai_cli
    except Exception as e:
        return [{"_error": f"cli_import_fail: {e}"}]

    def _one_batch(batch: list[dict]) -> list[dict]:
        compact = [{"title": (it.get("title") or "")[:200],
                    "desc": (it.get("desc") or "")[:600],
                    "pubDate": it.get("pubDate", ""),
                    "source": it.get("source", "")}
                   for it in batch]
        prompt = _PROMPT.format(
            toolkit_keys_json=json.dumps(list(TOOLKIT.keys())),
            today=date.today().isoformat(),
            rss_items_json=json.dumps(compact, ensure_ascii=False, indent=2),
        )
        out, status, provider, _ = query_ai_cli(prompt, timeout=timeout)
        if not out:
            return [{} for _ in batch]  # 该批全占位
        parsed = _extract_json_from_cli_output(out)
        if not parsed or not isinstance(parsed.get("items"), list):
            return [{} for _ in batch]
        # 长度校准: 短了补 {}, 长了截
        res = parsed["items"][:len(batch)]
        while len(res) < len(batch):
            res.append({})
        return res

    all_parsed: list[dict] = []
    total = len(items)
    for start in range(0, total, batch_size):
        batch = items[start:start + batch_size]
        all_parsed.extend(_one_batch(batch))
    # 若整个都是空 dict, 报错
    if all(not p for p in all_parsed):
        return [{"_error": "cli_all_batches_empty"}]
    return all_parsed


def _normalize_action(raw: dict, source_item: dict) -> dict | None:
    """规范化 + 校验; 与 toolkit 不匹配 (tool_key=none) 直接丢掉。"""
    tk = (raw.get("tool_key") or "").strip()
    if tk not in TOOLKIT:
        return None
    at = (raw.get("action_type") or "signaled").strip()
    if at not in _ACTION_TYPES:
        at = "signaled"
    d = (raw.get("date") or "").strip()
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        d = date.today().isoformat()
    try:
        conf = int(raw.get("confidence", 0))
    except Exception:
        conf = 0
    conf = max(0, min(10, conf))
    return {
        "tool_key": tk,
        "action_type": at,
        "date": d,
        "actor": (raw.get("actor") or "").strip()[:40],
        "amount_desc": (raw.get("amount_desc") or "").strip()[:120],
        "commentary": (raw.get("commentary") or "").strip()[:200],
        "verbatim": (raw.get("verbatim") or "").strip()[:200],
        "source": source_item.get("source", ""),
        "url": source_item.get("url", ""),
        "confidence": conf,
    }


# ── 30Y 收益率反应 ──────────────────────────────────────────────────────────
def _yield_reaction_bp(action_date: str) -> dict:
    """从 ^TYX (30Y) 拉近 30 天数据, 算 T+0 (当日 close - 前日 close) 和
    T+1d (次日 close - 当日 close) 的 bps 变化。用 basis points 表达。

    yfinance ^TYX 单位是 %, 所以 diff × 100 = bps.
    """
    try:
        import yfinance as yf
        t = yf.Ticker("^TYX")
        hist = t.history(period="60d")
        if hist.empty:
            return {}
        # 找到 action_date 或最近的交易日
        target = datetime.strptime(action_date, "%Y-%m-%d").date()
        # hist.index 是 tz-aware Timestamp
        dates = [ts.date() for ts in hist.index]
        # 找当日或往前推最近的交易日
        try:
            idx = next(i for i in range(len(dates)) if dates[i] >= target)
        except StopIteration:
            return {}
        if idx == 0:
            return {}
        closes = hist["Close"].tolist()
        t0_bp = round((closes[idx] - closes[idx - 1]) * 100, 1)
        result = {"t0_bp": t0_bp, "yield_at_close": round(closes[idx], 3)}
        if idx + 1 < len(closes):
            t1_bp = round((closes[idx + 1] - closes[idx]) * 100, 1)
            result["t1d_bp"] = t1_bp
        return result
    except Exception:
        return {}


# ── 聚合 ────────────────────────────────────────────────────────────────────
def _load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    out = []
    try:
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _append_history(new_actions: list[dict]) -> None:
    if not new_actions:
        return
    try:
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            for a in new_actions:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _dedupe_by_verbatim(actions: list[dict]) -> list[dict]:
    """RSS 会反复出现同一条; 用 (tool_key, date, verbatim[:80]) 去重。"""
    seen = set()
    out = []
    for a in actions:
        key = (a.get("tool_key"), a.get("date"),
               (a.get("verbatim") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _aggregate(all_actions: list[dict]) -> dict:
    """按 tool_key 聚合, 取每个工具的最新事件 + 计数。"""
    by_key: dict[str, list[dict]] = {}
    for a in all_actions:
        by_key.setdefault(a["tool_key"], []).append(a)

    tools_out = []
    for tk, profile in TOOLKIT.items():
        events = sorted(by_key.get(tk, []),
                        key=lambda x: x.get("date", ""), reverse=True)
        last = events[0] if events else None
        # 状态推导:
        # - 有 activated/expanded/announced → "已用"
        # - 只有 signaled → "待发/口头"
        # - 只有 opposed → "被否"
        # - 没事件 → "观察"
        if events:
            atypes = {e["action_type"] for e in events[:3]}
            if {"activated", "expanded", "announced"} & atypes:
                status = "已用"
            elif "opposed" in atypes and not ({"activated", "expanded", "announced"} & atypes):
                status = "被否"
            else:
                status = "口头/待发"
        else:
            status = "观察"

        # T+0/T+1d 收益率反应 (只对最近一次事件算, 且必须是 activated/expanded)
        yield_reaction = {}
        if last and last["action_type"] in ("activated", "expanded", "announced"):
            yield_reaction = _yield_reaction_bp(last["date"])

        tools_out.append({
            "tool_key": tk,
            "zh": profile["zh"],
            "owner": profile["owner"],
            "feasibility": profile["feasibility"],
            "canonical_desc": profile["canonical_desc"],
            "invalidation": profile["invalidation"],
            "status": status,
            "last_action": last,
            "yield_reaction_30y": yield_reaction,
            "event_count_180d": len(events),
        })

    # 排序: 已用 > 口头 > 被否 > 观察; 组内按最近事件时间倒序
    _status_rank = {"已用": 0, "口头/待发": 1, "被否": 2, "观察": 3}
    tools_out.sort(key=lambda x: (
        _status_rank.get(x["status"], 9),
        x["last_action"]["date"] if x["last_action"] else "",
    ), reverse=False)
    # 组内倒序按 last_action.date
    for status_key in _status_rank:
        group = [t for t in tools_out if t["status"] == status_key]
        group.sort(key=lambda x: x["last_action"]["date"] if x["last_action"] else "",
                   reverse=True)
    return {
        "as_of": datetime.now().isoformat(),
        "count_total_actions": len(all_actions),
        "tools": tools_out,
    }


# ── 主入口 ──────────────────────────────────────────────────────────────────
def build_policy_toolkit() -> dict:
    """全量刷新: 拉 RSS → CLI → 合并历史 → 写 latest.json。"""
    rss_items = _fetch_all_rss()
    if not rss_items:
        # RSS 全挂, 仍返回一个空聚合让 dashboard 有 fallback 视图
        agg = _aggregate(_load_history())
        agg["warning"] = "RSS unreachable"
        LATEST_PATH.write_text(json.dumps(agg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        return agg

    parsed = _parse_via_cli(rss_items)
    new_actions = []
    for i, raw in enumerate(parsed):
        if isinstance(raw, dict) and raw.get("_error"):
            # CLI 错就用旧历史, 不写新的
            agg = _aggregate(_load_history())
            agg["warning"] = raw["_error"]
            LATEST_PATH.write_text(json.dumps(agg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            return agg
        if i >= len(rss_items):
            break
        action = _normalize_action(raw, rss_items[i])
        if action and action["confidence"] >= 5:
            new_actions.append(action)

    # 合并历史 + 去重
    history = _load_history()
    combined = _dedupe_by_verbatim(history + new_actions)
    # 只保留过去 180 天
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    combined = [a for a in combined if a.get("date", "") >= cutoff]

    # 把新增的 append 到历史
    new_verbatims = {(a["tool_key"], a["date"], (a["verbatim"] or "")[:80])
                     for a in combined}
    old_verbatims = {(a.get("tool_key"), a.get("date"),
                      (a.get("verbatim") or "")[:80]) for a in history}
    to_append = [a for a in combined
                 if (a["tool_key"], a["date"], (a["verbatim"] or "")[:80])
                 not in old_verbatims]
    _append_history(to_append)

    agg = _aggregate(combined)
    LATEST_PATH.write_text(json.dumps(agg, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return agg


def load_latest() -> dict:
    """只读磁盘; dashboard/webui 用这个。"""
    if not LATEST_PATH.exists():
        return {"as_of": None, "tools": [], "status": "not_computed"}
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"as_of": None, "tools": [], "status": "load_error",
                "error": str(e)[:80]}


# ── CLI 测试 ────────────────────────────────────────────────────────────────
def _cli_main():
    if "--show" in sys.argv:
        print(json.dumps(load_latest(), ensure_ascii=False, indent=2))
        return
    print("Fetching Treasury + Fed RSS...")
    r = build_policy_toolkit()
    print(f"tools: {len(r.get('tools', []))} · "
          f"actions total: {r.get('count_total_actions', 0)}")
    for t in r.get("tools", []):
        la = t.get("last_action") or {}
        yr = t.get("yield_reaction_30y") or {}
        yr_s = ""
        if yr:
            yr_s = f" · 30Y T+0 {yr.get('t0_bp', '?')}bp"
            if "t1d_bp" in yr:
                yr_s += f" T+1d {yr['t1d_bp']}bp"
        print(f"  [{t['status']}] {t['zh']} ({t['owner']}) "
              f"— feas={t['feasibility']} · events={t['event_count_180d']}"
              + (f" · last {la.get('date')} {la.get('action_type')}" if la else "")
              + yr_s)


if __name__ == "__main__":
    _cli_main()
